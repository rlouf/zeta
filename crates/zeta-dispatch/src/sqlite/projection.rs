use std::collections::HashSet;
use std::str::FromStr;

use rusqlite::{params, Connection, OptionalExtension, Row, TransactionBehavior};
use serde_json::{Map, Value};
use time::{format_description::well_known::Rfc3339, OffsetDateTime};
use zeta_journal::{verify, Event, HeadExpectation};

use super::journal::load_entries;
use super::{
    corrupt_projection, database_error, nonnegative_u32_projection, positive_u64_projection,
    Dispatch, DispatchError, CREATE_PROJECTIONS, DROP_PROJECTIONS, PROJECTION_EPOCH,
};
use crate::dispatch::{
    DeferredPublicationStatus, EffectDeliverySemantics, EffectStatus, QueueItem, WaitStatus,
};
use crate::identity::{pending_queue_item_id, AttemptId, QueueItemId, RunId, SessionId};
use crate::state::{AttemptStatus, QueueItemStatus};

impl Dispatch {
    /// Returns one durable queue-item read model.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when the projection row cannot be read or
    /// rehydrated as the public typed model.
    pub fn queue_item(&self, id: &QueueItemId) -> Result<Option<QueueItem>, DispatchError> {
        load_queue_item(&self.connection, id.as_str())
    }

    /// Returns all queue items in input-cursor and identity order.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when a projection row cannot be read or
    /// rehydrated as the public typed model.
    pub fn list_queue_items(&self) -> Result<Vec<QueueItem>, DispatchError> {
        load_queue_items(&self.connection)
    }

    /// Rebuilds every event-sourced projection from the ordered journal.
    ///
    /// Live claims and locks are coordination state, so rebuild discards them.
    /// A replayed claimed item becomes pending when still unbound and available
    /// when already bound. Historical running attempts remain running.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when schema reset, journal decoding, lifecycle
    /// validation, or replay fails.
    pub fn rebuild_projections(&mut self) -> Result<usize, DispatchError> {
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|error| database_error("begin projection rebuild", error))?;
        let replayed = rebuild_projections_in_transaction(&transaction)?;
        transaction
            .execute(
                "UPDATE dispatch_schema SET projection_epoch = ?1 WHERE singleton = 1",
                params![PROJECTION_EPOCH],
            )
            .map_err(|error| database_error("record projection epoch", error))?;
        transaction
            .commit()
            .map_err(|error| database_error("commit projection rebuild", error))?;
        Ok(replayed)
    }
}

pub(super) fn index_event(connection: &Connection, event: &Event) -> Result<(), DispatchError> {
    if event.event_type.starts_with("runtime.effect.") {
        return index_effect(connection, event);
    }
    if event.event_type.starts_with("runtime.wait.") {
        return index_wait(connection, event);
    }
    if event
        .event_type
        .starts_with("runtime.deferred_publication.")
    {
        return index_deferred_publication(connection, event);
    }
    if event.event_type == "runtime.queue_item.cancel_requested" {
        return index_queue_item_cancel_requested(connection, event);
    }
    if event.event_type.starts_with("runtime.queue_item.") {
        return index_queue_item(connection, event);
    }
    if event.event_type.starts_with("runtime.attempt.") {
        return index_attempt(connection, event);
    }
    if is_queueable_event(event) {
        index_pending_queue_item(connection, event)?;
    }
    Ok(())
}

