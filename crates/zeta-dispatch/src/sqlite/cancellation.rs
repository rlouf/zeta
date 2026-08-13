use std::str::FromStr;

use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde_json::{Map, Value};
use zeta_journal::Event;

use super::attempts::{attempt_payload, load_latest_running_attempt};
use super::journal::{append_runtime_event, entry_by_field};
use super::routing::{load_queue_item, queue_item_payload};
use super::{corrupt_projection, database_error, Dispatch, DispatchError};
use crate::dispatch::{
    Attempt, CancellationFinalizationIdentities, CancellationIdentities, CancellationOutcome,
    CancellationStatus, QueueItem, RuntimeEventIdentity,
};
use crate::identity::{queue_item_idempotency_key, QueueItemId, RunId, SessionId};
use crate::state::{AttemptStatus, QueueItemStatus};

impl Dispatch {
    /// Cancels the preferred queue item associated with a public run id.
    ///
    /// Nonterminal work wins over historical terminal rows when a run id was
    /// reused. The resolved item then follows the same intent-first lifecycle
    /// and optional session ownership check as [`Self::cancel_queue_item`].
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when run resolution, queue cancellation, or
    /// its atomic journal transaction fails.
    pub fn cancel_run(
        &mut self,
        run_id: &RunId,
        expected_session_id: Option<&str>,
        reason: Option<&str>,
        identities: CancellationIdentities,
    ) -> Result<CancellationOutcome, DispatchError> {
        let queue_item_id = self
            .connection
            .query_row(
                "SELECT queue.queue_item_id
                 FROM queue_items AS queue
                 LEFT JOIN journal_entries AS input
                   ON input.event_id = queue.event_id
                 WHERE input.run_id = ?1
                    OR EXISTS (
                      SELECT 1
                      FROM attempts AS attempt
                      WHERE attempt.queue_item_id = queue.queue_item_id
                        AND attempt.run_id = ?1
                    )
                 ORDER BY CASE
                    WHEN queue.status IN (
                      'completed', 'failed', 'cancelled',
                      'dead_lettered', 'unhandled'
                    ) THEN 1 ELSE 0
                 END,
                 queue.input_cursor ASC,
                 queue.queue_item_id ASC
                 LIMIT 1",
                params![run_id.as_str()],
                |row| row.get::<_, String>(0),
            )
            .optional()
            .map_err(|database| database_error("resolve cancellation run", database))?;
        let Some(queue_item_id) = queue_item_id else {
            return Ok(CancellationOutcome {
                queue_item_id: None,
                status: CancellationStatus::Unknown,
                changed: false,
                events: Vec::new(),
            });
        };
        let queue_item_id = QueueItemId::from_str(&queue_item_id)
            .map_err(|_error| corrupt_projection("queue_items", "queue_item_id"))?;
        self.cancel_queue_item(&queue_item_id, expected_session_id, reason, identities)
    }

