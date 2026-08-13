use std::collections::HashSet;
use std::str::FromStr;

use rusqlite::{params, Connection, OptionalExtension, Row, TransactionBehavior};
use serde_json::{Map, Value};
use zeta_journal::Event;

use super::journal::{
    append_lifecycle_candidate, entry_by_field, same_lifecycle_intention, same_logical_event,
    validate_event_identity,
};
use super::projection::index_event;
use super::{
    corrupt_projection, database_error, nonnegative_u32_projection, optional_payload_string,
    positive_u64_projection, required_payload_string, required_runtime_id,
    validate_optional_runtime_id, Dispatch, DispatchError,
};
use crate::dispatch::{QueueItem, RoutingOutcome, RuntimeEventIdentity};
use crate::identity::{pending_queue_item_id, queue_item_idempotency_key, QueueItemId, SessionId};
use crate::routing::{route_event, Route};
use crate::state::QueueItemStatus;

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

    /// Resolves and persists one ingress event's route plan atomically.
    ///
    /// No match records the unbound item as unhandled. One match binds the
    /// original pending identity directly. Multiple matches close the unbound
    /// barrier before creating one available item per decision. Retrying with
    /// fresh identities returns lifecycle events retained under their stable
    /// idempotency keys.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when the ingress event is missing, the number
    /// of explicit runtime identities differs from the route plan, a different
    /// route already committed, session resolution fails, or persistence fails.
    pub fn route_ingress_event(
        &mut self,
        event_id: &str,
        routes: &[Route],
        identities: &[RuntimeEventIdentity],
    ) -> Result<RoutingOutcome, DispatchError> {
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|error| database_error("begin route commit", error))?;
        let Some(entry) = entry_by_field(&transaction, "event_id", event_id)? else {
            return Err(DispatchError::IngressEventNotFound {
                event_id: event_id.to_owned(),
            });
        };
        let input = entry.event;
        let mut decisions = route_event(&input, routes)?;
        let expected_identities = if decisions.len() > 1 {
            decisions.len() + 1
        } else {
            1
        };
        if identities.len() != expected_identities {
            return Err(DispatchError::RuntimeEventIdentityCount {
                expected: expected_identities,
                actual: identities.len(),
            });
        }
        let mut generated_ids = HashSet::new();
        for identity in identities {
            if !generated_ids.insert(identity.id()) {
                return Err(DispatchError::DuplicateRuntimeEventIdentity {
                    event_id: identity.id().to_owned(),
                });
            }
        }
        let pending_id = pending_queue_item_id(&input.id);
        let lifecycle = lifecycle_for_route(&input, &pending_id, &mut decisions, identities);
        let pending = queue_status_and_target(&transaction, &pending_id)?;
        let already_committed = retained_routing_events(&transaction, &lifecycle)?;
        if let Some(events) = already_committed {
            transaction
                .commit()
                .map_err(|error| database_error("commit route retry", error))?;
            return Ok(RoutingOutcome { decisions, events });
        }
        if pending != Some((QueueItemStatus::Pending, String::new())) {
            return Err(DispatchError::IngressAlreadyRouted { event_id: input.id });
        }

        let mut events = Vec::new();
        for event in lifecycle {
            validate_event_identity(&event)?;
            let candidate = event.clone();
            let outcome = append_lifecycle_candidate(&transaction, event)?;
            if !outcome.inserted && !same_logical_event(&candidate, &outcome.event) {
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
            .commit()
            .map_err(|error| database_error("commit route", error))?;
        Ok(RoutingOutcome { decisions, events })
    }

    /// Returns ingress event ids whose unbound work items still await routing.
    ///
    /// An unbound pending item is a barrier that blocks every later claim, so
    /// a crash between [`Dispatch::ingest_event`] and
    /// [`Dispatch::route_ingress_event`] would stall the queue forever unless
    /// restart recovery can discover these events and re-drive routing in
    /// input order.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when the queue projection cannot be read.
    pub fn unrouted_ingress_events(&self) -> Result<Vec<String>, DispatchError> {
        let mut statement = self
            .connection
            .prepare(
                "SELECT event_id FROM queue_items
                 WHERE status = 'pending' AND target_agent = ''
                 ORDER BY input_cursor ASC",
            )
            .map_err(|error| database_error("prepare unrouted ingress events", error))?;
        let rows = statement
            .query_map([], |row| row.get::<_, String>(0))
            .map_err(|error| database_error("read unrouted ingress events", error))?;
        let mut event_ids = Vec::new();
        for row in rows {
            let event_id =
                row.map_err(|error| database_error("read unrouted ingress event", error))?;
            event_ids.push(event_id);
        }
        Ok(event_ids)
    }
}