fn index_effect(connection: &Connection, event: &Event) -> Result<(), DispatchError> {
    let status = lifecycle_effect_status(event)?;
    let key = required_runtime_id(event, "effect_key")?;
    let operation = required_runtime_id(event, "operation")?;
    let semantics = required_runtime_id(event, "semantics")?;
    let semantics = parse_effect_semantics(event, &semantics)?;
    let scope = required_runtime_id(event, "scope")?;
    let queue_item_id = optional_payload_string(event, "queue_item_id")?;
    if let Some(queue_item_id) = &queue_item_id {
        QueueItemId::from_str(queue_item_id)
            .map_err(|_error| invalid_lifecycle(event, "queue_item_id"))?;
    }
    let params = required_payload_object(event, "params")?;
    let params_json =
        serde_json::to_string(&params).map_err(|_error| invalid_lifecycle(event, "params"))?;
    let result = optional_payload_object(event, "result")?;
    let caused_by = event
        .caused_by
        .as_deref()
        .filter(|caused_by| !caused_by.is_empty())
        .ok_or_else(|| invalid_lifecycle(event, "caused_by"))?;
    let expected_idempotency_key = format!("runtime.effect.{}:{key}", effect_status_str(status));
    if event.idempotency_key.as_deref() != Some(expected_idempotency_key.as_str()) {
        return Err(invalid_lifecycle(event, "idempotency_key"));
    }
    validate_effect_result(event, status, semantics, result.as_ref())?;

    if status == EffectStatus::Planned {
        connection
            .execute(
                "INSERT INTO effects (
                    effect_key, operation, semantics, scope, queue_item_id,
                    params_json, status, result_json, caused_by,
                    planned_event_id, terminal_event_id, updated_at
                 ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, 'planned', NULL,
                           ?7, ?8, NULL, ?9)",
                params![
                    key,
                    operation,
                    effect_semantics_str(semantics),
                    scope,
                    queue_item_id,
                    params_json,
                    caused_by,
                    &event.id,
                    event.timestamp_ms,
                ],
            )
            .map_err(|database| database_error("project effect planning", database))?;
        return Ok(());
    }

    let stored = connection
        .query_row(
            "SELECT operation, semantics, scope, queue_item_id,
                    params_json, status, caused_by
             FROM effects WHERE effect_key = ?1",
            params![key],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, Option<String>>(3)?,
                    row.get::<_, String>(4)?,
                    row.get::<_, String>(5)?,
                    row.get::<_, String>(6)?,
                ))
            },
        )
        .optional()
        .map_err(|database| database_error("read effect transition", database))?;
    let Some((
        stored_operation,
        stored_semantics,
        stored_scope,
        stored_queue_item_id,
        stored_params_json,
        stored_status,
        stored_caused_by,
    )) = stored
    else {
        return Err(invalid_lifecycle(event, "effect_key"));
    };
    if stored_operation != operation
        || stored_semantics != effect_semantics_str(semantics)
        || stored_scope != scope
        || stored_queue_item_id != queue_item_id
        || stored_params_json != params_json
        || stored_caused_by != caused_by
    {
        return Err(invalid_lifecycle(event, "effect_identity"));
    }
    let previous = parse_effect_status(event, &stored_status)?;
    validate_effect_transition(event, previous, status)?;
    let result_json = result
        .map(|result| serde_json::to_string(&result))
        .transpose()
        .map_err(|_error| invalid_lifecycle(event, "result"))?;
    let terminal_event_id = effect_status_is_terminal(status).then_some(event.id.as_str());
    connection
        .execute(
            "UPDATE effects
             SET status = ?1, result_json = ?2,
                 terminal_event_id = ?3, updated_at = ?4
             WHERE effect_key = ?5",
            params![
                effect_status_str(status),
                result_json,
                terminal_event_id,
                event.timestamp_ms,
                key,
            ],
        )
        .map_err(|database| database_error("project effect transition", database))?;
    Ok(())
}

fn validate_effect_result(
    event: &Event,
    status: EffectStatus,
    semantics: EffectDeliverySemantics,
    result: Option<&Map<String, Value>>,
) -> Result<(), DispatchError> {
    if effect_status_is_terminal(status) != result.is_some() {
        return Err(invalid_lifecycle(event, "result"));
    }
    if status == EffectStatus::Ambiguous && semantics != EffectDeliverySemantics::UnsafeToRetry {
        return Err(invalid_lifecycle(event, "semantics"));
    }
    if status == EffectStatus::Failed && semantics == EffectDeliverySemantics::UnsafeToRetry {
        return Err(invalid_lifecycle(event, "status"));
    }
    Ok(())
}

fn validate_effect_transition(
    event: &Event,
    previous: EffectStatus,
    next: EffectStatus,
) -> Result<(), DispatchError> {
    let valid = matches!(
        (previous, next),
        (EffectStatus::Planned, EffectStatus::Started)
            | (EffectStatus::Started, EffectStatus::Completed)
            | (EffectStatus::Started, EffectStatus::Failed)
            | (EffectStatus::Started, EffectStatus::Ambiguous)
    );
    if !valid {
        return Err(invalid_lifecycle(event, "status"));
    }
    Ok(())
}

fn index_wait(connection: &Connection, event: &Event) -> Result<(), DispatchError> {
    match event.event_type.as_str() {
        "runtime.wait.created" => index_wait_created(connection, event),
        "runtime.wait.matched" => index_wait_terminal(connection, event, WaitStatus::Matched),
        "runtime.wait.timed_out" => index_wait_terminal(connection, event, WaitStatus::TimedOut),
        "runtime.wait.cancelled" => index_wait_terminal(connection, event, WaitStatus::Cancelled),
        _ => Err(invalid_lifecycle(event, "event_type")),
    }
}

fn index_wait_created(connection: &Connection, event: &Event) -> Result<(), DispatchError> {
    let handle = required_runtime_id(event, "handle")?;
    let agent_id = required_runtime_id(event, "agent_id")?;
    let session_id = required_runtime_id(event, "session_id")?;
    SessionId::from_str(&session_id).map_err(|_error| invalid_lifecycle(event, "session_id"))?;
    if event.session_id.as_deref() != Some(session_id.as_str()) {
        return Err(invalid_lifecycle(event, "session_id"));
    }
    let event_type = required_runtime_id(event, "event_type")?;
    if event_type.starts_with("runtime.") {
        return Err(invalid_lifecycle(event, "event_type"));
    }
    let fields = required_payload_object(event, "fields")?;
    let fields_json =
        serde_json::to_string(&fields).map_err(|_error| invalid_lifecycle(event, "fields"))?;
    let deadline_ms = optional_payload_string(event, "deadline")?
        .map(|deadline| lifecycle_timestamp_ms(event, "deadline", &deadline))
        .transpose()?;
    let source_queue_item_id = required_runtime_id(event, "source_queue_item_id")?;
    QueueItemId::from_str(&source_queue_item_id)
        .map_err(|_error| invalid_lifecycle(event, "source_queue_item_id"))?;
    let project_generation = optional_payload_string(event, "project_generation")?;
    validate_optional_runtime_id(event, "project_generation", project_generation.as_deref())?;
    connection
        .execute(
            "INSERT INTO waits (
                handle, agent_id, session_id, event_type, fields_json,
                deadline_ms, source_queue_item_id, project_generation,
                created_event_id, status, matched_event_id,
                terminal_event_id, updated_at
             ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9,
                       'active', NULL, NULL, ?10)",
            params![
                handle,
                agent_id,
                session_id,
                event_type,
                fields_json,
                deadline_ms,
                source_queue_item_id,
                project_generation,
                &event.id,
                event.timestamp_ms,
            ],
        )
        .map_err(|database| database_error("project wait creation", database))?;
    Ok(())
}