    /// Makes cancellation intent durable and closes queued work immediately.
    ///
    /// Claimed work retains its live claim and returns `cancelling`; the worker
    /// must observe the durable intent and record its terminal attempt. Pending,
    /// available, and retry-scheduled work records intent and cancellation in
    /// one immediate transaction. Repeated calls return stable dispositions
    /// without replacing the first reason.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError::CancellationSessionMismatch`] when an expected
    /// session does not own the item, or another [`DispatchError`] for identity,
    /// lifecycle, projection, or storage failures.
    pub fn cancel_queue_item(
        &mut self,
        queue_item_id: &QueueItemId,
        expected_session_id: Option<&str>,
        reason: Option<&str>,
        identities: CancellationIdentities,
    ) -> Result<CancellationOutcome, DispatchError> {
        if identities.requested.id() == identities.cancelled.id() {
            return Err(DispatchError::DuplicateRuntimeEventIdentity {
                event_id: identities.requested.id().to_owned(),
            });
        }
        if reason == Some("") {
            return Err(DispatchError::InvalidCoordinationInput { field: "reason" });
        }
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|database| database_error("begin queue cancellation", database))?;
        let Some(mut queue_item) = load_queue_item(&transaction, queue_item_id.as_str())? else {
            transaction
                .commit()
                .map_err(|database| database_error("commit unknown cancellation", database))?;
            return Ok(CancellationOutcome {
                queue_item_id: None,
                status: CancellationStatus::Unknown,
                changed: false,
                events: Vec::new(),
            });
        };
        if expected_session_id.is_some()
            && queue_item.session_id.as_ref().map(SessionId::as_str) != expected_session_id
        {
            return Err(DispatchError::CancellationSessionMismatch {
                expected: expected_session_id.unwrap_or_default().to_owned(),
                actual: queue_item.session_id.as_ref().map(ToString::to_string),
            });
        }
        if queue_item.status == QueueItemStatus::Cancelled {
            let events = retained_cancellation_events(&transaction, &queue_item)?;
            transaction
                .commit()
                .map_err(|database| database_error("commit repeated cancellation", database))?;
            return Ok(CancellationOutcome {
                queue_item_id: Some(queue_item.id.clone()),
                status: CancellationStatus::AlreadyCancelled,
                changed: false,
                events,
            });
        }
        if matches!(
            queue_item.status,
            QueueItemStatus::Completed
                | QueueItemStatus::Failed
                | QueueItemStatus::DeadLettered
                | QueueItemStatus::Unhandled
        ) {
            transaction
                .commit()
                .map_err(|database| database_error("commit terminal cancellation", database))?;
            return Ok(CancellationOutcome {
                queue_item_id: Some(queue_item.id.clone()),
                status: CancellationStatus::AlreadyTerminal,
                changed: false,
                events: Vec::new(),
            });
        }
        let input = entry_by_field(&transaction, "event_id", &queue_item.event_id)?
            .ok_or(DispatchError::CorruptProjection {
                table: "queue_items",
                field: "event_id",
            })?
            .event;
        let run_id = queue_run_id(&transaction, &queue_item, &input)?;
        let (requested, request_inserted) = match queue_item.cancellation_requested_event_id {
            Some(ref event_id) => {
                let event = entry_by_field(&transaction, "event_id", event_id)?
                    .ok_or(DispatchError::CorruptProjection {
                        table: "queue_items",
                        field: "cancel_requested_event_id",
                    })?
                    .event;
                (event, false)
            }
            None => {
                let event = cancellation_requested_event(
                    &identities.requested,
                    &input,
                    &queue_item,
                    queue_item.status,
                    reason,
                    run_id.as_deref(),
                );
                let outcome = append_runtime_event(&transaction, event)?;
                queue_item.cancellation_requested_event_id = Some(outcome.event.id.clone());
                queue_item.cancellation_requested_at = Some(outcome.event.timestamp_ms);
                queue_item.cancellation_reason = reason.map(str::to_owned);
                (outcome.event, outcome.inserted)
            }
        };
        if queue_item.status == QueueItemStatus::Claimed {
            transaction
                .commit()
                .map_err(|database| database_error("commit cancellation intent", database))?;
            return Ok(CancellationOutcome {
                queue_item_id: Some(queue_item.id.clone()),
                status: CancellationStatus::Cancelling,
                changed: request_inserted,
                events: vec![requested],
            });
        }
        let cancelled = cancellation_terminal_event(
            &identities.cancelled,
            &queue_item,
            &requested,
            queue_item.cancellation_reason.as_deref().or(reason),
            run_id.as_deref(),
        );
        let cancelled = append_runtime_event(&transaction, cancelled)?;
        let changed = request_inserted || cancelled.inserted;
        transaction
            .commit()
            .map_err(|database| database_error("commit queue cancellation", database))?;
        Ok(CancellationOutcome {
            queue_item_id: Some(queue_item.id),
            status: CancellationStatus::Cancelled,
            changed,
            events: vec![requested, cancelled.event],
        })
    }

    /// Finalizes the oldest unowned queue item with durable cancellation intent.
    ///
    /// Recovery records a running historical attempt as cancelled before the
    /// queue item. Rows with live claims are left to their fenced worker.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] for duplicate identities, corrupt projected
    /// intent or attempts, lifecycle failures, and storage errors.
    pub fn finalize_next_requested_cancellation(
        &mut self,
        identities: CancellationFinalizationIdentities,
        finished_at: &str,
    ) -> Result<Option<CancellationOutcome>, DispatchError> {
        if finished_at.is_empty() {
            return Err(DispatchError::InvalidCoordinationInput {
                field: "finished_at",
            });
        }
        if identities.attempt_cancelled.id() == identities.queue_cancelled.id() {
            return Err(DispatchError::DuplicateRuntimeEventIdentity {
                event_id: identities.attempt_cancelled.id().to_owned(),
            });
        }
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|database| database_error("begin cancellation recovery", database))?;
        let queue_item_id = transaction
            .query_row(
                "SELECT queue.queue_item_id
                 FROM queue_items AS queue
                 WHERE queue.cancel_requested_event_id IS NOT NULL
                   AND queue.status IN ('pending', 'available', 'retry_scheduled')
                   AND NOT EXISTS (
                     SELECT 1 FROM queue_claims AS claim
                     WHERE claim.queue_item_id = queue.queue_item_id
                   )
                 ORDER BY queue.input_cursor ASC, queue.queue_item_id ASC
                 LIMIT 1",
                [],
                |row| row.get::<_, String>(0),
            )
            .optional()
            .map_err(|database| database_error("select cancellation recovery", database))?;
        let Some(queue_item_id) = queue_item_id else {
            transaction.commit().map_err(|database| {
                database_error("commit empty cancellation recovery", database)
            })?;
            return Ok(None);
        };
        let queue_item_id = QueueItemId::from_str(&queue_item_id)
            .map_err(|_error| corrupt_projection("queue_items", "queue_item_id"))?;
        let queue_item = load_queue_item(&transaction, queue_item_id.as_str())?.ok_or(
            DispatchError::CorruptProjection {
                table: "queue_items",
                field: "queue_item_id",
            },
        )?;
        let requested_id = queue_item
            .cancellation_requested_event_id
            .as_deref()
            .ok_or(DispatchError::CorruptProjection {
                table: "queue_items",
                field: "cancel_requested_event_id",
            })?;
        let requested = entry_by_field(&transaction, "event_id", requested_id)?
            .ok_or(DispatchError::CorruptProjection {
                table: "queue_items",
                field: "cancel_requested_event_id",
            })?
            .event;
        let attempt = load_latest_running_attempt(&transaction, &queue_item_id)?;
        let mut events = Vec::new();
        if let Some(attempt) = &attempt {
            let event = recovered_attempt_cancelled_event(
                &identities.attempt_cancelled,
                &requested,
                attempt,
                finished_at,
                queue_item.cancellation_reason.as_deref(),
            );
            events.push(append_runtime_event(&transaction, event)?.event);
        }
        let input = entry_by_field(&transaction, "event_id", &queue_item.event_id)?
            .ok_or(DispatchError::CorruptProjection {
                table: "queue_items",
                field: "event_id",
            })?
            .event;
        let run_id = match &attempt {
            Some(attempt) => attempt.run_id.as_ref().map(ToString::to_string),
            None => queue_run_id(&transaction, &queue_item, &input)?,
        };
        let queue_event = cancellation_terminal_event(
            &identities.queue_cancelled,
            &queue_item,
            &requested,
            queue_item.cancellation_reason.as_deref(),
            run_id.as_deref(),
        );
        events.push(append_runtime_event(&transaction, queue_event)?.event);
        transaction
            .execute(
                "DELETE FROM queue_claims WHERE queue_item_id = ?1",
                params![queue_item_id.as_str()],
            )
            .map_err(|database| database_error("clear recovered cancellation claim", database))?;
        transaction
            .commit()
            .map_err(|database| database_error("commit cancellation recovery", database))?;
        Ok(Some(CancellationOutcome {
            queue_item_id: Some(queue_item_id),
            status: CancellationStatus::Cancelled,
            changed: true,
            events,
        }))
    }
}

