use std::collections::HashSet;
use std::str::FromStr;

use rusqlite::{params, Connection, OptionalExtension, Row, TransactionBehavior};
use serde_json::{Map, Value};
use time::{format_description::well_known::Rfc3339, OffsetDateTime};
use zeta_journal::Event;

use super::cancellation::live_cancelled_events;
use super::coordination::{claim_is_current_in, release_claim_in_transaction};
use super::journal::{
    append_lifecycle_candidate, append_runtime_event, entry_by_field, same_lifecycle_intention,
    validate_distinct_runtime_identities, validate_event_identity,
};
use super::projection::index_event;
use super::resources::{cancel_resource_in_transaction, resource_kind_for_handle};
use super::routing::{load_queue_item, queue_item_payload};
use super::{
    corrupt_projection, database_error, nonnegative_u32_projection, optional_payload_string,
    required_runtime_id, validate_optional_runtime_id, Dispatch, DispatchError,
};
use crate::dispatch::{
    Attempt, AttemptCompletion, AttemptCompletionDisposition, AttemptControl, AttemptFailure,
    QueueClaim, QueueItem, RuntimeEventIdentity, StartedAttempt,
};
use crate::identity::{
    attempt_id, queue_item_attempt_idempotency_key, queue_item_idempotency_key, run_id_for_attempt,
    AttemptId, QueueItemId, RunId, SessionId,
};
use crate::state::{
    classify_attempt_failure_code, AttemptFailureCode, AttemptStatus, FailureClass,
    QueueItemStatus, RetryPolicy,
};

impl Dispatch {
    /// Returns all durable attempts in input and attempt-number order.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when a projection row cannot be read or
    /// rehydrated as the public typed model.
    pub fn list_attempts(&self) -> Result<Vec<Attempt>, DispatchError> {
        load_attempts(&self.connection)
    }