fn index_wait_terminal(
    connection: &Connection,
    event: &Event,
    status: WaitStatus,
) -> Result<(), DispatchError> {
    let handle = required_runtime_id(event, "handle")?;
    let matched_event_id = if status == WaitStatus::Matched {
        Some(required_runtime_id(event, "matched_event_id")?)
    } else {
        None
    };
    if matched_event_id
        .as_deref()
        .is_some_and(|matched_event_id| event.caused_by.as_deref() != Some(matched_event_id))
    {
        return Err(invalid_lifecycle(event, "matched_event_id"));
    }
    let changed = connection
        .execute(
            "UPDATE waits
             SET status = ?1, matched_event_id = ?2,
                 terminal_event_id = ?3, updated_at = ?4
             WHERE handle = ?5 AND status = 'active'",
            params![
                wait_status_str(status),
                matched_event_id,
                &event.id,
                event.timestamp_ms,
                handle,
            ],
        )
        .map_err(|database| database_error("project wait terminal", database))?;
    if changed != 1 {
        return Err(invalid_lifecycle(event, "handle"));
    }
    Ok(())
}

fn index_deferred_publication(connection: &Connection, event: &Event) -> Result<(), DispatchError> {
    match event.event_type.as_str() {
        "runtime.deferred_publication.created" => {
            index_deferred_publication_created(connection, event)
        }
        "runtime.deferred_publication.published" => index_deferred_publication_terminal(
            connection,
            event,
            DeferredPublicationStatus::Published,
        ),
        "runtime.deferred_publication.cancelled" => index_deferred_publication_terminal(
            connection,
            event,
            DeferredPublicationStatus::Cancelled,
        ),
        _ => Err(invalid_lifecycle(event, "event_type")),
    }
}

fn index_deferred_publication_created(
    connection: &Connection,
    event: &Event,
) -> Result<(), DispatchError> {
    let handle = required_runtime_id(event, "handle")?;
    let event_type = required_runtime_id(event, "event_type")?;
    if event_type.starts_with("runtime.") {
        return Err(invalid_lifecycle(event, "event_type"));
    }
    let payload = required_payload_object(event, "payload")?;
    let payload_json =
        serde_json::to_string(&payload).map_err(|_error| invalid_lifecycle(event, "payload"))?;
    let publish_at = required_runtime_id(event, "publish_at")?;
    let publish_at_ms = lifecycle_timestamp_ms(event, "publish_at", &publish_at)?;
    let source_agent_id = required_runtime_id(event, "source_agent_id")?;
    let source_queue_item_id = required_runtime_id(event, "source_queue_item_id")?;
    QueueItemId::from_str(&source_queue_item_id)
        .map_err(|_error| invalid_lifecycle(event, "source_queue_item_id"))?;
    let position = required_nonnegative_u64(event, "position")?;
    let position =
        i64::try_from(position).map_err(|_error| invalid_lifecycle(event, "position"))?;
    let source_session_id = optional_payload_string(event, "source_session_id")?;
    if source_session_id != event.session_id {
        return Err(invalid_lifecycle(event, "source_session_id"));
    }
    if let Some(session_id) = &source_session_id {
        SessionId::from_str(session_id)
            .map_err(|_error| invalid_lifecycle(event, "source_session_id"))?;
    }
    if let Some(run_id) = &event.run_id {
        RunId::from_str(run_id).map_err(|_error| invalid_lifecycle(event, "source_run_id"))?;
    }
    connection
        .execute(
            "INSERT INTO deferred_publications (
                handle, event_type, payload_json, publish_at_ms,
                source_agent_id, source_session_id, source_run_id,
                source_queue_item_id, position, created_event_id, status,
                published_event_id, terminal_event_id, updated_at
             ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10,
                       'pending', NULL, NULL, ?11)",
            params![
                handle,
                event_type,
                payload_json,
                publish_at_ms,
                source_agent_id,
                source_session_id,
                event.run_id.as_deref(),
                source_queue_item_id,
                position,
                &event.id,
                event.timestamp_ms,
            ],
        )
        .map_err(|database| database_error("project deferred publication creation", database))?;
    Ok(())
}