fn queue_run_id(
    connection: &Connection,
    queue_item: &QueueItem,
    input: &Event,
) -> Result<Option<String>, DispatchError> {
    if input.run_id.is_some() {
        return Ok(input.run_id.clone());
    }
    connection
        .query_row(
            "SELECT run_id FROM attempts
             WHERE queue_item_id = ?1 AND run_id IS NOT NULL
             ORDER BY attempt_number DESC LIMIT 1",
            params![queue_item.id.as_str()],
            |row| row.get::<_, String>(0),
        )
        .optional()
        .map_err(|database| database_error("resolve queue run", database))
}

fn cancellation_requested_event(
    identity: &RuntimeEventIdentity,
    input: &Event,
    queue_item: &QueueItem,
    status: QueueItemStatus,
    reason: Option<&str>,
    run_id: Option<&str>,
) -> Event {
    let mut payload = queue_item_payload(queue_item, status);
    if let Some(reason) = reason {
        payload.insert("reason".to_owned(), Value::String(reason.to_owned()));
    }
    Event {
        id: identity.id().to_owned(),
        event_type: "runtime.queue_item.cancel_requested".to_owned(),
        source: "zeta".to_owned(),
        payload,
        idempotency_key: Some(format!(
            "queue_item:{}:{}:cancel_requested",
            queue_item.event_id, queue_item.target_agent
        )),
        caused_by: Some(input.id.clone()),
        session_id: queue_item.session_id.as_ref().map(ToString::to_string),
        run_id: run_id.map(str::to_owned),
        turn_id: None,
        timestamp_ms: identity.timestamp_ms(),
        cursor: None,
    }
}