fn lifecycle_for_route(
    input: &Event,
    pending_id: &QueueItemId,
    decisions: &mut [crate::routing::RouteDecision],
    identities: &[RuntimeEventIdentity],
) -> Vec<Event> {
    let mut events = Vec::new();
    if decisions.is_empty() {
        events.push(queue_lifecycle_event(
            &identities[0],
            input,
            QueueLifecycleFields {
                queue_item_id: pending_id,
                target_agent: "",
                status: QueueItemStatus::Unhandled,
                session_id: None,
                project_generation: None,
                lock_keys: &[],
            },
        ));
        return events;
    }
    if decisions.len() == 1 {
        decisions[0].bind_queue_item_id(pending_id.clone());
        events.push(queue_lifecycle_event(
            &identities[0],
            input,
            QueueLifecycleFields {
                queue_item_id: pending_id,
                target_agent: decisions[0].agent_id(),
                status: QueueItemStatus::Available,
                session_id: Some(decisions[0].session_id()),
                project_generation: decisions[0].project_generation(),
                lock_keys: decisions[0].lock_keys(),
            },
        ));
        return events;
    }

    events.push(queue_lifecycle_event(
        &identities[0],
        input,
        QueueLifecycleFields {
            queue_item_id: pending_id,
            target_agent: "",
            status: QueueItemStatus::Completed,
            session_id: None,
            project_generation: None,
            lock_keys: &[],
        },
    ));
    for index in 0..decisions.len() {
        let decision = &decisions[index];
        events.push(queue_lifecycle_event(
            &identities[index + 1],
            input,
            QueueLifecycleFields {
                queue_item_id: decision.queue_item_id(),
                target_agent: decision.agent_id(),
                status: QueueItemStatus::Available,
                session_id: Some(decision.session_id()),
                project_generation: decision.project_generation(),
                lock_keys: decision.lock_keys(),
            },
        ));
    }
    events
}

pub(super) struct QueueLifecycleFields<'a> {
    pub(super) queue_item_id: &'a QueueItemId,
    pub(super) target_agent: &'a str,
    pub(super) status: QueueItemStatus,
    pub(super) session_id: Option<&'a SessionId>,
    pub(super) project_generation: Option<&'a str>,
    pub(super) lock_keys: &'a [String],
}

pub(super) fn queue_lifecycle_event(
    identity: &RuntimeEventIdentity,
    input: &Event,
    fields: QueueLifecycleFields<'_>,
) -> Event {
    let QueueLifecycleFields {
        queue_item_id,
        target_agent,
        status,
        session_id,
        project_generation,
        lock_keys,
    } = fields;
    let mut payload = Map::new();
    payload.insert(
        "queue_item_id".to_owned(),
        Value::String(queue_item_id.to_string()),
    );
    payload.insert("event_id".to_owned(), Value::String(input.id.clone()));
    payload.insert(
        "target_agent".to_owned(),
        Value::String(target_agent.to_owned()),
    );
    payload.insert("status".to_owned(), Value::String(status.to_string()));
    if let Some(session_id) = session_id {
        payload.insert(
            "session_id".to_owned(),
            Value::String(session_id.to_string()),
        );
    }
    if let Some(project_generation) = project_generation {
        payload.insert(
            "project_generation".to_owned(),
            Value::String(project_generation.to_owned()),
        );
    }
    if !lock_keys.is_empty() {
        let mut values = Vec::new();
        for lock_key in lock_keys {
            values.push(Value::String(lock_key.clone()));
        }
        payload.insert("lock_keys".to_owned(), Value::Array(values));
    }
    Event {
        id: identity.id().to_owned(),
        event_type: format!("runtime.queue_item.{status}"),
        source: "zeta".to_owned(),
        payload,
        idempotency_key: Some(queue_item_idempotency_key(&input.id, target_agent, status)),
        caused_by: Some(input.id.clone()),
        session_id: session_id
            .map(ToString::to_string)
            .or_else(|| input.session_id.clone()),
        run_id: input.run_id.clone(),
        turn_id: input.turn_id.clone(),
        timestamp_ms: identity.timestamp_ms(),
        cursor: None,
    }
}