fn index_deferred_publication_terminal(
    connection: &Connection,
    event: &Event,
    status: DeferredPublicationStatus,
) -> Result<(), DispatchError> {
    let handle = required_runtime_id(event, "handle")?;
    let published_event_id = if status == DeferredPublicationStatus::Published {
        Some(required_runtime_id(event, "published_event_id")?)
    } else {
        None
    };
    if published_event_id
        .as_deref()
        .is_some_and(|published_event_id| event.caused_by.as_deref() != Some(published_event_id))
    {
        return Err(invalid_lifecycle(event, "published_event_id"));
    }
    let changed = connection
        .execute(
            "UPDATE deferred_publications
             SET status = ?1, published_event_id = ?2,
                 terminal_event_id = ?3, updated_at = ?4
             WHERE handle = ?5 AND status IN ('pending', 'claimed')",
            params![
                deferred_publication_status_str(status),
                published_event_id,
                &event.id,
                event.timestamp_ms,
                handle,
            ],
        )
        .map_err(|database| database_error("project deferred publication terminal", database))?;
    if changed != 1 {
        return Err(invalid_lifecycle(event, "handle"));
    }
    Ok(())
}

fn index_queue_item_cancel_requested(
    connection: &Connection,
    event: &Event,
) -> Result<(), DispatchError> {
    let queue_item_id = required_runtime_id(event, "queue_item_id")?;
    let queue_item_id = QueueItemId::from_str(&queue_item_id).map_err(|_error| {
        DispatchError::InvalidLifecycleEvent {
            event_id: event.id.clone(),
            field: "queue_item_id",
        }
    })?;
    let input_event_id = required_runtime_id(event, "event_id")?;
    let target_agent = required_payload_string(event, "target_agent", true)?;
    let supplied_status = required_payload_string(event, "status", false)?;
    QueueItemStatus::from_str(&supplied_status).map_err(|_error| {
        DispatchError::InvalidLifecycleEvent {
            event_id: event.id.clone(),
            field: "status",
        }
    })?;
    let reason = optional_payload_string(event, "reason")?;
    let changed = connection
        .execute(
            "UPDATE queue_items
             SET cancel_requested_event_id = COALESCE(cancel_requested_event_id, ?1),
                 cancel_requested_at = COALESCE(cancel_requested_at, ?2),
                 cancel_reason = COALESCE(cancel_reason, ?3)
             WHERE queue_item_id = ?4
               AND event_id = ?5
               AND target_agent = ?6",
            params![
                &event.id,
                event.timestamp_ms,
                reason,
                queue_item_id.as_str(),
                input_event_id,
                target_agent,
            ],
        )
        .map_err(|database| database_error("project cancellation request", database))?;
    if changed != 1 {
        return Err(DispatchError::InvalidLifecycleEvent {
            event_id: event.id.clone(),
            field: "queue_item_id",
        });
    }
    Ok(())
}

fn is_queueable_event(event: &Event) -> bool {
    for prefix in ["runtime.", "zeta."] {
        if event.event_type.starts_with(prefix) {
            return false;
        }
    }
    true
}

fn index_pending_queue_item(connection: &Connection, event: &Event) -> Result<(), DispatchError> {
    let Some(cursor) = event.cursor else {
        return Err(DispatchError::InvalidLifecycleEvent {
            event_id: event.id.clone(),
            field: "cursor",
        });
    };
    if cursor > i64::MAX as u64 {
        return Err(DispatchError::InvalidLifecycleEvent {
            event_id: event.id.clone(),
            field: "cursor",
        });
    }
    let queue_item_id = pending_queue_item_id(&event.id);
    connection
        .execute(
            "INSERT INTO queue_items (
                queue_item_id, event_id, target_agent, input_cursor, status,
                available_at, updated_at
             ) VALUES (?1, ?2, '', ?3, 'pending', ?4, ?4)
             ON CONFLICT(queue_item_id) DO NOTHING",
            params![
                queue_item_id.as_str(),
                &event.id,
                cursor as i64,
                event.timestamp_ms
            ],
        )
        .map_err(|error| database_error("project pending queue item", error))?;
    Ok(())
}