fn cancellation_terminal_event(
    identity: &RuntimeEventIdentity,
    queue_item: &QueueItem,
    requested: &Event,
    reason: Option<&str>,
    run_id: Option<&str>,
) -> Event {
    let mut payload = queue_item_payload(queue_item, QueueItemStatus::Cancelled);
    let mut result = Map::new();
    result.insert("outcome".to_owned(), Value::String("cancelled".to_owned()));
    if let Some(reason) = reason {
        payload.insert("reason".to_owned(), Value::String(reason.to_owned()));
        result.insert("reason".to_owned(), Value::String(reason.to_owned()));
    }
    payload.insert("result".to_owned(), Value::Object(result));
    Event {
        id: identity.id().to_owned(),
        event_type: "runtime.queue_item.cancelled".to_owned(),
        source: "zeta".to_owned(),
        payload,
        idempotency_key: Some(queue_item_idempotency_key(
            &queue_item.event_id,
            &queue_item.target_agent,
            QueueItemStatus::Cancelled,
        )),
        caused_by: Some(requested.id.clone()),
        session_id: queue_item.session_id.as_ref().map(ToString::to_string),
        run_id: run_id.map(str::to_owned),
        turn_id: None,
        timestamp_ms: identity.timestamp_ms(),
        cursor: None,
    }
}