    /// Commits queue-claimed and attempt-started facts under one live claim.
    ///
    /// The final ownership check, both journal appends, both projections, and
    /// the attempt's claim-token association share one immediate transaction.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError::ClaimNotCurrent`] after lease expiry or for a
    /// stale worker/token pair, and another [`DispatchError`] for invalid
    /// identities, lifecycle projection, or storage failure.
    pub fn start_claimed_attempt(
        &mut self,
        claim: &QueueClaim,
        now_ms: i64,
        queue_identity: RuntimeEventIdentity,
        attempt_identity: RuntimeEventIdentity,
        started_at: &str,
        claimed_run_id: Option<&str>,
    ) -> Result<StartedAttempt, DispatchError> {
        if started_at.is_empty() {
            return Err(DispatchError::InvalidCoordinationInput {
                field: "started_at",
            });
        }
        if queue_identity.id() == attempt_identity.id() {
            return Err(DispatchError::DuplicateRuntimeEventIdentity {
                event_id: queue_identity.id().to_owned(),
            });
        }
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|error| database_error("begin attempt start", error))?;
        if !claim_is_current_in(&transaction, claim, now_ms)? {
            return Err(DispatchError::ClaimNotCurrent {
                queue_item_id: claim.queue_item_id.clone(),
            });
        }
        let Some(queue_item) = load_queue_item(&transaction, claim.queue_item_id.as_str())? else {
            return Err(DispatchError::CorruptProjection {
                table: "queue_items",
                field: "queue_item_id",
            });
        };
        if queue_item.target_agent.is_empty() {
            return Err(DispatchError::InvalidCoordinationInput {
                field: "target_agent",
            });
        }
        let input = entry_by_field(&transaction, "event_id", &queue_item.event_id)?
            .ok_or(DispatchError::CorruptProjection {
                table: "queue_items",
                field: "event_id",
            })?
            .event;
        if let Some(attempt) = load_running_attempt_for_claim(&transaction, claim)? {
            if attempt.started_at != started_at {
                return Err(DispatchError::InvalidCoordinationInput {
                    field: "started_at",
                });
            }
            let Some(run_id) = &attempt.run_id else {
                return Err(DispatchError::CorruptProjection {
                    table: "attempts",
                    field: "run_id",
                });
            };
            if run_id_for_attempt(claimed_run_id, &attempt.id) != *run_id {
                return Err(DispatchError::InvalidCoordinationInput { field: "run_id" });
            }
            let candidates = [
                claimed_queue_event(
                    &queue_identity,
                    &input,
                    &queue_item,
                    attempt.attempt_number,
                    run_id,
                ),
                started_attempt_event(
                    &attempt_identity,
                    &input,
                    &queue_item,
                    &AttemptStartFields {
                        attempt_id: &attempt.id,
                        attempt_number: attempt.attempt_number,
                        run_id,
                        worker_name: &claim.worker_name,
                        started_at,
                    },
                ),
            ];
            let mut events = Vec::new();
            for candidate in candidates {
                let Some(idempotency_key) = &candidate.idempotency_key else {
                    return Err(DispatchError::CorruptProjection {
                        table: "attempts",
                        field: "idempotency_key",
                    });
                };
                let Some(retained) =
                    entry_by_field(&transaction, "idempotency_key", idempotency_key)?
                else {
                    return Err(DispatchError::CorruptProjection {
                        table: "attempts",
                        field: "lifecycle_event",
                    });
                };
                if !same_lifecycle_intention(&candidate, &retained.event) {
                    return Err(DispatchError::RuntimeEventIdentityCollision {
                        event_id: candidate.id,
                    });
                }
                events.push(retained.event);
            }
            transaction
                .commit()
                .map_err(|error| database_error("commit attempt start retry", error))?;
            return Ok(StartedAttempt { attempt, events });
        }
        let Some(attempt_number) = queue_item.attempt_count.checked_add(1) else {
            return Err(DispatchError::InvalidCoordinationInput {
                field: "attempt_number",
            });
        };
        let attempt_id = attempt_id(&queue_item.id, attempt_number);
        let run_id = run_id_for_attempt(claimed_run_id, &attempt_id);
        let queue_event = claimed_queue_event(
            &queue_identity,
            &input,
            &queue_item,
            attempt_number,
            &run_id,
        );
        let attempt_event = started_attempt_event(
            &attempt_identity,
            &input,
            &queue_item,
            &AttemptStartFields {
                attempt_id: &attempt_id,
                attempt_number,
                run_id: &run_id,
                worker_name: &claim.worker_name,
                started_at,
            },
        );
        let mut events = Vec::new();
        for event in [queue_event, attempt_event] {
            validate_event_identity(&event)?;
            let candidate = event.clone();
            let outcome = append_lifecycle_candidate(&transaction, event)?;
            if !outcome.inserted && !same_lifecycle_intention(&candidate, &outcome.event) {
                return Err(DispatchError::RuntimeEventIdentityCollision {
                    event_id: candidate.id,
                });
            }
            if outcome.inserted {
                index_event(&transaction, &outcome.event)?;
            }
            events.push(outcome.event);
        }
        transaction
            .execute(
                "UPDATE attempts
                 SET claim_token = ?1, heartbeat_at = ?2
                 WHERE attempt_id = ?3",
                params![claim.token.as_str(), now_ms, attempt_id.as_str()],
            )
            .map_err(|error| database_error("fence running attempt", error))?;
        transaction
            .commit()
            .map_err(|error| database_error("commit attempt start", error))?;
        Ok(StartedAttempt {
            attempt: Attempt {
                id: attempt_id,
                queue_item_id: queue_item.id,
                event_id: queue_item.event_id,
                attempt_number,
                target_agent: queue_item.target_agent,
                worker_name: Some(claim.worker_name.clone()),
                status: AttemptStatus::Running,
                started_at: started_at.to_owned(),
                finished_at: None,
                error: None,
                session_id: queue_item.session_id,
                run_id: Some(run_id),
                project_revision: queue_item.project_revision,
            },
            events,
        })
    }

    /// Records one fenced attempt failure and its retry or dead-letter decision.
    ///
    /// Both lifecycle facts, their projections, claim release, and lock release
    /// share one immediate transaction. Retry availability uses the failed
    /// attempt number for backoff and the next attempt number for idempotency.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError::ClaimNotCurrent`] for stale ownership, or
    /// another [`DispatchError`] for invalid inputs, missing running state,
    /// lifecycle projection, retry-delay overflow, or storage failure.
    pub fn fail_claimed_attempt(
        &mut self,
        claim: &QueueClaim,
        now_ms: i64,
        identities: [RuntimeEventIdentity; 2],
        failure: &AttemptFailure,
    ) -> Result<Vec<Event>, DispatchError> {
        if failure.finished_at.is_empty() {
            return Err(DispatchError::InvalidCoordinationInput {
                field: "finished_at",
            });
        }
        if failure.error.is_empty() {
            return Err(DispatchError::InvalidCoordinationInput { field: "error" });
        }
        if identities[0].id() == identities[1].id() {
            return Err(DispatchError::DuplicateRuntimeEventIdentity {
                event_id: identities[0].id().to_owned(),
            });
        }
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|database| database_error("begin attempt failure", database))?;
        if !claim_is_current_in(&transaction, claim, now_ms)? {
            return Err(DispatchError::ClaimNotCurrent {
                queue_item_id: claim.queue_item_id.clone(),
            });
        }
        let Some(attempt) = load_running_attempt_for_claim(&transaction, claim)? else {
            return Err(DispatchError::InvalidCoordinationInput {
                field: "running_attempt",
            });
        };
        let Some(queue_item) = load_queue_item(&transaction, claim.queue_item_id.as_str())? else {
            return Err(DispatchError::CorruptProjection {
                table: "queue_items",
                field: "queue_item_id",
            });
        };
        let input = entry_by_field(&transaction, "event_id", &attempt.event_id)?
            .ok_or(DispatchError::CorruptProjection {
                table: "attempts",
                field: "event_id",
            })?
            .event;
        if queue_item.cancellation_requested_event_id.is_some() {
            let candidates = live_cancelled_events(
                &identities,
                &input,
                &queue_item,
                &attempt,
                &failure.finished_at,
                None,
            );
            let mut events = Vec::new();
            for event in candidates {
                events.push(append_runtime_event(&transaction, event)?.event);
            }
            release_claim_in_transaction(&transaction, claim, "release cancelled attempt claim")?;
            transaction
                .commit()
                .map_err(|database| database_error("commit cancelled attempt", database))?;
            return Ok(events);
        }
        let failure = AttemptFailureFields {
            finished_at: &failure.finished_at,
            error: &failure.error,
            error_code: failure.error_code,
            retry_policy: failure.retry_policy,
            now_ms,
        };
        let failed = failed_attempt_event(&identities[0], &input, &attempt, &failure);
        let disposition = failed_queue_disposition_event(
            &identities[1],
            &input,
            &queue_item,
            &attempt,
            &failure,
        )?;
        let mut events = Vec::new();
        for event in [failed, disposition] {
            validate_event_identity(&event)?;
            let candidate = event.clone();
            let outcome = append_lifecycle_candidate(&transaction, event)?;
            if !outcome.inserted && !same_lifecycle_intention(&candidate, &outcome.event) {
                return Err(DispatchError::RuntimeEventIdentityCollision {
                    event_id: candidate.id,
                });
            }
            if outcome.inserted {
                index_event(&transaction, &outcome.event)?;
            }
            events.push(outcome.event);
        }
        release_claim_in_transaction(&transaction, claim, "release failed attempt claim")?;
        transaction
            .commit()
            .map_err(|database| database_error("commit attempt failure", database))?;
        Ok(events)
    }

    /// Commits a successful attempt, ordered controls, and queue completion.
    ///
    /// The result is validated before its first journal append. Control
    /// requests are committed in numeric position order between the attempt
    /// and queue terminal facts. A durable cancellation request observed under
    /// the same fence wins and records cancellation instead.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError::ClaimNotCurrent`] for stale ownership,
    /// [`DispatchError::InvalidCompletion`] for malformed proposals, or another
    /// [`DispatchError`] for identity, lifecycle, projection, and storage
    /// failures. Every error leaves the complete success batch unapplied.
    pub fn complete_claimed_attempt(
        &mut self,
        claim: &QueueClaim,
        now_ms: i64,
        identities: &[RuntimeEventIdentity],
        completion: &AttemptCompletion,
    ) -> Result<Vec<Event>, DispatchError> {
        if completion.finished_at.is_empty() {
            return Err(DispatchError::InvalidCompletion {
                field: "finished_at",
            });
        }
        let (completed_at, controls) = completion_controls(completion)?;
        let expected_identities = controls.len() + 2;
        if identities.len() != expected_identities {
            return Err(DispatchError::RuntimeEventIdentityCount {
                expected: expected_identities,
                actual: identities.len(),
            });
        }
        validate_distinct_runtime_identities(identities)?;
        let result = completion_result(completion, &controls)?;
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|database| database_error("begin attempt completion", database))?;
        if !claim_is_current_in(&transaction, claim, now_ms)? {
            return Err(DispatchError::ClaimNotCurrent {
                queue_item_id: claim.queue_item_id.clone(),
            });
        }
        let Some(attempt) = load_running_attempt_for_claim(&transaction, claim)? else {
            return Err(DispatchError::InvalidCoordinationInput {
                field: "running_attempt",
            });
        };
        let Some(queue_item) = load_queue_item(&transaction, claim.queue_item_id.as_str())? else {
            return Err(DispatchError::CorruptProjection {
                table: "queue_items",
                field: "queue_item_id",
            });
        };
        let input = entry_by_field(&transaction, "event_id", &attempt.event_id)?
            .ok_or(DispatchError::CorruptProjection {
                table: "attempts",
                field: "event_id",
            })?
            .event;
        let cancelled = match completion.disposition {
            AttemptCompletionDisposition::Succeeded => {
                queue_item.cancellation_requested_event_id.is_some()
            }
            AttemptCompletionDisposition::Cancelled => true,
        };
        if cancelled {
            let cancellation_identities = [
                identities[0].clone(),
                identities[identities.len() - 1].clone(),
            ];
            let candidates = live_cancelled_events(
                &cancellation_identities,
                &input,
                &queue_item,
                &attempt,
                &completion.finished_at,
                Some(&result),
            );
            let mut events = Vec::new();
            for event in candidates {
                events.push(append_runtime_event(&transaction, event)?.event);
            }
            release_claim_in_transaction(&transaction, claim, "release completed cancellation")?;
            transaction
                .commit()
                .map_err(|database| database_error("commit completed cancellation", database))?;
            return Ok(events);
        }
        let completed_attempt =
            completed_attempt_event(&identities[0], &input, &attempt, completion, &result);
        let completed_attempt = append_runtime_event(&transaction, completed_attempt)?.event;
        let mut events = vec![completed_attempt.clone()];
        for (control, identity) in controls.iter().zip(&identities[1..identities.len() - 1]) {
            match control {
                AttemptControl::Cancel {
                    handle,
                    reason,
                    source_agent_id,
                    source_session_id,
                    position: _position,
                } => {
                    if source_agent_id != &attempt.target_agent
                        || attempt.session_id.as_ref().map(SessionId::as_str)
                            != Some(source_session_id.as_str())
                    {
                        return Err(DispatchError::CancellationAuthorityMismatch {
                            handle: handle.clone(),
                        });
                    }
                    let resource_kind = resource_kind_for_handle(handle)?;
                    let outcome = cancel_resource_in_transaction(
                        &transaction,
                        handle,
                        reason.as_deref(),
                        Some(source_agent_id),
                        Some(source_session_id),
                        identity,
                        resource_kind,
                    )?;
                    if let Some(event) = outcome.event {
                        events.push(event);
                    }
                }
                AttemptControl::Publish { .. } | AttemptControl::Wait { .. } => {
                    let event = completion_control_event(
                        identity,
                        control,
                        completed_at,
                        &input,
                        &queue_item,
                        &attempt,
                        &completed_attempt,
                    )?;
                    events.push(append_runtime_event(&transaction, event)?.event);
                }
            }
        }
        let completed_queue = completed_queue_event(
            &identities[identities.len() - 1],
            &input,
            &queue_item,
            &attempt,
            &result,
        );
        events.push(append_runtime_event(&transaction, completed_queue)?.event);
        release_claim_in_transaction(&transaction, claim, "release completed attempt claim")?;
        transaction
            .commit()
            .map_err(|database| database_error("commit attempt completion", database))?;
        Ok(events)
    }
}