fn index_queue_item(connection: &Connection, event: &Event) -> Result<(), DispatchError> {
    let queue_item_id = required_runtime_id(event, "queue_item_id")?;
    let queue_item_id = QueueItemId::from_str(&queue_item_id).map_err(|_error| {
        DispatchError::InvalidLifecycleEvent {
            event_id: event.id.clone(),
            field: "queue_item_id",
        }
    })?;
    let input_event_id = required_runtime_id(event, "event_id")?;
    let target_agent = required_payload_string(event, "target_agent", true)?;
    let status = lifecycle_queue_status(event)?;
    let pending_id = pending_queue_item_id(&input_event_id);
    if queue_item_id != pending_id {
        connection
            .execute(
                "DELETE FROM queue_items
                 WHERE queue_item_id = ?1 AND target_agent = ''",
                params![pending_id.as_str()],
            )
            .map_err(|error| database_error("close pending route barrier", error))?;
    }

    let previous = connection
        .query_row(
            "SELECT event_id, target_agent, status
             FROM queue_items WHERE queue_item_id = ?1",
            params![queue_item_id.as_str()],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                ))
            },
        )
        .optional()
        .map_err(|error| database_error("read queue transition", error))?;
    let previous_status = match previous {
        Some((previous_event_id, previous_target_agent, previous_status)) => {
            if previous_event_id != input_event_id {
                return Err(DispatchError::InvalidLifecycleEvent {
                    event_id: event.id.clone(),
                    field: "event_id",
                });
            }
            if previous_target_agent != target_agent
                && previous_status != QueueItemStatus::Pending.to_string()
            {
                return Err(DispatchError::InvalidLifecycleEvent {
                    event_id: event.id.clone(),
                    field: "target_agent",
                });
            }
            Some(
                QueueItemStatus::from_str(&previous_status).map_err(|_error| {
                    DispatchError::CorruptProjection {
                        table: "queue_items",
                        field: "status",
                    }
                })?,
            )
        }
        None => None,
    };
    let closes_unbound_barrier = previous_status == Some(QueueItemStatus::Pending)
        && queue_item_id == pending_id
        && target_agent.is_empty()
        && status == QueueItemStatus::Completed;
    let cancels_pending_item = previous_status == Some(QueueItemStatus::Pending)
        && status == QueueItemStatus::Cancelled
        && connection
            .query_row(
                "SELECT 1 FROM queue_items
                 WHERE queue_item_id = ?1
                   AND cancel_requested_event_id IS NOT NULL",
                params![queue_item_id.as_str()],
                |_row| Ok(()),
            )
            .optional()
            .map_err(|database| database_error("check pending cancellation", database))?
            .is_some();
    if !closes_unbound_barrier && !cancels_pending_item {
        QueueItemStatus::validate_transition(previous_status, status)?;
    }

    let input_cursor = input_event_cursor(connection, &input_event_id, event)?;
    let session_id =
        optional_payload_string(event, "session_id")?.or_else(|| event.session_id.clone());
    validate_optional_runtime_id(event, "session_id", session_id.as_deref())?;
    let project_generation = optional_payload_string(event, "project_generation")?;
    validate_optional_runtime_id(event, "project_generation", project_generation.as_deref())?;
    let lock_keys = optional_payload_string_array(event, "lock_keys")?;
    let lock_keys_json = lock_keys
        .map(|keys| serde_json::to_string(&keys))
        .transpose()
        .map_err(|_error| DispatchError::InvalidLifecycleEvent {
            event_id: event.id.clone(),
            field: "lock_keys",
        })?;
    let last_error = queue_last_error(event)?;
    let available_at = if status == QueueItemStatus::Available {
        Some(queue_available_at(event)?)
    } else {
        None
    };
    connection
        .execute(
            "INSERT INTO queue_items (
                queue_item_id, event_id, target_agent, project_generation,
                session_id, lock_keys_json, input_cursor, status, available_at,
                last_error, updated_at
             ) VALUES (?1, ?2, ?3, ?4, ?5, COALESCE(?6, '[]'), ?7, ?8, ?9, ?10, ?11)
             ON CONFLICT(queue_item_id) DO UPDATE SET
                event_id = excluded.event_id,
                target_agent = excluded.target_agent,
                project_generation = COALESCE(
                    excluded.project_generation,
                    queue_items.project_generation
                ),
                session_id = COALESCE(excluded.session_id, queue_items.session_id),
                lock_keys_json = COALESCE(?6, queue_items.lock_keys_json),
                input_cursor = excluded.input_cursor,
                status = excluded.status,
                available_at = CASE
                    WHEN excluded.status = 'available' THEN excluded.available_at
                    ELSE queue_items.available_at
                END,
                last_error = excluded.last_error,
                updated_at = excluded.updated_at",
            params![
                queue_item_id.as_str(),
                input_event_id,
                target_agent,
                project_generation,
                session_id,
                lock_keys_json,
                input_cursor,
                status.to_string(),
                available_at,
                last_error,
                event.timestamp_ms,
            ],
        )
        .map_err(|error| database_error("project queue lifecycle", error))?;
    Ok(())
}