fn retained_routing_events(
    connection: &Connection,
    candidates: &[Event],
) -> Result<Option<Vec<Event>>, DispatchError> {
    let mut retained = Vec::new();
    let mut missing = 0;
    for candidate in candidates {
        let Some(idempotency_key) = &candidate.idempotency_key else {
            return Err(DispatchError::InvalidLifecycleEvent {
                event_id: candidate.id.clone(),
                field: "idempotency_key",
            });
        };
        let entry = entry_by_field(connection, "idempotency_key", idempotency_key)?;
        match entry {
            Some(entry) => {
                if !same_lifecycle_intention(candidate, &entry.event) {
                    return Err(DispatchError::IngressAlreadyRouted {
                        event_id: candidate.caused_by.clone().unwrap_or_default(),
                    });
                }
                retained.push(entry.event);
            }
            None => missing += 1,
        }
    }
    if missing == candidates.len() {
        return Ok(None);
    }
    if missing != 0 {
        return Err(DispatchError::IngressAlreadyRouted {
            event_id: candidates[0].caused_by.clone().unwrap_or_default(),
        });
    }
    Ok(Some(retained))
}

pub(super) fn queue_item_payload(
    queue_item: &QueueItem,
    status: QueueItemStatus,
) -> Map<String, Value> {
    let mut payload = Map::new();
    payload.insert(
        "queue_item_id".to_owned(),
        Value::String(queue_item.id.to_string()),
    );
    payload.insert(
        "event_id".to_owned(),
        Value::String(queue_item.event_id.clone()),
    );
    payload.insert(
        "target_agent".to_owned(),
        Value::String(queue_item.target_agent.clone()),
    );
    payload.insert("status".to_owned(), Value::String(status.to_string()));
    if let Some(session_id) = &queue_item.session_id {
        payload.insert(
            "session_id".to_owned(),
            Value::String(session_id.to_string()),
        );
    }
    if let Some(project_generation) = &queue_item.project_generation {
        payload.insert(
            "project_generation".to_owned(),
            Value::String(project_generation.clone()),
        );
    }
    payload
}

pub(super) fn index_queue_item_cancel_requested(
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

pub(super) fn index_pending_queue_item(
    connection: &Connection,
    event: &Event,
) -> Result<(), DispatchError> {
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

pub(super) fn index_queue_item(
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

fn queue_status_and_target(
    connection: &Connection,
    queue_item_id: &QueueItemId,
) -> Result<Option<(QueueItemStatus, String)>, DispatchError> {
    let stored = connection
        .query_row(
            "SELECT status, target_agent FROM queue_items WHERE queue_item_id = ?1",
            params![queue_item_id.as_str()],
            |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
        )
        .optional()
        .map_err(|error| database_error("read queue routing state", error))?;
    let Some((status, target_agent)) = stored else {
        return Ok(None);
    };
    let status =
        QueueItemStatus::from_str(&status).map_err(|_error| DispatchError::CorruptProjection {
            table: "queue_items",
            field: "status",
        })?;
    Ok(Some((status, target_agent)))
}