fn claimed_queue_event(
    identity: &RuntimeEventIdentity,
    input: &Event,
    queue_item: &QueueItem,
    attempt_number: u32,
    run_id: &RunId,
) -> Event {
    let mut payload = queue_item_payload(queue_item, QueueItemStatus::Claimed);
    payload.insert("attempt_number".to_owned(), Value::from(attempt_number));
    Event {
        id: identity.id().to_owned(),
        event_type: "runtime.queue_item.claimed".to_owned(),
        source: "zeta".to_owned(),
        payload,
        idempotency_key: Some(queue_item_attempt_idempotency_key(
            &queue_item.event_id,
            &queue_item.target_agent,
            QueueItemStatus::Claimed,
            attempt_number,
        )),
        caused_by: Some(queue_item.event_id.clone()),
        session_id: queue_item.session_id.as_ref().map(ToString::to_string),
        run_id: Some(run_id.to_string()),
        turn_id: input.turn_id.clone(),
        timestamp_ms: identity.timestamp_ms(),
        cursor: None,
    }
}

struct AttemptStartFields<'a> {
    attempt_id: &'a AttemptId,
    attempt_number: u32,
    run_id: &'a RunId,
    worker_name: &'a str,
    started_at: &'a str,
}

fn started_attempt_event(
    identity: &RuntimeEventIdentity,
    input: &Event,
    queue_item: &QueueItem,
    fields: &AttemptStartFields<'_>,
) -> Event {
    let mut payload = Map::new();
    payload.insert(
        "attempt_id".to_owned(),
        Value::String(fields.attempt_id.to_string()),
    );
    payload.insert(
        "queue_item_id".to_owned(),
        Value::String(queue_item.id.to_string()),
    );
    payload.insert(
        "event_id".to_owned(),
        Value::String(queue_item.event_id.clone()),
    );
    payload.insert(
        "attempt_number".to_owned(),
        Value::from(fields.attempt_number),
    );
    payload.insert(
        "target_agent".to_owned(),
        Value::String(queue_item.target_agent.clone()),
    );
    payload.insert("status".to_owned(), Value::String("running".to_owned()));
    payload.insert(
        "started_at".to_owned(),
        Value::String(fields.started_at.to_owned()),
    );
    payload.insert("finished_at".to_owned(), Value::Null);
    payload.insert("error".to_owned(), Value::Null);
    payload.insert(
        "session_id".to_owned(),
        queue_item
            .session_id
            .as_ref()
            .map(|id| Value::String(id.to_string()))
            .unwrap_or(Value::Null),
    );
    payload.insert(
        "run_id".to_owned(),
        Value::String(fields.run_id.to_string()),
    );
    payload.insert(
        "worker_name".to_owned(),
        Value::String(fields.worker_name.to_owned()),
    );
    if let Some(project_revision) = &queue_item.project_revision {
        payload.insert(
            "project_revision".to_owned(),
            Value::String(project_revision.clone()),
        );
    }
    Event {
        id: identity.id().to_owned(),
        event_type: "runtime.attempt.started".to_owned(),
        source: "zeta".to_owned(),
        payload,
        idempotency_key: Some(format!(
            "attempt:{}:{}:started",
            queue_item.id, fields.attempt_number
        )),
        caused_by: Some(queue_item.event_id.clone()),
        session_id: queue_item.session_id.as_ref().map(ToString::to_string),
        run_id: Some(fields.run_id.to_string()),
        turn_id: input.turn_id.clone(),
        timestamp_ms: identity.timestamp_ms(),
        cursor: None,
    }
}