fn index_attempt(connection: &Connection, event: &Event) -> Result<(), DispatchError> {
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
    let supplied_project_generation = optional_payload_string(event, "project_generation")?;
    validate_optional_runtime_id(
        event,
        "project_generation",
        supplied_project_generation.as_deref(),
    )?;
    let previous = connection
        .query_row(
            "SELECT queue_item_id, event_id, attempt_number, target_agent,
                    status, started_at, session_id, run_id, project_generation
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
                    project_generation: row.get(8)?,
                })
            },
        )
        .optional()
        .map_err(|error| database_error("read attempt transition", error))?;
    let (previous_status, started_at, session_id, run_id, project_generation) = match previous {
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
                || supplied_project_generation
                    .as_deref()
                    .is_some_and(|value| Some(value) != previous.project_generation.as_deref())
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
                previous.project_generation,
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
                supplied_project_generation,
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
                project_generation
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
                project_generation = COALESCE(
                    excluded.project_generation,
                    attempts.project_generation
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
                project_generation,
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
    project_generation: Option<String>,
}

fn lifecycle_queue_status(event: &Event) -> Result<QueueItemStatus, DispatchError> {
    let suffix = event.event_type.rsplit('.').next().unwrap_or_default();
    let expected = QueueItemStatus::from_str(suffix).map_err(|_error| {
        DispatchError::InvalidLifecycleEvent {
            event_id: event.id.clone(),
            field: "event_type",
        }
    })?;
    let actual = match event.payload.get("status") {
        Some(Value::String(status)) => QueueItemStatus::from_str(status).map_err(|_error| {
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

fn required_runtime_id(event: &Event, field: &'static str) -> Result<String, DispatchError> {
    required_payload_string(event, field, false)
}

fn required_payload_object(
    event: &Event,
    field: &'static str,
) -> Result<Map<String, Value>, DispatchError> {
    match event.payload.get(field) {
        Some(Value::Object(value)) => Ok(value.clone()),
        _ => Err(invalid_lifecycle(event, field)),
    }
}

fn optional_payload_object(
    event: &Event,
    field: &'static str,
) -> Result<Option<Map<String, Value>>, DispatchError> {
    match event.payload.get(field) {
        Some(Value::Object(value)) => Ok(Some(value.clone())),
        Some(Value::Null) | None => Ok(None),
        Some(_value) => Err(invalid_lifecycle(event, field)),
    }
}

fn required_nonnegative_u64(event: &Event, field: &'static str) -> Result<u64, DispatchError> {
    event
        .payload
        .get(field)
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_lifecycle(event, field))
}

fn lifecycle_timestamp_ms(
    event: &Event,
    field: &'static str,
    value: &str,
) -> Result<i64, DispatchError> {
    let timestamp =
        OffsetDateTime::parse(value, &Rfc3339).map_err(|_error| invalid_lifecycle(event, field))?;
    i64::try_from(timestamp.unix_timestamp_nanos().div_euclid(1_000_000))
        .map_err(|_error| invalid_lifecycle(event, field))
}

fn wait_status_str(status: WaitStatus) -> &'static str {
    match status {
        WaitStatus::Active => "active",
        WaitStatus::Matched => "matched",
        WaitStatus::TimedOut => "timed_out",
        WaitStatus::Cancelled => "cancelled",
    }
}

fn deferred_publication_status_str(status: DeferredPublicationStatus) -> &'static str {
    match status {
        DeferredPublicationStatus::Pending => "pending",
        DeferredPublicationStatus::Claimed => "claimed",
        DeferredPublicationStatus::Published => "published",
        DeferredPublicationStatus::Cancelled => "cancelled",
    }
}

fn lifecycle_effect_status(event: &Event) -> Result<EffectStatus, DispatchError> {
    let suffix = event.event_type.rsplit('.').next().unwrap_or_default();
    let status = parse_effect_status(event, suffix)?;
    let supplied = required_payload_string(event, "status", false)?;
    if supplied != suffix {
        return Err(invalid_lifecycle(event, "status"));
    }
    Ok(status)
}

fn parse_effect_status(event: &Event, value: &str) -> Result<EffectStatus, DispatchError> {
    match value {
        "planned" => Ok(EffectStatus::Planned),
        "started" => Ok(EffectStatus::Started),
        "completed" => Ok(EffectStatus::Completed),
        "failed" => Ok(EffectStatus::Failed),
        "ambiguous" => Ok(EffectStatus::Ambiguous),
        _ => Err(invalid_lifecycle(event, "status")),
    }
}

fn effect_status_str(status: EffectStatus) -> &'static str {
    match status {
        EffectStatus::Planned => "planned",
        EffectStatus::Started => "started",
        EffectStatus::Completed => "completed",
        EffectStatus::Failed => "failed",
        EffectStatus::Ambiguous => "ambiguous",
    }
}

pub(super) fn effect_status_is_terminal(status: EffectStatus) -> bool {
    matches!(
        status,
        EffectStatus::Completed | EffectStatus::Failed | EffectStatus::Ambiguous
    )
}

fn parse_effect_semantics(
    event: &Event,
    value: &str,
) -> Result<EffectDeliverySemantics, DispatchError> {
    match value {
        "idempotent_with_key" => Ok(EffectDeliverySemantics::IdempotentWithKey),
        "connector_deduplicated" => Ok(EffectDeliverySemantics::ConnectorDeduplicated),
        "at_least_once" => Ok(EffectDeliverySemantics::AtLeastOnce),
        "unsafe_to_retry" => Ok(EffectDeliverySemantics::UnsafeToRetry),
        _ => Err(invalid_lifecycle(event, "semantics")),
    }
}

fn effect_semantics_str(semantics: EffectDeliverySemantics) -> &'static str {
    match semantics {
        EffectDeliverySemantics::IdempotentWithKey => "idempotent_with_key",
        EffectDeliverySemantics::ConnectorDeduplicated => "connector_deduplicated",
        EffectDeliverySemantics::AtLeastOnce => "at_least_once",
        EffectDeliverySemantics::UnsafeToRetry => "unsafe_to_retry",
    }
}

fn invalid_lifecycle(event: &Event, field: &'static str) -> DispatchError {
    DispatchError::InvalidLifecycleEvent {
        event_id: event.id.clone(),
        field,
    }
}

fn required_payload_string(
    event: &Event,
    field: &'static str,
    allow_empty: bool,
) -> Result<String, DispatchError> {
    let Some(Value::String(value)) = event.payload.get(field) else {
        return Err(DispatchError::InvalidLifecycleEvent {
            event_id: event.id.clone(),
            field,
        });
    };
    if !allow_empty && value.is_empty() {
        return Err(DispatchError::InvalidLifecycleEvent {
            event_id: event.id.clone(),
            field,
        });
    }
    Ok(value.clone())
}

