use std::collections::HashSet;
use std::str::FromStr;

use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};

use super::{corrupt_projection, database_error, Dispatch, DispatchError};
use crate::dispatch::{LockLease, QueueClaim};
use crate::identity::{ClaimToken, QueueItemId};

impl Dispatch {
    /// Claims the oldest eligible queue item and all of its authored locks.
    ///
    /// The caller supplies an opaque fresh token. Claim and lock insertion
    /// share one immediate transaction. Earlier unbound work, earlier work in
    /// the same session, or any live lock conflict makes a candidate ineligible.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] for an empty worker, invalid lease, reused
    /// token, corrupt projection, or storage failure.
    pub fn claim_next_queue_item(
        &mut self,
        worker_name: &str,
        token: ClaimToken,
        lease_ms: u64,
        now_ms: i64,
    ) -> Result<Option<QueueClaim>, DispatchError> {
        if worker_name.is_empty() {
            return Err(DispatchError::InvalidCoordinationInput {
                field: "worker_name",
            });
        }
        let claimed_until = lease_deadline(now_ms, lease_ms)?;
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|error| database_error("begin queue claim", error))?;
        reconcile_expired_in_transaction(&transaction, now_ms)?;
        if claim_token_exists(&transaction, &token)? {
            return Err(DispatchError::InvalidCoordinationInput {
                field: "claim_token",
            });
        }
        let candidates = claim_candidates(&transaction, now_ms)?;
        for candidate in candidates {
            if !locks_are_available(&transaction, &candidate.lock_keys, now_ms)? {
                continue;
            }
            transaction
                .execute(
                    "INSERT INTO queue_claims (
                        queue_item_id, worker_name, claim_token,
                        claimed_at, claimed_until
                     ) VALUES (?1, ?2, ?3, ?4, ?5)",
                    params![
                        candidate.queue_item_id.as_str(),
                        worker_name,
                        token.as_str(),
                        now_ms,
                        claimed_until,
                    ],
                )
                .map_err(|error| database_error("insert queue claim", error))?;
            for lock_key in &candidate.lock_keys {
                transaction
                    .execute(
                        "INSERT INTO locks (
                            lock_key, owner, acquired_at, expires_at
                         ) VALUES (?1, ?2, ?3, ?4)",
                        params![lock_key, token.as_str(), now_ms, claimed_until],
                    )
                    .map_err(|error| database_error("acquire queue lock", error))?;
            }
            transaction
                .commit()
                .map_err(|error| database_error("commit queue claim", error))?;
            return Ok(Some(QueueClaim {
                queue_item_id: candidate.queue_item_id,
                worker_name: worker_name.to_owned(),
                token,
                claimed_until,
            }));
        }
        transaction
            .commit()
            .map_err(|error| database_error("commit empty queue claim", error))?;
        Ok(None)
    }

    /// Reports whether a claim still owns its unexpired coordination row.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when the ownership row cannot be read.
    pub fn claim_is_current(&self, claim: &QueueClaim, now_ms: i64) -> Result<bool, DispatchError> {
        claim_is_current_in(&self.connection, claim, now_ms)
    }

    /// Renews one current claim and every lock held by its exact token.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] for an invalid lease or storage failure.
    pub fn renew_claim(
        &mut self,
        claim: &QueueClaim,
        lease_ms: u64,
        now_ms: i64,
    ) -> Result<bool, DispatchError> {
        let claimed_until = lease_deadline(now_ms, lease_ms)?;
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|error| database_error("begin claim renewal", error))?;
        let updated = transaction
            .execute(
                "UPDATE queue_claims
                 SET claimed_until = ?1
                 WHERE queue_item_id = ?2
                   AND worker_name = ?3
                   AND claim_token = ?4
                   AND claimed_until > ?5",
                params![
                    claimed_until,
                    claim.queue_item_id.as_str(),
                    &claim.worker_name,
                    claim.token.as_str(),
                    now_ms,
                ],
            )
            .map_err(|error| database_error("renew queue claim", error))?;
        if updated == 1 {
            transaction
                .execute(
                    "UPDATE locks SET expires_at = ?1 WHERE owner = ?2",
                    params![claimed_until, claim.token.as_str()],
                )
                .map_err(|error| database_error("renew queue locks", error))?;
        }
        transaction
            .commit()
            .map_err(|error| database_error("commit claim renewal", error))?;
        Ok(updated == 1)
    }

    /// Releases one exact current claim and all locks owned by its token.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when coordination rows cannot be updated.
    pub fn release_claim(
        &mut self,
        claim: &QueueClaim,
        now_ms: i64,
    ) -> Result<bool, DispatchError> {
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|error| database_error("begin claim release", error))?;
        let released = transaction
            .execute(
                "DELETE FROM queue_claims
                 WHERE queue_item_id = ?1
                   AND worker_name = ?2
                   AND claim_token = ?3
                   AND claimed_until > ?4",
                params![
                    claim.queue_item_id.as_str(),
                    &claim.worker_name,
                    claim.token.as_str(),
                    now_ms,
                ],
            )
            .map_err(|error| database_error("release queue claim", error))?;
        if released == 1 {
            transaction
                .execute(
                    "UPDATE queue_items
                     SET status = 'available'
                     WHERE queue_item_id = ?1 AND status = 'claimed'",
                    params![claim.queue_item_id.as_str()],
                )
                .map_err(|error| database_error("release claimed projection", error))?;
        }
        transaction
            .commit()
            .map_err(|error| database_error("commit claim release", error))?;
        Ok(released == 1)
    }

    /// Releases every claim whose exclusive deadline has passed.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when expired coordination cannot be reconciled.
    pub fn reconcile_expired_claims(&mut self, now_ms: i64) -> Result<usize, DispatchError> {
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|error| database_error("begin expired claim reconciliation", error))?;
        let reconciled = reconcile_expired_in_transaction(&transaction, now_ms)?;
        transaction
            .commit()
            .map_err(|error| database_error("commit expired claim reconciliation", error))?;
        Ok(reconciled)
    }

    /// Returns every live lock in key order.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when a lock row cannot be read or rehydrated.
    pub fn list_locks(&self) -> Result<Vec<LockLease>, DispatchError> {
        load_locks(&self.connection)
    }
}