fn completion_controls(
    completion: &AttemptCompletion,
) -> Result<(OffsetDateTime, Vec<AttemptControl>), DispatchError> {
    let completed_at = parse_completion_timestamp(&completion.finished_at, "finished_at")?;
    validate_optional_completion_string(&completion.metadata, "final_answer")?;
    validate_optional_completion_array(&completion.metadata, "tool_calls")?;
    if let Some(value) = completion.metadata.get("usage") {
        if !value.is_object() {
            return Err(invalid_completion("usage"));
        }
    }
    let mut controls = completion.controls.clone();
    let mut positions = HashSet::new();
    for control in &controls {
        let position = control.position();
        if !positions.insert(position) {
            return Err(invalid_completion("control.position"));
        }
        match control {
            AttemptControl::Publish {
                handle,
                event_type,
                payload: _payload,
                at,
                position: _position,
            } => {
                validate_completion_control_string(handle, "publish_event_requests.handle")?;
                validate_completion_control_string(
                    event_type,
                    "publish_event_requests.event_type",
                )?;
                if let Some(at) = at {
                    validate_completion_control_string(at, "publish_event_requests.at")?;
                    parse_completion_timestamp(at, "publish_event_requests.at")?;
                }
            }
            AttemptControl::Wait {
                handle,
                event_type,
                fields: _fields,
                deadline,
                position: _position,
            } => {
                validate_completion_control_string(handle, "wait_requests.handle")?;
                validate_completion_control_string(event_type, "wait_requests.event_type")?;
                if let Some(deadline) = deadline {
                    validate_completion_control_string(deadline, "wait_requests.deadline")?;
                    parse_completion_timestamp(deadline, "wait_requests.deadline")?;
                }
            }
            AttemptControl::Cancel {
                handle,
                reason,
                source_agent_id,
                source_session_id,
                position: _position,
            } => {
                validate_completion_control_string(handle, "cancel_requests.handle")?;
                if let Some(reason) = reason {
                    validate_completion_control_string(reason, "cancel_requests.reason")?;
                }
                validate_completion_control_string(
                    source_agent_id,
                    "cancel_requests.source_agent_id",
                )?;
                validate_completion_control_string(
                    source_session_id,
                    "cancel_requests.source_session_id",
                )?;
            }
        }
    }
    controls.sort_by_key(AttemptControl::position);
    Ok((completed_at, controls))
}

fn completion_result(
    completion: &AttemptCompletion,
    controls: &[AttemptControl],
) -> Result<Map<String, Value>, DispatchError> {
    for field in [
        "publish_event_requests",
        "wait_requests",
        "cancel_requests",
        "content_promotions",
    ] {
        if completion.metadata.contains_key(field) {
            return Err(invalid_completion(field));
        }
    }

    let mut publish_requests = Vec::new();
    let mut wait_requests = Vec::new();
    let mut cancel_requests = Vec::new();
    for control in controls {
        match control {
            AttemptControl::Publish {
                handle,
                event_type,
                payload,
                at,
                position,
            } => {
                publish_requests.push(json_object([
                    ("handle", Value::String(handle.clone())),
                    ("event_type", Value::String(event_type.clone())),
                    ("payload", Value::Object(payload.clone())),
                    (
                        "at",
                        at.as_ref()
                            .map(|value| Value::String(value.clone()))
                            .unwrap_or(Value::Null),
                    ),
                    ("position", Value::from(*position)),
                ]));
            }
            AttemptControl::Wait {
                handle,
                event_type,
                fields,
                deadline,
                position,
            } => {
                wait_requests.push(json_object([
                    ("handle", Value::String(handle.clone())),
                    ("event_type", Value::String(event_type.clone())),
                    ("fields", Value::Object(fields.clone())),
                    (
                        "deadline",
                        deadline
                            .as_ref()
                            .map(|value| Value::String(value.clone()))
                            .unwrap_or(Value::Null),
                    ),
                    ("position", Value::from(*position)),
                ]));
            }
            AttemptControl::Cancel {
                handle,
                reason,
                source_agent_id,
                source_session_id,
                position,
            } => {
                cancel_requests.push(json_object([
                    ("handle", Value::String(handle.clone())),
                    (
                        "reason",
                        reason
                            .as_ref()
                            .map(|value| Value::String(value.clone()))
                            .unwrap_or(Value::Null),
                    ),
                    ("source_agent_id", Value::String(source_agent_id.clone())),
                    (
                        "source_session_id",
                        Value::String(source_session_id.clone()),
                    ),
                    ("position", Value::from(*position)),
                ]));
            }
        }
    }

    let mut result = completion.metadata.clone();
    if !publish_requests.is_empty() {
        result.insert(
            "publish_event_requests".to_owned(),
            Value::Array(publish_requests),
        );
    }
    if !wait_requests.is_empty() {
        result.insert("wait_requests".to_owned(), Value::Array(wait_requests));
    }
    if !cancel_requests.is_empty() {
        result.insert("cancel_requests".to_owned(), Value::Array(cancel_requests));
    }
    Ok(result)
}

fn json_object<const N: usize>(fields: [(&str, Value); N]) -> Value {
    let mut object = Map::new();
    for (key, value) in fields {
        object.insert(key.to_owned(), value);
    }
    Value::Object(object)
}

fn validate_optional_completion_array(
    result: &Map<String, Value>,
    field: &'static str,
) -> Result<(), DispatchError> {
    match result.get(field) {
        Some(Value::Array(_)) | None => Ok(()),
        Some(_value) => Err(invalid_completion(field)),
    }
}

fn validate_optional_completion_string(
    result: &Map<String, Value>,
    field: &'static str,
) -> Result<(), DispatchError> {
    match result.get(field) {
        Some(Value::String(_)) | None => Ok(()),
        Some(_value) => Err(invalid_completion(field)),
    }
}

fn validate_completion_control_string(
    value: &str,
    field: &'static str,
) -> Result<(), DispatchError> {
    if value.is_empty() {
        return Err(invalid_completion(field));
    }
    Ok(())
}

fn parse_completion_timestamp(
    value: &str,
    field: &'static str,
) -> Result<OffsetDateTime, DispatchError> {
    OffsetDateTime::parse(value, &Rfc3339).map_err(|_error| invalid_completion(field))
}

fn invalid_completion(field: &'static str) -> DispatchError {
    DispatchError::InvalidCompletion { field }
}