fn optional_payload_string(
    event: &Event,
    field: &'static str,
) -> Result<Option<String>, DispatchError> {
    match event.payload.get(field) {
        Some(Value::String(value)) => Ok(Some(value.clone())),
        Some(Value::Null) => Ok(None),
        Some(_value) => Err(DispatchError::InvalidLifecycleEvent {
            event_id: event.id.clone(),
            field,
        }),
        None => Ok(None),
    }
}

fn optional_payload_string_array(
    event: &Event,
    field: &'static str,
) -> Result<Option<Vec<String>>, DispatchError> {
    let Some(value) = event.payload.get(field) else {
        return Ok(None);
    };
    let Value::Array(values) = value else {
        return Err(DispatchError::InvalidLifecycleEvent {
            event_id: event.id.clone(),
            field,
        });
    };
    let mut result = Vec::new();
    let mut seen = HashSet::new();
    for value in values {
        let Value::String(value) = value else {
            return Err(DispatchError::InvalidLifecycleEvent {
                event_id: event.id.clone(),
                field,
            });
        };
        if value.is_empty() || !seen.insert(value) {
            return Err(DispatchError::InvalidLifecycleEvent {
                event_id: event.id.clone(),
                field,
            });
        }
        result.push(value.clone());
    }
    Ok(Some(result))
}

fn queue_last_error(event: &Event) -> Result<Option<String>, DispatchError> {
    if let Some(error) = optional_payload_string(event, "error")? {
        return Ok(Some(error));
    }
    let Some(value) = event.payload.get("last_error") else {
        return Ok(None);
    };
    let Value::Object(last_error) = value else {
        return Err(DispatchError::InvalidLifecycleEvent {
            event_id: event.id.clone(),
            field: "last_error",
        });
    };
    match last_error.get("message") {
        Some(Value::String(message)) => Ok(Some(message.clone())),
        _ => Err(DispatchError::InvalidLifecycleEvent {
            event_id: event.id.clone(),
            field: "last_error",
        }),
    }
}