struct ClaimCandidate {
    queue_item_id: QueueItemId,
    lock_keys: Vec<String>,
}

fn claim_candidates(
    connection: &Connection,
    now_ms: i64,
) -> Result<Vec<ClaimCandidate>, DispatchError> {
    let mut statement = connection
        .prepare(
            "SELECT queue.queue_item_id, queue.lock_keys_json
             FROM queue_items AS queue
             WHERE queue.status = 'available'
               AND COALESCE(queue.available_at, queue.updated_at) <= ?1
               AND NOT EXISTS (
                 SELECT 1 FROM queue_claims AS claim
                 WHERE claim.queue_item_id = queue.queue_item_id
               )
               AND queue.cancel_requested_event_id IS NULL
               AND NOT EXISTS (
                 SELECT 1 FROM queue_items AS barrier
                 WHERE barrier.input_cursor < queue.input_cursor
                   AND barrier.target_agent = ''
                   AND barrier.status = 'pending'
               )
               AND (
                 queue.session_id IS NULL OR NOT EXISTS (
                   SELECT 1 FROM queue_items AS earlier
                   WHERE earlier.session_id = queue.session_id
                     AND earlier.input_cursor < queue.input_cursor
                     AND earlier.status NOT IN (
                       'completed', 'cancelled', 'dead_lettered', 'unhandled'
                     )
                 )
               )
             ORDER BY queue.input_cursor ASC, queue.queue_item_id ASC",
        )
        .map_err(|error| database_error("prepare claim candidates", error))?;
    let rows = statement
        .query_map(params![now_ms], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })
        .map_err(|error| database_error("read claim candidates", error))?;
    let mut candidates = Vec::new();
    for row in rows {
        let (queue_item_id, lock_keys_json) =
            row.map_err(|error| database_error("read claim candidate", error))?;
        let queue_item_id = QueueItemId::from_str(&queue_item_id)
            .map_err(|_error| corrupt_projection("queue_items", "queue_item_id"))?;
        let lock_keys: Vec<String> = serde_json::from_str(&lock_keys_json)
            .map_err(|_error| corrupt_projection("queue_items", "lock_keys_json"))?;
        let mut seen = HashSet::new();
        for lock_key in &lock_keys {
            if lock_key.is_empty() || !seen.insert(lock_key) {
                return Err(corrupt_projection("queue_items", "lock_keys_json"));
            }
        }
        candidates.push(ClaimCandidate {
            queue_item_id,
            lock_keys,
        });
    }
    Ok(candidates)
}