fn completed_attempt_event(
    identity: &RuntimeEventIdentity,
    input: &Event,
    attempt: &Attempt,
    completion: &AttemptCompletion,
    result: &Map<String, Value>,
) -> Event {
    let mut payload = attempt_payload(attempt, AttemptStatus::Completed);
    payload.insert(
        "finished_at".to_owned(),
        Value::String(completion.finished_at.clone()),
    );
    payload.insert("result".to_owned(), Value::Object(result.clone()));
    let summary = result.get("summary").or_else(|| result.get("final_answer"));
    if let Some(Value::String(summary)) = summary {
        payload.insert("summary".to_owned(), Value::String(summary.clone()));
    }
    for key in ["tool_calls", "usage"] {
        if let Some(value) = result.get(key) {
            payload.insert(key.to_owned(), value.clone());
        }
    }
    Event {
        id: identity.id().to_owned(),
        event_type: "runtime.attempt.completed".to_owned(),
        source: "zeta".to_owned(),
        payload,
        idempotency_key: Some(crate::identity::attempt_idempotency_key(
            &attempt.queue_item_id,
            attempt.attempt_number,
            AttemptStatus::Completed,
        )),
        caused_by: Some(input.id.clone()),
        session_id: attempt.session_id.as_ref().map(ToString::to_string),
        run_id: attempt.run_id.as_ref().map(ToString::to_string),
        turn_id: input.turn_id.clone(),
        timestamp_ms: identity.timestamp_ms(),
        cursor: None,
    }
}

fn completion_control_event(
    identity: &RuntimeEventIdentity,
    control: &AttemptControl,
    completed_at: OffsetDateTime,
    input: &Event,
    queue_item: &QueueItem,
    attempt: &Attempt,
    completed_attempt: &Event,
) -> Result<Event, DispatchError> {
    let session_id = attempt.session_id.as_ref().map(ToString::to_string);
    let run_id = attempt.run_id.as_ref().map(ToString::to_string);
    match control {
        AttemptControl::Publish {
            position,
            handle,
            event_type,
            payload,
            at,
        } => {
            let immediate = match at {
                Some(at) => {
                    parse_completion_timestamp(at, "publish_event_requests.at")? <= completed_at
                }
                None => true,
            };
            if immediate {
                return Ok(Event {
                    id: identity.id().to_owned(),
                    event_type: event_type.clone(),
                    source: format!("agent:{}", attempt.target_agent),
                    payload: payload.clone(),
                    idempotency_key: Some(format!("agent.publish:{}:{position}", queue_item.id)),
                    caused_by: Some(completed_attempt.id.clone()),
                    session_id,
                    run_id,
                    turn_id: input.turn_id.clone(),
                    timestamp_ms: identity.timestamp_ms(),
                    cursor: None,
                });
            }
            let mut deferred = Map::new();
            deferred.insert("handle".to_owned(), Value::String(handle.clone()));
            deferred.insert("event_type".to_owned(), Value::String(event_type.clone()));
            deferred.insert("payload".to_owned(), Value::Object(payload.clone()));
            deferred.insert(
                "publish_at".to_owned(),
                at.as_ref()
                    .map(|at| Value::String(at.clone()))
                    .unwrap_or(Value::Null),
            );
            deferred.insert(
                "source_agent_id".to_owned(),
                Value::String(attempt.target_agent.clone()),
            );
            deferred.insert(
                "source_session_id".to_owned(),
                session_id.clone().map(Value::String).unwrap_or(Value::Null),
            );
            deferred.insert(
                "source_queue_item_id".to_owned(),
                Value::String(queue_item.id.to_string()),
            );
            deferred.insert("position".to_owned(), Value::from(*position));
            Ok(Event {
                id: identity.id().to_owned(),
                event_type: "runtime.deferred_publication.created".to_owned(),
                source: "zeta".to_owned(),
                payload: deferred,
                idempotency_key: Some(format!("agent.defer:{}:{position}", queue_item.id)),
                caused_by: Some(completed_attempt.id.clone()),
                session_id,
                run_id,
                turn_id: input.turn_id.clone(),
                timestamp_ms: identity.timestamp_ms(),
                cursor: None,
            })
        }
        AttemptControl::Wait {
            position,
            handle,
            event_type,
            fields,
            deadline,
        } => {
            let mut wait = Map::new();
            wait.insert("handle".to_owned(), Value::String(handle.clone()));
            wait.insert(
                "agent_id".to_owned(),
                Value::String(attempt.target_agent.clone()),
            );
            wait.insert(
                "session_id".to_owned(),
                session_id.clone().map(Value::String).unwrap_or(Value::Null),
            );
            wait.insert("event_type".to_owned(), Value::String(event_type.clone()));
            wait.insert("fields".to_owned(), Value::Object(fields.clone()));
            wait.insert(
                "deadline".to_owned(),
                deadline
                    .as_ref()
                    .map(|value| Value::String(value.clone()))
                    .unwrap_or(Value::Null),
            );
            wait.insert(
                "source_queue_item_id".to_owned(),
                Value::String(queue_item.id.to_string()),
            );
            wait.insert(
                "project_revision".to_owned(),
                queue_item
                    .project_revision
                    .as_ref()
                    .map(|value| Value::String(value.clone()))
                    .unwrap_or(Value::Null),
            );
            Ok(Event {
                id: identity.id().to_owned(),
                event_type: "runtime.wait.created".to_owned(),
                source: "zeta".to_owned(),
                payload: wait,
                idempotency_key: Some(format!("agent.wait:{}:{position}", queue_item.id)),
                caused_by: Some(completed_attempt.id.clone()),
                session_id,
                run_id,
                turn_id: input.turn_id.clone(),
                timestamp_ms: identity.timestamp_ms(),
                cursor: None,
            })
        }
        AttemptControl::Cancel { .. } => Err(invalid_completion("control")),
    }
}

fn completed_queue_event(
    identity: &RuntimeEventIdentity,
    input: &Event,
    queue_item: &QueueItem,
    attempt: &Attempt,
    result: &Map<String, Value>,
) -> Event {
    let mut payload = queue_item_payload(queue_item, QueueItemStatus::Completed);
    payload.insert("result".to_owned(), Value::Object(result.clone()));
    Event {
        id: identity.id().to_owned(),
        event_type: "runtime.queue_item.completed".to_owned(),
        source: "zeta".to_owned(),
        payload,
        idempotency_key: Some(queue_item_idempotency_key(
            &queue_item.event_id,
            &queue_item.target_agent,
            QueueItemStatus::Completed,
        )),
        caused_by: Some(input.id.clone()),
        session_id: queue_item.session_id.as_ref().map(ToString::to_string),
        run_id: attempt.run_id.as_ref().map(ToString::to_string),
        turn_id: input.turn_id.clone(),
        timestamp_ms: identity.timestamp_ms(),
        cursor: None,
    }
}

struct AttemptFailureFields<'a> {
    finished_at: &'a str,
    error: &'a str,
    error_code: AttemptFailureCode,
    retry_policy: RetryPolicy,
    now_ms: i64,
}