fn validate_optional_runtime_id(
    event: &Event,
    field: &'static str,
    value: Option<&str>,
) -> Result<(), DispatchError> {
    if value == Some("") {
        return Err(DispatchError::InvalidLifecycleEvent {
            event_id: event.id.clone(),
            field,
        });
    }
    Ok(())
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

fn queue_available_at(event: &Event) -> Result<i64, DispatchError> {
    let Some(value) = event.payload.get("not_before") else {
        return Ok(event.timestamp_ms);
    };
    let Value::Number(number) = value else {
        return Err(DispatchError::InvalidLifecycleEvent {
            event_id: event.id.clone(),
            field: "not_before",
        });
    };
    if let Some(value) = number.as_i64() {
        return Ok(value);
    }
    let Some(value) = number.as_f64() else {
        return Err(DispatchError::InvalidLifecycleEvent {
            event_id: event.id.clone(),
            field: "not_before",
        });
    };
    if !value.is_finite() || value < i64::MIN as f64 || value >= i64::MAX as f64 {
        return Err(DispatchError::InvalidLifecycleEvent {
            event_id: event.id.clone(),
            field: "not_before",
        });
    }
    Ok(value as i64)
}

fn input_event_cursor(
    connection: &Connection,
    event_id: &str,
    lifecycle_event: &Event,
) -> Result<i64, DispatchError> {
    let cursor = connection
        .query_row(
            "SELECT cursor FROM journal_entries WHERE event_id = ?1",
            params![event_id],
            |row| row.get::<_, i64>(0),
        )
        .optional()
        .map_err(|error| database_error("read input event cursor", error))?;
    let Some(cursor) = cursor else {
        return Err(DispatchError::InvalidLifecycleEvent {
            event_id: lifecycle_event.id.clone(),
            field: "event_id",
        });
    };
    if cursor <= 0 {
        return Err(DispatchError::CorruptJournal {
            cursor: None,
            field: "cursor",
        });
    }
    Ok(cursor)
}

pub(super) fn rebuild_projections_in_transaction(
    connection: &Connection,
) -> Result<usize, DispatchError> {
    let entries = load_entries(connection, false)?;
    verify(&entries, HeadExpectation::Unanchored).map_err(DispatchError::Verification)?;
    connection
        .execute_batch(DROP_PROJECTIONS)
        .map_err(|error| database_error("drop projections", error))?;
    connection
        .execute_batch(CREATE_PROJECTIONS)
        .map_err(|error| database_error("create projections", error))?;
    for entry in &entries {
        index_event(connection, &entry.event)?;
    }
    connection
        .execute(
            "UPDATE queue_items
             SET status = CASE
                    WHEN target_agent = '' THEN 'pending'
                    ELSE 'available'
                 END
             WHERE status = 'claimed'",
            [],
        )
        .map_err(|error| database_error("recover claimed queue items", error))?;
    connection
        .execute(
            "UPDATE attempts SET claim_token = NULL, heartbeat_at = NULL",
            [],
        )
        .map_err(|error| database_error("clear replayed attempt ownership", error))?;
    connection
        .execute(
            "UPDATE deferred_publications SET status = 'pending' WHERE status = 'claimed'",
            [],
        )
        .map_err(|error| database_error("recover claimed deferred publications", error))?;
    Ok(entries.len())
}

const QUEUE_ITEM_COLUMNS: &str = "queue.queue_item_id, queue.event_id,
    queue.target_agent, queue.project_generation, queue.session_id,
    queue.lock_keys_json, queue.input_cursor,
    CASE WHEN claim.queue_item_id IS NULL THEN queue.status ELSE 'claimed' END,
    queue.available_at, claim.worker_name, claim.claimed_until,
    queue.cancel_requested_event_id, queue.cancel_requested_at,
    queue.cancel_reason, queue.attempt_count, queue.last_error, queue.updated_at";

pub(super) fn load_queue_item(
    connection: &Connection,
    queue_item_id: &str,
) -> Result<Option<QueueItem>, DispatchError> {
    let sql = format!(
        "SELECT {QUEUE_ITEM_COLUMNS}
         FROM queue_items AS queue
         LEFT JOIN queue_claims AS claim
           ON claim.queue_item_id = queue.queue_item_id
         WHERE queue.queue_item_id = ?1"
    );
    let stored = connection
        .query_row(&sql, params![queue_item_id], StoredQueueItem::from_row)
        .optional()
        .map_err(|error| database_error("read queue item", error))?;
    stored.map(StoredQueueItem::into_model).transpose()
}

pub(super) fn load_queue_items(connection: &Connection) -> Result<Vec<QueueItem>, DispatchError> {
    let sql = format!(
        "SELECT {QUEUE_ITEM_COLUMNS}
         FROM queue_items AS queue
         LEFT JOIN queue_claims AS claim
           ON claim.queue_item_id = queue.queue_item_id
         ORDER BY queue.input_cursor ASC, queue.queue_item_id ASC"
    );
    let mut statement = connection
        .prepare(&sql)
        .map_err(|error| database_error("prepare queue item read", error))?;
    let rows = statement
        .query_map([], StoredQueueItem::from_row)
        .map_err(|error| database_error("read queue items", error))?;
    let mut items = Vec::new();
    for row in rows {
        let stored = row.map_err(|error| database_error("read queue item", error))?;
        items.push(stored.into_model()?);
    }
    Ok(items)
}

struct StoredQueueItem {
    queue_item_id: String,
    event_id: String,
    target_agent: String,
    project_generation: Option<String>,
    session_id: Option<String>,
    lock_keys_json: String,
    input_cursor: i64,
    status: String,
    available_at: Option<i64>,
    claimed_by: Option<String>,
    claimed_until: Option<i64>,
    cancellation_requested_event_id: Option<String>,
    cancellation_requested_at: Option<i64>,
    cancellation_reason: Option<String>,
    attempt_count: i64,
    last_error: Option<String>,
    updated_at: i64,
}

impl StoredQueueItem {
    fn from_row(row: &Row<'_>) -> rusqlite::Result<Self> {
        Ok(StoredQueueItem {
            queue_item_id: row.get(0)?,
            event_id: row.get(1)?,
            target_agent: row.get(2)?,
            project_generation: row.get(3)?,
            session_id: row.get(4)?,
            lock_keys_json: row.get(5)?,
            input_cursor: row.get(6)?,
            status: row.get(7)?,
            available_at: row.get(8)?,
            claimed_by: row.get(9)?,
            claimed_until: row.get(10)?,
            cancellation_requested_event_id: row.get(11)?,
            cancellation_requested_at: row.get(12)?,
            cancellation_reason: row.get(13)?,
            attempt_count: row.get(14)?,
            last_error: row.get(15)?,
            updated_at: row.get(16)?,
        })
    }

    fn into_model(self) -> Result<QueueItem, DispatchError> {
        let id = QueueItemId::from_str(&self.queue_item_id)
            .map_err(|_error| corrupt_projection("queue_items", "queue_item_id"))?;
        let session_id = self
            .session_id
            .map(|id| SessionId::from_str(&id))
            .transpose()
            .map_err(|_error| corrupt_projection("queue_items", "session_id"))?;
        let status = QueueItemStatus::from_str(&self.status)
            .map_err(|_error| corrupt_projection("queue_items", "status"))?;
        let lock_keys: Vec<String> = serde_json::from_str(&self.lock_keys_json)
            .map_err(|_error| corrupt_projection("queue_items", "lock_keys_json"))?;
        let mut seen_lock_keys = HashSet::new();
        for lock_key in &lock_keys {
            if lock_key.is_empty() || !seen_lock_keys.insert(lock_key) {
                return Err(corrupt_projection("queue_items", "lock_keys_json"));
            }
        }
        let input_cursor =
            positive_u64_projection(self.input_cursor, "queue_items", "input_cursor")?;
        let attempt_count =
            nonnegative_u32_projection(self.attempt_count, "queue_items", "attempt_count")?;
        Ok(QueueItem {
            id,
            event_id: self.event_id,
            target_agent: self.target_agent,
            project_generation: self.project_generation,
            session_id,
            lock_keys,
            input_cursor,
            status,
            available_at: self.available_at,
            claimed_by: self.claimed_by,
            claimed_until: self.claimed_until,
            cancellation_requested_event_id: self.cancellation_requested_event_id,
            cancellation_requested_at: self.cancellation_requested_at,
            cancellation_reason: self.cancellation_reason,
            attempt_count,
            last_error: self.last_error,
            updated_at: self.updated_at,
        })
    }
}