fn locks_are_available(
    connection: &Connection,
    lock_keys: &[String],
    now_ms: i64,
) -> Result<bool, DispatchError> {
    for lock_key in lock_keys {
        let held = connection
            .query_row(
                "SELECT 1 FROM locks
                 WHERE lock_key = ?1 AND expires_at > ?2",
                params![lock_key, now_ms],
                |_row| Ok(()),
            )
            .optional()
            .map_err(|error| database_error("check queue lock", error))?;
        if held.is_some() {
            return Ok(false);
        }
    }
    Ok(true)
}

fn claim_token_exists(connection: &Connection, token: &ClaimToken) -> Result<bool, DispatchError> {
    let existing = connection
        .query_row(
            "SELECT 1 FROM queue_claims WHERE claim_token = ?1",
            params![token.as_str()],
            |_row| Ok(()),
        )
        .optional()
        .map_err(|error| database_error("check claim token", error))?;
    Ok(existing.is_some())
}

pub(super) fn claim_is_current_in(
    connection: &Connection,
    claim: &QueueClaim,
    now_ms: i64,
) -> Result<bool, DispatchError> {
    let current = connection
        .query_row(
            "SELECT 1 FROM queue_claims
             WHERE queue_item_id = ?1
               AND worker_name = ?2
               AND claim_token = ?3
               AND claimed_until > ?4",
            params![
                claim.queue_item_id.as_str(),
                &claim.worker_name,
                claim.token.as_str(),
                now_ms,
            ],
            |_row| Ok(()),
        )
        .optional()
        .map_err(|error| database_error("check queue claim", error))?;
    Ok(current.is_some())
}

pub(super) fn release_claim_in_transaction(
    connection: &Connection,
    claim: &QueueClaim,
    operation: &'static str,
) -> Result<(), DispatchError> {
    let deleted = connection
        .execute(
            "DELETE FROM queue_claims
             WHERE queue_item_id = ?1
               AND worker_name = ?2
               AND claim_token = ?3",
            params![
                claim.queue_item_id.as_str(),
                &claim.worker_name,
                claim.token.as_str(),
            ],
        )
        .map_err(|database| database_error(operation, database))?;
    if deleted != 1 {
        return Err(DispatchError::ClaimNotCurrent {
            queue_item_id: claim.queue_item_id.clone(),
        });
    }
    Ok(())
}

fn lease_deadline(now_ms: i64, lease_ms: u64) -> Result<i64, DispatchError> {
    if lease_ms == 0 || lease_ms > i64::MAX as u64 {
        return Err(DispatchError::InvalidCoordinationInput { field: "lease_ms" });
    }
    let lease_ms = lease_ms as i64;
    now_ms
        .checked_add(lease_ms)
        .ok_or(DispatchError::InvalidCoordinationInput { field: "lease_ms" })
}

fn reconcile_expired_in_transaction(
    connection: &Connection,
    now_ms: i64,
) -> Result<usize, DispatchError> {
    connection
        .execute(
            "UPDATE queue_items
             SET status = 'available'
             WHERE status = 'claimed'
               AND queue_item_id IN (
                 SELECT queue_item_id FROM queue_claims
                 WHERE claimed_until <= ?1
               )",
            params![now_ms],
        )
        .map_err(|error| database_error("recover expired queue projection", error))?;
    let deleted = connection
        .execute(
            "DELETE FROM queue_claims WHERE claimed_until <= ?1",
            params![now_ms],
        )
        .map_err(|error| database_error("delete expired queue claims", error))?;
    Ok(deleted)
}

fn load_locks(connection: &Connection) -> Result<Vec<LockLease>, DispatchError> {
    let mut statement = connection
        .prepare(
            "SELECT lock_key, owner, acquired_at, expires_at
             FROM locks ORDER BY lock_key ASC",
        )
        .map_err(|error| database_error("prepare lock read", error))?;
    let rows = statement
        .query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, i64>(2)?,
                row.get::<_, i64>(3)?,
            ))
        })
        .map_err(|error| database_error("read locks", error))?;
    let mut locks = Vec::new();
    for row in rows {
        let (key, owner, acquired_at, expires_at) =
            row.map_err(|error| database_error("read lock", error))?;
        let owner =
            ClaimToken::from_str(&owner).map_err(|_error| corrupt_projection("locks", "owner"))?;
        if key.is_empty() || expires_at <= acquired_at {
            return Err(corrupt_projection("locks", "lease"));
        }
        locks.push(LockLease {
            key,
            owner,
            acquired_at,
            expires_at,
        });
    }
    Ok(locks)
}