fn failed_attempt_event(
    identity: &RuntimeEventIdentity,
    input: &Event,
    attempt: &Attempt,
    failure: &AttemptFailureFields<'_>,
) -> Event {
    let mut payload = attempt_payload(attempt, AttemptStatus::Failed);
    payload.insert(
        "finished_at".to_owned(),
        Value::String(failure.finished_at.to_owned()),
    );
    payload.insert("error".to_owned(), Value::String(failure.error.to_owned()));
    payload.insert(
        "error_code".to_owned(),
        Value::String(failure.error_code.to_string()),
    );
    Event {
        id: identity.id().to_owned(),
        event_type: "runtime.attempt.failed".to_owned(),
        source: "zeta".to_owned(),
        payload,
        idempotency_key: Some(crate::identity::attempt_idempotency_key(
            &attempt.queue_item_id,
            attempt.attempt_number,
            AttemptStatus::Failed,
        )),
        caused_by: Some(input.id.clone()),
        session_id: attempt.session_id.as_ref().map(ToString::to_string),
        run_id: attempt.run_id.as_ref().map(ToString::to_string),
        turn_id: input.turn_id.clone(),
        timestamp_ms: identity.timestamp_ms(),
        cursor: None,
    }
}

fn failed_queue_disposition_event(
    identity: &RuntimeEventIdentity,
    input: &Event,
    queue_item: &QueueItem,
    attempt: &Attempt,
    failure: &AttemptFailureFields<'_>,
) -> Result<Event, DispatchError> {
    let failure_class = classify_attempt_failure_code(failure.error_code);
    let retry = failure_class == FailureClass::Retryable
        && failure
            .retry_policy
            .permits_retry_after(attempt.attempt_number);
    let (event_type, idempotency_key, payload) = if retry {
        let delay_ms = failure
            .retry_policy
            .delay_ms(attempt.attempt_number)
            .map_err(|_error| DispatchError::InvalidCoordinationInput {
                field: "retry_policy",
            })?;
        let delay_ms =
            i64::try_from(delay_ms).map_err(|_error| DispatchError::InvalidCoordinationInput {
                field: "retry_policy",
            })?;
        let not_before = failure.now_ms.checked_add(delay_ms).ok_or(
            DispatchError::InvalidCoordinationInput {
                field: "not_before",
            },
        )?;
        let next_attempt = attempt.attempt_number.checked_add(1).ok_or(
            DispatchError::InvalidCoordinationInput {
                field: "attempt_number",
            },
        )?;
        let mut payload = queue_item_payload(queue_item, QueueItemStatus::Available);
        payload.insert("not_before".to_owned(), Value::from(not_before));
        (
            "runtime.queue_item.available",
            queue_item_attempt_idempotency_key(
                &queue_item.event_id,
                &queue_item.target_agent,
                QueueItemStatus::Available,
                next_attempt,
            ),
            payload,
        )
    } else {
        let reason = if failure_class == FailureClass::Permanent {
            "permanent"
        } else {
            "exhausted"
        };
        let mut payload = queue_item_payload(queue_item, QueueItemStatus::DeadLettered);
        payload.insert("reason".to_owned(), Value::String(reason.to_owned()));
        payload.insert(
            "attempt_count".to_owned(),
            Value::from(attempt.attempt_number),
        );
        payload.insert(
            "last_attempt_id".to_owned(),
            Value::String(attempt.id.to_string()),
        );
        payload.insert(
            "dead_lettered_at".to_owned(),
            Value::String(failure.finished_at.to_owned()),
        );
        let mut last_error = Map::new();
        last_error.insert(
            "code".to_owned(),
            Value::String(failure.error_code.to_string()),
        );
        last_error.insert(
            "message".to_owned(),
            Value::String(failure.error.to_owned()),
        );
        payload.insert("last_error".to_owned(), Value::Object(last_error));
        (
            "runtime.queue_item.dead_lettered",
            queue_item_attempt_idempotency_key(
                &queue_item.event_id,
                &queue_item.target_agent,
                QueueItemStatus::DeadLettered,
                attempt.attempt_number,
            ),
            payload,
        )
    };
    Ok(Event {
        id: identity.id().to_owned(),
        event_type: event_type.to_owned(),
        source: "zeta".to_owned(),
        payload,
        idempotency_key: Some(idempotency_key),
        caused_by: Some(input.id.clone()),
        session_id: queue_item.session_id.as_ref().map(ToString::to_string),
        run_id: attempt.run_id.as_ref().map(ToString::to_string),
        turn_id: input.turn_id.clone(),
        timestamp_ms: identity.timestamp_ms(),
        cursor: None,
    })
}

pub(super) fn attempt_payload(attempt: &Attempt, status: AttemptStatus) -> Map<String, Value> {
    let mut payload = Map::new();
    payload.insert(
        "attempt_id".to_owned(),
        Value::String(attempt.id.to_string()),
    );
    payload.insert(
        "queue_item_id".to_owned(),
        Value::String(attempt.queue_item_id.to_string()),
    );
    payload.insert(
        "event_id".to_owned(),
        Value::String(attempt.event_id.clone()),
    );
    payload.insert(
        "attempt_number".to_owned(),
        Value::from(attempt.attempt_number),
    );
    payload.insert(
        "target_agent".to_owned(),
        Value::String(attempt.target_agent.clone()),
    );
    payload.insert("status".to_owned(), Value::String(status.to_string()));
    payload.insert(
        "started_at".to_owned(),
        Value::String(attempt.started_at.clone()),
    );
    payload.insert("finished_at".to_owned(), Value::Null);
    payload.insert("error".to_owned(), Value::Null);
    payload.insert(
        "session_id".to_owned(),
        attempt
            .session_id
            .as_ref()
            .map(|id| Value::String(id.to_string()))
            .unwrap_or(Value::Null),
    );
    payload.insert(
        "run_id".to_owned(),
        attempt
            .run_id
            .as_ref()
            .map(|id| Value::String(id.to_string()))
            .unwrap_or(Value::Null),
    );
    if let Some(worker_name) = &attempt.worker_name {
        payload.insert("worker_name".to_owned(), Value::String(worker_name.clone()));
    }
    if let Some(project_revision) = &attempt.project_revision {
        payload.insert(
            "project_revision".to_owned(),
            Value::String(project_revision.clone()),
        );
    }
    payload
}