pub(super) fn live_cancelled_events(
    identities: &[RuntimeEventIdentity; 2],
    input: &Event,
    queue_item: &QueueItem,
    attempt: &Attempt,
    finished_at: &str,
    raw_result: Option<&Map<String, Value>>,
) -> [Event; 2] {
    let mut result = raw_result.cloned().unwrap_or_default();
    result.insert("outcome".to_owned(), Value::String("cancelled".to_owned()));
    result.insert(
        "stop_reason".to_owned(),
        Value::String("aborted".to_owned()),
    );
    let mut terminal_attempt = attempt_payload(attempt, AttemptStatus::Cancelled);
    terminal_attempt.insert(
        "finished_at".to_owned(),
        Value::String(finished_at.to_owned()),
    );
    terminal_attempt.insert("result".to_owned(), Value::Object(result.clone()));
    let attempt_event = Event {
        id: identities[0].id().to_owned(),
        event_type: "runtime.attempt.cancelled".to_owned(),
        source: "zeta".to_owned(),
        payload: terminal_attempt,
        idempotency_key: Some(crate::identity::attempt_idempotency_key(
            &attempt.queue_item_id,
            attempt.attempt_number,
            AttemptStatus::Cancelled,
        )),
        caused_by: Some(input.id.clone()),
        session_id: attempt.session_id.as_ref().map(ToString::to_string),
        run_id: attempt.run_id.as_ref().map(ToString::to_string),
        turn_id: input.turn_id.clone(),
        timestamp_ms: identities[0].timestamp_ms(),
        cursor: None,
    };
    let mut terminal_queue = queue_item_payload(queue_item, QueueItemStatus::Cancelled);
    terminal_queue.insert("result".to_owned(), Value::Object(result));
    let queue_event = Event {
        id: identities[1].id().to_owned(),
        event_type: "runtime.queue_item.cancelled".to_owned(),
        source: "zeta".to_owned(),
        payload: terminal_queue,
        idempotency_key: Some(queue_item_idempotency_key(
            &queue_item.event_id,
            &queue_item.target_agent,
            QueueItemStatus::Cancelled,
        )),
        caused_by: Some(input.id.clone()),
        session_id: queue_item.session_id.as_ref().map(ToString::to_string),
        run_id: attempt.run_id.as_ref().map(ToString::to_string),
        turn_id: input.turn_id.clone(),
        timestamp_ms: identities[1].timestamp_ms(),
        cursor: None,
    };
    [attempt_event, queue_event]
}

fn recovered_attempt_cancelled_event(
    identity: &RuntimeEventIdentity,
    requested: &Event,
    attempt: &Attempt,
    finished_at: &str,
    reason: Option<&str>,
) -> Event {
    let mut result = Map::new();
    result.insert("outcome".to_owned(), Value::String("cancelled".to_owned()));
    if let Some(reason) = reason {
        result.insert("reason".to_owned(), Value::String(reason.to_owned()));
    }
    let mut payload = attempt_payload(attempt, AttemptStatus::Cancelled);
    payload.remove("error");
    payload.remove("project_generation");
    payload.insert(
        "finished_at".to_owned(),
        Value::String(finished_at.to_owned()),
    );
    payload
        .entry("worker_name".to_owned())
        .or_insert(Value::Null);
    payload.insert("result".to_owned(), Value::Object(result));
    Event {
        id: identity.id().to_owned(),
        event_type: "runtime.attempt.cancelled".to_owned(),
        source: "zeta".to_owned(),
        payload,
        idempotency_key: Some(crate::identity::attempt_idempotency_key(
            &attempt.queue_item_id,
            attempt.attempt_number,
            AttemptStatus::Cancelled,
        )),
        caused_by: Some(requested.id.clone()),
        session_id: attempt.session_id.as_ref().map(ToString::to_string),
        run_id: attempt.run_id.as_ref().map(ToString::to_string),
        turn_id: None,
        timestamp_ms: identity.timestamp_ms(),
        cursor: None,
    }
}

fn retained_cancellation_events(
    connection: &Connection,
    queue_item: &QueueItem,
) -> Result<Vec<Event>, DispatchError> {
    let Some(requested_id) = &queue_item.cancellation_requested_event_id else {
        return Ok(Vec::new());
    };
    let requested = entry_by_field(connection, "event_id", requested_id)?
        .ok_or(DispatchError::CorruptProjection {
            table: "queue_items",
            field: "cancel_requested_event_id",
        })?
        .event;
    let cancelled_key = queue_item_idempotency_key(
        &queue_item.event_id,
        &queue_item.target_agent,
        QueueItemStatus::Cancelled,
    );
    let cancelled = entry_by_field(connection, "idempotency_key", &cancelled_key)?
        .ok_or(DispatchError::CorruptProjection {
            table: "queue_items",
            field: "cancelled_event",
        })?
        .event;
    Ok(vec![requested, cancelled])
}