pub(super) fn index_attempt(connection: &Connection, event: &Event) -> Result<(), DispatchError> {
    let attempt_id = required_runtime_id(event, "attempt_id")?;
    let attempt_id = AttemptId::from_str(&attempt_id).map_err(|_error| {
        DispatchError::InvalidLifecycleEvent {
            event_id: event.id.clone(),
            field: "attempt_id",
        }
    })?;
    let queue_item_id = required_runtime_id(event, "queue_item_id")?;
    let queue_item_id = QueueItemId::from_str(&queue_item_id).map_err(|_error| {
        DispatchError::InvalidLifecycleEvent {
            event_id: event.id.clone(),
            field: "queue_item_id",
        }
    })?;
    let input_event_id = required_runtime_id(event, "event_id")?;
    let attempt_number = required_positive_u32(event, "attempt_number")?;
    let target_agent = required_runtime_id(event, "target_agent")?;
    let status = lifecycle_attempt_status(event)?;
    let supplied_started_at = optional_payload_string(event, "started_at")?;
    let supplied_session_id =
        optional_payload_string(event, "session_id")?.or_else(|| event.session_id.clone());
    validate_optional_runtime_id(event, "session_id", supplied_session_id.as_deref())?;
    let supplied_run_id =
        optional_payload_string(event, "run_id")?.or_else(|| event.run_id.clone());
    validate_optional_runtime_id(event, "run_id", supplied_run_id.as_deref())?;
    let supplied_project_revision = optional_payload_string(event, "project_revision")?;
    validate_optional_runtime_id(
        event,
        "project_revision",
        supplied_project_revision.as_deref(),
    )?;
    let previous = connection
        .query_row(
            "SELECT queue_item_id, event_id, attempt_number, target_agent,
                    status, started_at, session_id, run_id, project_revision
             FROM attempts WHERE attempt_id = ?1",
            params![attempt_id.as_str()],
            |row| {
                Ok(StoredAttemptIdentity {
                    queue_item_id: row.get(0)?,
                    event_id: row.get(1)?,
                    attempt_number: row.get(2)?,
                    target_agent: row.get(3)?,
                    status: row.get(4)?,
                    started_at: row.get(5)?,
                    session_id: row.get(6)?,
                    run_id: row.get(7)?,
                    project_revision: row.get(8)?,
                })
            },
        )
        .optional()
        .map_err(|error| database_error("read attempt transition", error))?;
    let (previous_status, started_at, session_id, run_id, project_revision) = match previous {
        Some(previous) => {
            if previous.queue_item_id != queue_item_id.as_str()
                || previous.event_id != input_event_id
                || previous.attempt_number != i64::from(attempt_number)
                || previous.target_agent != target_agent
            {
                return Err(DispatchError::InvalidLifecycleEvent {
                    event_id: event.id.clone(),
                    field: "attempt_identity",
                });
            }
            if supplied_started_at
                .as_deref()
                .is_some_and(|value| value != previous.started_at)
                || supplied_session_id
                    .as_deref()
                    .is_some_and(|value| Some(value) != previous.session_id.as_deref())
                || supplied_run_id
                    .as_deref()
                    .is_some_and(|value| Some(value) != previous.run_id.as_deref())
                || supplied_project_revision
                    .as_deref()
                    .is_some_and(|value| Some(value) != previous.project_revision.as_deref())
            {
                return Err(DispatchError::InvalidLifecycleEvent {
                    event_id: event.id.clone(),
                    field: "attempt_identity",
                });
            }
            let previous_status = AttemptStatus::from_str(&previous.status).map_err(|_error| {
                DispatchError::CorruptProjection {
                    table: "attempts",
                    field: "status",
                }
            })?;
            (
                Some(previous_status),
                previous.started_at,
                previous.session_id,
                previous.run_id,
                previous.project_revision,
            )
        }
        None => {
            let Some(started_at) = supplied_started_at else {
                return Err(DispatchError::InvalidLifecycleEvent {
                    event_id: event.id.clone(),
                    field: "started_at",
                });
            };
            (
                None,
                started_at,
                supplied_session_id,
                supplied_run_id,
                supplied_project_revision,
            )
        }
    };
    AttemptStatus::validate_transition(previous_status, status)?;

    let worker_name = optional_payload_string(event, "worker_name")?;
    let finished_at = optional_payload_string(event, "finished_at")?;
    let error = optional_payload_string(event, "error")?;
    let claim_token = if status == AttemptStatus::Running {
        match &worker_name {
            Some(worker_name) => connection
                .query_row(
                    "SELECT claim_token FROM queue_claims
                     WHERE queue_item_id = ?1 AND worker_name = ?2",
                    params![queue_item_id.as_str(), worker_name],
                    |row| row.get::<_, String>(0),
                )
                .optional()
                .map_err(|database| database_error("resolve attempt claim", database))?,
            None => None,
        }
    } else {
        None
    };
    connection
        .execute(
            "INSERT INTO attempts (
                attempt_id, queue_item_id, event_id, attempt_number,
                target_agent, worker_name, claim_token, status, started_at,
                heartbeat_at, finished_at, error, session_id, run_id,
                project_revision
             ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15)
             ON CONFLICT(attempt_id) DO UPDATE SET
                claim_token = CASE
                    WHEN excluded.status = 'running'
                    THEN COALESCE(attempts.claim_token, excluded.claim_token)
                    ELSE NULL
                END,
                status = excluded.status,
                heartbeat_at = CASE
                    WHEN excluded.status = 'running' THEN excluded.heartbeat_at
                    ELSE NULL
                END,
                finished_at = excluded.finished_at,
                error = excluded.error,
                session_id = excluded.session_id,
                run_id = excluded.run_id,
                project_revision = COALESCE(
                    excluded.project_revision,
                    attempts.project_revision
                )",
            params![
                attempt_id.as_str(),
                queue_item_id.as_str(),
                input_event_id,
                i64::from(attempt_number),
                target_agent,
                worker_name,
                claim_token,
                status.to_string(),
                started_at,
                event.timestamp_ms,
                finished_at,
                error,
                session_id,
                run_id,
                project_revision,
            ],
        )
        .map_err(|error| database_error("project attempt lifecycle", error))?;
    if status == AttemptStatus::Running {
        connection
            .execute(
                "UPDATE queue_items
                 SET attempt_count = MAX(attempt_count, ?1)
                 WHERE queue_item_id = ?2",
                params![i64::from(attempt_number), queue_item_id.as_str()],
            )
            .map_err(|error| database_error("project attempt count", error))?;
    }
    Ok(())
}

struct StoredAttemptIdentity {
    queue_item_id: String,
    event_id: String,
    attempt_number: i64,
    target_agent: String,
    status: String,
    started_at: String,
    session_id: Option<String>,
    run_id: Option<String>,
    project_revision: Option<String>,
}

fn lifecycle_attempt_status(event: &Event) -> Result<AttemptStatus, DispatchError> {
    let suffix = event.event_type.rsplit('.').next().unwrap_or_default();
    let expected = if suffix == "started" {
        AttemptStatus::Running
    } else {
        AttemptStatus::from_str(suffix).map_err(|_error| DispatchError::InvalidLifecycleEvent {
            event_id: event.id.clone(),
            field: "event_type",
        })?
    };
    let actual = match event.payload.get("status") {
        Some(Value::String(status)) => AttemptStatus::from_str(status).map_err(|_error| {
            DispatchError::InvalidLifecycleEvent {
                event_id: event.id.clone(),
                field: "status",
            }
        })?,
        Some(_value) => {
            return Err(DispatchError::InvalidLifecycleEvent {
                event_id: event.id.clone(),
                field: "status",
            });
        }
        None => expected,
    };
    if actual != expected {
        return Err(DispatchError::InvalidLifecycleEvent {
            event_id: event.id.clone(),
            field: "status",
        });
    }
    Ok(actual)
}

fn required_positive_u32(event: &Event, field: &'static str) -> Result<u32, DispatchError> {
    let Some(Value::Number(number)) = event.payload.get(field) else {
        return Err(DispatchError::InvalidLifecycleEvent {
            event_id: event.id.clone(),
            field,
        });
    };
    let Some(value) = number.as_u64() else {
        return Err(DispatchError::InvalidLifecycleEvent {
            event_id: event.id.clone(),
            field,
        });
    };
    if value == 0 || value > u64::from(u32::MAX) {
        return Err(DispatchError::InvalidLifecycleEvent {
            event_id: event.id.clone(),
            field,
        });
    }
    Ok(value as u32)
}

const ATTEMPT_COLUMNS: &str = "attempt.attempt_id, attempt.queue_item_id,
    attempt.event_id, attempt.attempt_number, attempt.target_agent,
    attempt.worker_name, attempt.status, attempt.started_at,
    attempt.finished_at, attempt.error, attempt.session_id, attempt.run_id,
    attempt.project_revision";

pub(super) fn load_attempts(connection: &Connection) -> Result<Vec<Attempt>, DispatchError> {
    let sql = format!(
        "SELECT {ATTEMPT_COLUMNS}
         FROM attempts AS attempt
         JOIN queue_items AS queue
           ON queue.queue_item_id = attempt.queue_item_id
         ORDER BY queue.input_cursor ASC, attempt.attempt_number ASC,
                  attempt.attempt_id ASC"
    );
    let mut statement = connection
        .prepare(&sql)
        .map_err(|error| database_error("prepare attempt read", error))?;
    let rows = statement
        .query_map([], StoredAttempt::from_row)
        .map_err(|error| database_error("read attempts", error))?;
    let mut attempts = Vec::new();
    for row in rows {
        let stored = row.map_err(|error| database_error("read attempt", error))?;
        attempts.push(stored.into_model()?);
    }
    Ok(attempts)
}

fn load_running_attempt_for_claim(
    connection: &Connection,
    claim: &QueueClaim,
) -> Result<Option<Attempt>, DispatchError> {
    let sql = format!(
        "SELECT {ATTEMPT_COLUMNS}
         FROM attempts AS attempt
         WHERE attempt.queue_item_id = ?1
           AND attempt.worker_name = ?2
           AND attempt.claim_token = ?3
           AND attempt.status = 'running'
         ORDER BY attempt.attempt_number DESC
         LIMIT 1"
    );
    let stored = connection
        .query_row(
            &sql,
            params![
                claim.queue_item_id.as_str(),
                &claim.worker_name,
                claim.token.as_str(),
            ],
            StoredAttempt::from_row,
        )
        .optional()
        .map_err(|database| database_error("read running attempt", database))?;
    stored.map(StoredAttempt::into_model).transpose()
}

pub(super) fn load_latest_running_attempt(
    connection: &Connection,
    queue_item_id: &QueueItemId,
) -> Result<Option<Attempt>, DispatchError> {
    let sql = format!(
        "SELECT {ATTEMPT_COLUMNS}
         FROM attempts AS attempt
         WHERE attempt.queue_item_id = ?1
           AND attempt.status = 'running'
         ORDER BY attempt.attempt_number DESC
         LIMIT 1"
    );
    let stored = connection
        .query_row(
            &sql,
            params![queue_item_id.as_str()],
            StoredAttempt::from_row,
        )
        .optional()
        .map_err(|database| database_error("read latest running attempt", database))?;
    stored.map(StoredAttempt::into_model).transpose()
}

struct StoredAttempt {
    attempt_id: String,
    queue_item_id: String,
    event_id: String,
    attempt_number: i64,
    target_agent: String,
    worker_name: Option<String>,
    status: String,
    started_at: String,
    finished_at: Option<String>,
    error: Option<String>,
    session_id: Option<String>,
    run_id: Option<String>,
    project_revision: Option<String>,
}

impl StoredAttempt {
    fn from_row(row: &Row<'_>) -> rusqlite::Result<Self> {
        Ok(StoredAttempt {
            attempt_id: row.get(0)?,
            queue_item_id: row.get(1)?,
            event_id: row.get(2)?,
            attempt_number: row.get(3)?,
            target_agent: row.get(4)?,
            worker_name: row.get(5)?,
            status: row.get(6)?,
            started_at: row.get(7)?,
            finished_at: row.get(8)?,
            error: row.get(9)?,
            session_id: row.get(10)?,
            run_id: row.get(11)?,
            project_revision: row.get(12)?,
        })
    }

    fn into_model(self) -> Result<Attempt, DispatchError> {
        let id = AttemptId::from_str(&self.attempt_id)
            .map_err(|_error| corrupt_projection("attempts", "attempt_id"))?;
        let queue_item_id = QueueItemId::from_str(&self.queue_item_id)
            .map_err(|_error| corrupt_projection("attempts", "queue_item_id"))?;
        let attempt_number =
            nonnegative_u32_projection(self.attempt_number, "attempts", "attempt_number")?;
        if attempt_number == 0 {
            return Err(corrupt_projection("attempts", "attempt_number"));
        }
        let status = AttemptStatus::from_str(&self.status)
            .map_err(|_error| corrupt_projection("attempts", "status"))?;
        let session_id = self
            .session_id
            .map(|id| SessionId::from_str(&id))
            .transpose()
            .map_err(|_error| corrupt_projection("attempts", "session_id"))?;
        let run_id = self
            .run_id
            .map(|id| RunId::from_str(&id))
            .transpose()
            .map_err(|_error| corrupt_projection("attempts", "run_id"))?;
        Ok(Attempt {
            id,
            queue_item_id,
            event_id: self.event_id,
            attempt_number,
            target_agent: self.target_agent,
            worker_name: self.worker_name,
            status,
            started_at: self.started_at,
            finished_at: self.finished_at,
            error: self.error,
            session_id,
            run_id,
            project_revision: self.project_revision,
        })
    }
}
