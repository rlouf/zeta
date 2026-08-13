use std::str::FromStr;

use rusqlite::{params, Connection, OptionalExtension, Row, Transaction, TransactionBehavior};
use serde_json::{Map, Value};
use time::{format_description::well_known::Rfc3339, OffsetDateTime};
use zeta_journal::Event;

use super::journal::{append_runtime_event, entry_by_field, validate_distinct_runtime_identities};
use super::{
    corrupt_projection, database_error, invalid_lifecycle, optional_payload_string,
    required_payload_object, required_runtime_id, validate_optional_runtime_id, Dispatch,
    DispatchError,
};
use crate::dispatch::{
    DeferredPublication, DeferredPublicationStatus, ResourceCancellationOutcome,
    ResourceCancellationStatus, ResourceKind, RuntimeEventIdentity, Wait, WaitStatus,
};
use crate::identity::{queue_item_id, queue_item_idempotency_key, QueueItemId, RunId, SessionId};
use crate::state::QueueItemStatus;

impl Dispatch {
    /// Returns durable waits in creation order.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when a projection row cannot be read or
    /// rehydrated as the public typed model.
    pub fn list_waits(&self) -> Result<Vec<Wait>, DispatchError> {
        load_waits(&self.connection)
    }

    /// Returns durable deferred publications in due-time order.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when a projection row cannot be read or
    /// rehydrated as the public typed model.
    pub fn list_deferred_publications(&self) -> Result<Vec<DeferredPublication>, DispatchError> {
        load_deferred_publications(&self.connection)
    }

    /// Resumes every active wait matched by one retained external event.
    ///
    /// Matching compares the exact event type and every authored top-level
    /// payload field. All matched facts and continuation queue items commit in
    /// one transaction, so a retry observes no active work and returns empty.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when the input event is missing, reserved,
    /// the identity count is not twice the number of matches, or persistence
    /// cannot commit the complete batch.
    pub fn resume_waits_for_event(
        &mut self,
        event_id: &str,
        identities: &[RuntimeEventIdentity],
    ) -> Result<Vec<Event>, DispatchError> {
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|database| database_error("begin wait matching", database))?;
        let input = entry_by_field(&transaction, "event_id", event_id)?
            .ok_or_else(|| DispatchError::IngressEventNotFound {
                event_id: event_id.to_owned(),
            })?
            .event;
        let waits = if input.event_type.starts_with("runtime.") {
            Vec::new()
        } else {
            matching_waits(&transaction, &input)?
        };
        let expected_identities = waits.len() * 2;
        if identities.len() != expected_identities {
            return Err(DispatchError::RuntimeEventIdentityCount {
                expected: expected_identities,
                actual: identities.len(),
            });
        }
        validate_distinct_runtime_identities(identities)?;
        let mut events = Vec::with_capacity(expected_identities);
        for (wait, identities) in waits.iter().zip(identities.chunks_exact(2)) {
            let matched = wait_matched_event(&identities[0], wait, &input);
            let matched = append_runtime_event(&transaction, matched)?.event;
            let continuation = wait_continuation_event(&identities[1], wait, &matched);
            let continuation = append_runtime_event(&transaction, continuation)?.event;
            events.extend([matched, continuation]);
        }
        transaction
            .commit()
            .map_err(|database| database_error("commit wait matching", database))?;
        Ok(events)
    }

    /// Resumes the oldest active wait whose deadline has passed.
    ///
    /// The timeout fact and continuation queue item share one immediate
    /// transaction. `None` means no active wait is due at `now_ms`.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when the identities collide, a projected wait
    /// is corrupt, or the complete transaction cannot be persisted.
    pub fn timeout_next_due_wait(
        &mut self,
        now_ms: i64,
        identities: [RuntimeEventIdentity; 2],
    ) -> Result<Option<Vec<Event>>, DispatchError> {
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|database| database_error("begin wait timeout", database))?;
        let Some(wait) = next_due_wait(&transaction, now_ms)? else {
            transaction
                .commit()
                .map_err(|database| database_error("commit empty wait timeout", database))?;
            return Ok(None);
        };
        validate_distinct_runtime_identities(&identities)?;
        let timed_out = wait_timed_out_event(&identities[0], &wait)?;
        let timed_out = append_runtime_event(&transaction, timed_out)?.event;
        let continuation = wait_continuation_event(&identities[1], &wait, &timed_out);
        let continuation = append_runtime_event(&transaction, continuation)?.event;
        transaction
            .commit()
            .map_err(|database| database_error("commit wait timeout", database))?;
        Ok(Some(vec![timed_out, continuation]))
    }

    /// Cancels one active wait or pending one-shot publication by handle.
    ///
    /// Optional agent and session identities constrain the operation to the
    /// resource's recorded owner. A terminal resource reports the fact that
    /// won without appending another event.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] for an invalid or unknown handle, an ownership
    /// mismatch, an empty reason, corrupt projected state, or storage failure.
    pub fn cancel_resource(
        &mut self,
        handle: &str,
        reason: Option<&str>,
        source_agent_id: Option<&str>,
        source_session_id: Option<&str>,
        identity: RuntimeEventIdentity,
    ) -> Result<ResourceCancellationOutcome, DispatchError> {
        if reason == Some("") {
            return Err(DispatchError::InvalidCoordinationInput { field: "reason" });
        }
        let resource_kind = resource_kind_for_handle(handle)?;
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|database| database_error("begin resource cancellation", database))?;
        let outcome = cancel_resource_in_transaction(
            &transaction,
            handle,
            reason,
            source_agent_id,
            source_session_id,
            &identity,
            resource_kind,
        )?;
        transaction
            .commit()
            .map_err(|database| database_error("commit resource cancellation", database))?;
        Ok(outcome)
    }

    /// Publishes the oldest pending deferred publication whose due time has passed.
    ///
    /// The published event, any wait resumptions it triggers, and the
    /// publication terminal fact share one transaction. `None` means no
    /// publication is due.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when explicit identities do not cover the
    /// publication, every wait continuation, and the terminal fact, or when
    /// the complete transaction cannot be persisted.
    pub fn publish_next_due_deferred_publication(
        &mut self,
        now_ms: i64,
        identities: &[RuntimeEventIdentity],
    ) -> Result<Option<Vec<Event>>, DispatchError> {
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|database| database_error("begin deferred publication", database))?;
        let Some(publication) = next_due_deferred_publication(&transaction, now_ms)? else {
            if !identities.is_empty() {
                return Err(DispatchError::RuntimeEventIdentityCount {
                    expected: 0,
                    actual: identities.len(),
                });
            }
            transaction.commit().map_err(|database| {
                database_error("commit empty deferred publication poll", database)
            })?;
            return Ok(None);
        };
        if identities.len() < 2 {
            return Err(DispatchError::RuntimeEventIdentityCount {
                expected: 2,
                actual: identities.len(),
            });
        }
        let published = deferred_publication_event(&identities[0], &publication);
        let waits = matching_waits(&transaction, &published)?;
        let expected_identities = 2 + waits.len() * 2;
        if identities.len() != expected_identities {
            return Err(DispatchError::RuntimeEventIdentityCount {
                expected: expected_identities,
                actual: identities.len(),
            });
        }
        validate_distinct_runtime_identities(identities)?;
        let published = append_runtime_event(&transaction, published)?.event;
        let mut events = vec![published.clone()];
        for (wait, identities) in waits
            .iter()
            .zip(identities[1..identities.len() - 1].chunks_exact(2))
        {
            let matched = wait_matched_event(&identities[0], wait, &published);
            let matched = append_runtime_event(&transaction, matched)?.event;
            let continuation = wait_continuation_event(&identities[1], wait, &matched);
            let continuation = append_runtime_event(&transaction, continuation)?.event;
            events.extend([matched, continuation]);
        }
        let terminal = deferred_publication_published_event(
            &identities[identities.len() - 1],
            &publication,
            &published,
        );
        events.push(append_runtime_event(&transaction, terminal)?.event);
        transaction
            .commit()
            .map_err(|database| database_error("commit deferred publication", database))?;
        Ok(Some(events))
    }
}

fn matching_waits(connection: &Connection, event: &Event) -> Result<Vec<Wait>, DispatchError> {
    let sql = format!(
        "SELECT {WAIT_COLUMNS}
         FROM waits AS wait
         JOIN journal_entries AS created
           ON created.event_id = wait.created_event_id
         WHERE wait.status = 'active' AND wait.event_type = ?1
         ORDER BY created.cursor ASC, wait.handle ASC"
    );
    let mut statement = connection
        .prepare(&sql)
        .map_err(|database| database_error("prepare matching wait read", database))?;
    let rows = statement
        .query_map(params![&event.event_type], StoredWait::from_row)
        .map_err(|database| database_error("read matching waits", database))?;
    let mut waits = Vec::new();
    for row in rows {
        let stored = row.map_err(|database| database_error("read matching wait", database))?;
        let wait = stored.into_model()?;
        if wait
            .fields
            .iter()
            .all(|(key, value)| event.payload.get(key) == Some(value))
        {
            waits.push(wait);
        }
    }
    Ok(waits)
}

pub(super) fn active_wait_for_session(
    connection: &Connection,
    session_id: &SessionId,
) -> Result<Option<Wait>, DispatchError> {
    let sql = format!(
        "SELECT {WAIT_COLUMNS}
         FROM waits AS wait
         WHERE wait.session_id = ?1 AND wait.status = 'active'"
    );
    let stored = connection
        .query_row(&sql, params![session_id.as_str()], StoredWait::from_row)
        .optional()
        .map_err(|database| database_error("read active session wait", database))?;
    stored.map(StoredWait::into_model).transpose()
}

pub(super) fn resource_kind_for_handle(handle: &str) -> Result<ResourceKind, DispatchError> {
    if handle.starts_with("wait_") {
        return Ok(ResourceKind::Wait);
    }
    if handle.starts_with("pub_") {
        return Ok(ResourceKind::DeferredPublication);
    }
    Err(DispatchError::InvalidCancellationHandle {
        handle: handle.to_owned(),
    })
}

pub(super) fn cancel_resource_in_transaction(
    transaction: &Transaction<'_>,
    handle: &str,
    reason: Option<&str>,
    source_agent_id: Option<&str>,
    source_session_id: Option<&str>,
    identity: &RuntimeEventIdentity,
    resource_kind: ResourceKind,
) -> Result<ResourceCancellationOutcome, DispatchError> {
    match resource_kind {
        ResourceKind::Wait => {
            let Some(wait) = load_wait(transaction, handle)? else {
                return Err(DispatchError::CancellationResourceNotFound {
                    handle: handle.to_owned(),
                });
            };
            authorize_resource_cancellation(
                handle,
                &wait.agent_id,
                Some(wait.session_id.as_str()),
                source_agent_id,
                source_session_id,
            )?;
            match wait.status {
                WaitStatus::Active => {
                    let event = wait_cancelled_event(
                        identity,
                        &wait,
                        reason,
                        source_agent_id,
                        source_session_id,
                    );
                    let event = append_runtime_event(transaction, event)?;
                    if !event.inserted {
                        return Err(corrupt_projection("waits", "status"));
                    }
                    Ok(ResourceCancellationOutcome {
                        handle: handle.to_owned(),
                        resource_kind,
                        status: ResourceCancellationStatus::Cancelled,
                        changed: true,
                        event: Some(event.event),
                    })
                }
                WaitStatus::Matched => Ok(terminal_resource_cancellation(
                    handle,
                    resource_kind,
                    ResourceCancellationStatus::Matched,
                )),
                WaitStatus::TimedOut => Ok(terminal_resource_cancellation(
                    handle,
                    resource_kind,
                    ResourceCancellationStatus::TimedOut,
                )),
                WaitStatus::Cancelled => Ok(terminal_resource_cancellation(
                    handle,
                    resource_kind,
                    ResourceCancellationStatus::Cancelled,
                )),
            }
        }
        ResourceKind::DeferredPublication => {
            let Some(publication) = load_deferred_publication(transaction, handle)? else {
                return Err(DispatchError::CancellationResourceNotFound {
                    handle: handle.to_owned(),
                });
            };
            authorize_resource_cancellation(
                handle,
                &publication.source_agent_id,
                publication
                    .source_session_id
                    .as_ref()
                    .map(SessionId::as_str),
                source_agent_id,
                source_session_id,
            )?;
            match publication.status {
                DeferredPublicationStatus::Pending => {
                    let event = deferred_publication_cancelled_event(
                        identity,
                        &publication,
                        reason,
                        source_agent_id,
                        source_session_id,
                    );
                    let event = append_runtime_event(transaction, event)?;
                    if !event.inserted {
                        return Err(corrupt_projection("deferred_publications", "status"));
                    }
                    Ok(ResourceCancellationOutcome {
                        handle: handle.to_owned(),
                        resource_kind,
                        status: ResourceCancellationStatus::Cancelled,
                        changed: true,
                        event: Some(event.event),
                    })
                }
                DeferredPublicationStatus::Published => Ok(terminal_resource_cancellation(
                    handle,
                    resource_kind,
                    ResourceCancellationStatus::Published,
                )),
                DeferredPublicationStatus::Cancelled => Ok(terminal_resource_cancellation(
                    handle,
                    resource_kind,
                    ResourceCancellationStatus::Cancelled,
                )),
                DeferredPublicationStatus::Claimed => {
                    Err(corrupt_projection("deferred_publications", "status"))
                }
            }
        }
    }
}

fn authorize_resource_cancellation(
    handle: &str,
    creator_agent_id: &str,
    creator_session_id: Option<&str>,
    source_agent_id: Option<&str>,
    source_session_id: Option<&str>,
) -> Result<(), DispatchError> {
    let agent_mismatch = source_agent_id.is_some_and(|source| source != creator_agent_id);
    let session_mismatch =
        source_session_id.is_some_and(|source| Some(source) != creator_session_id);
    if agent_mismatch || session_mismatch {
        return Err(DispatchError::CancellationAuthorityMismatch {
            handle: handle.to_owned(),
        });
    }
    Ok(())
}

fn terminal_resource_cancellation(
    handle: &str,
    resource_kind: ResourceKind,
    status: ResourceCancellationStatus,
) -> ResourceCancellationOutcome {
    ResourceCancellationOutcome {
        handle: handle.to_owned(),
        resource_kind,
        status,
        changed: false,
        event: None,
    }
}

fn next_due_wait(connection: &Connection, now_ms: i64) -> Result<Option<Wait>, DispatchError> {
    let sql = format!(
        "SELECT {WAIT_COLUMNS}
         FROM waits AS wait
         JOIN journal_entries AS created
           ON created.event_id = wait.created_event_id
         WHERE wait.status = 'active'
           AND wait.deadline_ms IS NOT NULL
           AND wait.deadline_ms <= ?1
         ORDER BY wait.deadline_ms ASC, created.cursor ASC, wait.handle ASC
         LIMIT 1"
    );
    let stored = connection
        .query_row(&sql, params![now_ms], StoredWait::from_row)
        .optional()
        .map_err(|database| database_error("read next due wait", database))?;
    stored.map(StoredWait::into_model).transpose()
}

fn next_due_deferred_publication(
    connection: &Connection,
    now_ms: i64,
) -> Result<Option<DeferredPublication>, DispatchError> {
    let sql = format!(
        "SELECT {DEFERRED_PUBLICATION_COLUMNS}
         FROM deferred_publications AS publication
         JOIN journal_entries AS created
           ON created.event_id = publication.created_event_id
         WHERE publication.status = 'pending' AND publication.publish_at_ms <= ?1
         ORDER BY publication.publish_at_ms ASC, created.cursor ASC,
                  publication.handle ASC
         LIMIT 1"
    );
    let stored = connection
        .query_row(&sql, params![now_ms], StoredDeferredPublication::from_row)
        .optional()
        .map_err(|database| database_error("read next due deferred publication", database))?;
    stored
        .map(StoredDeferredPublication::into_model)
        .transpose()
}

fn wait_matched_event(identity: &RuntimeEventIdentity, wait: &Wait, input: &Event) -> Event {
    let mut payload = Map::new();
    payload.insert("handle".to_owned(), Value::String(wait.handle.clone()));
    payload.insert("agent_id".to_owned(), Value::String(wait.agent_id.clone()));
    payload.insert(
        "session_id".to_owned(),
        Value::String(wait.session_id.to_string()),
    );
    payload.insert(
        "matched_event_id".to_owned(),
        Value::String(input.id.clone()),
    );
    payload.insert(
        "event_type".to_owned(),
        Value::String(input.event_type.clone()),
    );
    payload.insert("payload".to_owned(), Value::Object(input.payload.clone()));
    if let Some(project_generation) = &wait.project_generation {
        payload.insert(
            "project_generation".to_owned(),
            Value::String(project_generation.clone()),
        );
    }
    Event {
        id: identity.id().to_owned(),
        event_type: "runtime.wait.matched".to_owned(),
        source: "zeta".to_owned(),
        payload,
        idempotency_key: Some(format!("wait.matched:{}", wait.handle)),
        caused_by: Some(input.id.clone()),
        session_id: Some(wait.session_id.to_string()),
        run_id: None,
        turn_id: input.turn_id.clone(),
        timestamp_ms: identity.timestamp_ms(),
        cursor: None,
    }
}

fn wait_timed_out_event(
    identity: &RuntimeEventIdentity,
    wait: &Wait,
) -> Result<Event, DispatchError> {
    let deadline_ms = wait
        .deadline_ms
        .ok_or_else(|| corrupt_projection("waits", "deadline_ms"))?;
    let mut payload = Map::new();
    payload.insert("handle".to_owned(), Value::String(wait.handle.clone()));
    payload.insert("agent_id".to_owned(), Value::String(wait.agent_id.clone()));
    payload.insert(
        "session_id".to_owned(),
        Value::String(wait.session_id.to_string()),
    );
    payload.insert(
        "deadline".to_owned(),
        Value::String(format_wait_deadline(deadline_ms)?),
    );
    payload.insert(
        "project_generation".to_owned(),
        wait.project_generation
            .clone()
            .map(Value::String)
            .unwrap_or(Value::Null),
    );
    Ok(Event {
        id: identity.id().to_owned(),
        event_type: "runtime.wait.timed_out".to_owned(),
        source: "zeta".to_owned(),
        payload,
        idempotency_key: Some(format!("wait.timed_out:{}", wait.handle)),
        caused_by: Some(wait.created_event_id.clone()),
        session_id: Some(wait.session_id.to_string()),
        run_id: None,
        turn_id: None,
        timestamp_ms: identity.timestamp_ms(),
        cursor: None,
    })
}

fn wait_cancelled_event(
    identity: &RuntimeEventIdentity,
    wait: &Wait,
    reason: Option<&str>,
    source_agent_id: Option<&str>,
    source_session_id: Option<&str>,
) -> Event {
    let mut payload = Map::new();
    payload.insert("handle".to_owned(), Value::String(wait.handle.clone()));
    payload.insert("agent_id".to_owned(), Value::String(wait.agent_id.clone()));
    payload.insert(
        "session_id".to_owned(),
        Value::String(wait.session_id.to_string()),
    );
    if let Some(reason) = reason {
        payload.insert("reason".to_owned(), Value::String(reason.to_owned()));
    }
    if let Some(source_agent_id) = source_agent_id {
        payload.insert(
            "cancelled_by_agent_id".to_owned(),
            Value::String(source_agent_id.to_owned()),
        );
    }
    if let Some(source_session_id) = source_session_id {
        payload.insert(
            "cancelled_by_session_id".to_owned(),
            Value::String(source_session_id.to_owned()),
        );
    }
    Event {
        id: identity.id().to_owned(),
        event_type: "runtime.wait.cancelled".to_owned(),
        source: "zeta".to_owned(),
        payload,
        idempotency_key: Some(format!("wait.cancelled:{}", wait.handle)),
        caused_by: Some(wait.created_event_id.clone()),
        session_id: Some(wait.session_id.to_string()),
        run_id: None,
        turn_id: None,
        timestamp_ms: identity.timestamp_ms(),
        cursor: None,
    }
}

fn format_wait_deadline(deadline_ms: i64) -> Result<String, DispatchError> {
    let timestamp = OffsetDateTime::from_unix_timestamp_nanos(i128::from(deadline_ms) * 1_000_000)
        .map_err(|_error| corrupt_projection("waits", "deadline_ms"))?;
    let year = timestamp.year();
    let month = u8::from(timestamp.month());
    let day = timestamp.day();
    let hour = timestamp.hour();
    let minute = timestamp.minute();
    let second = timestamp.second();
    let microsecond = timestamp.nanosecond() / 1_000;
    if microsecond == 0 {
        return Ok(format!(
            "{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}+00:00"
        ));
    }
    Ok(format!(
        "{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}.{microsecond:06}+00:00"
    ))
}

fn wait_continuation_event(identity: &RuntimeEventIdentity, wait: &Wait, matched: &Event) -> Event {
    let queue_item_id = queue_item_id(&matched.id, &wait.agent_id);
    let mut payload = Map::new();
    payload.insert(
        "queue_item_id".to_owned(),
        Value::String(queue_item_id.to_string()),
    );
    payload.insert("event_id".to_owned(), Value::String(matched.id.clone()));
    payload.insert(
        "target_agent".to_owned(),
        Value::String(wait.agent_id.clone()),
    );
    payload.insert(
        "status".to_owned(),
        Value::String(QueueItemStatus::Available.to_string()),
    );
    if let Some(project_generation) = &wait.project_generation {
        payload.insert(
            "project_generation".to_owned(),
            Value::String(project_generation.clone()),
        );
    }
    Event {
        id: identity.id().to_owned(),
        event_type: "runtime.queue_item.available".to_owned(),
        source: "zeta".to_owned(),
        payload,
        idempotency_key: Some(queue_item_idempotency_key(
            &matched.id,
            &wait.agent_id,
            QueueItemStatus::Available,
        )),
        caused_by: Some(matched.id.clone()),
        session_id: Some(wait.session_id.to_string()),
        run_id: None,
        turn_id: matched.turn_id.clone(),
        timestamp_ms: identity.timestamp_ms(),
        cursor: None,
    }
}

fn deferred_publication_event(
    identity: &RuntimeEventIdentity,
    publication: &DeferredPublication,
) -> Event {
    Event {
        id: identity.id().to_owned(),
        event_type: publication.event_type.clone(),
        source: format!("agent:{}", publication.source_agent_id),
        payload: publication.payload.clone(),
        idempotency_key: Some(format!(
            "agent.publish:{}:{}",
            publication.source_queue_item_id, publication.position
        )),
        caused_by: Some(publication.created_event_id.clone()),
        session_id: publication
            .source_session_id
            .as_ref()
            .map(ToString::to_string),
        run_id: publication.source_run_id.as_ref().map(ToString::to_string),
        turn_id: None,
        timestamp_ms: identity.timestamp_ms(),
        cursor: None,
    }
}

fn deferred_publication_published_event(
    identity: &RuntimeEventIdentity,
    publication: &DeferredPublication,
    published: &Event,
) -> Event {
    let mut payload = Map::new();
    payload.insert(
        "handle".to_owned(),
        Value::String(publication.handle.clone()),
    );
    payload.insert(
        "source_agent_id".to_owned(),
        Value::String(publication.source_agent_id.clone()),
    );
    payload.insert(
        "source_queue_item_id".to_owned(),
        Value::String(publication.source_queue_item_id.to_string()),
    );
    payload.insert("position".to_owned(), Value::from(publication.position));
    payload.insert(
        "published_event_id".to_owned(),
        Value::String(published.id.clone()),
    );
    Event {
        id: identity.id().to_owned(),
        event_type: "runtime.deferred_publication.published".to_owned(),
        source: "zeta".to_owned(),
        payload,
        idempotency_key: Some(format!(
            "deferred_publication.published:{}",
            publication.handle
        )),
        caused_by: Some(published.id.clone()),
        session_id: publication
            .source_session_id
            .as_ref()
            .map(ToString::to_string),
        run_id: publication.source_run_id.as_ref().map(ToString::to_string),
        turn_id: published.turn_id.clone(),
        timestamp_ms: identity.timestamp_ms(),
        cursor: None,
    }
}

fn deferred_publication_cancelled_event(
    identity: &RuntimeEventIdentity,
    publication: &DeferredPublication,
    reason: Option<&str>,
    source_agent_id: Option<&str>,
    source_session_id: Option<&str>,
) -> Event {
    let mut payload = Map::new();
    payload.insert(
        "handle".to_owned(),
        Value::String(publication.handle.clone()),
    );
    payload.insert(
        "source_agent_id".to_owned(),
        Value::String(publication.source_agent_id.clone()),
    );
    payload.insert(
        "source_queue_item_id".to_owned(),
        Value::String(publication.source_queue_item_id.to_string()),
    );
    payload.insert("position".to_owned(), Value::from(publication.position));
    if let Some(reason) = reason {
        payload.insert("reason".to_owned(), Value::String(reason.to_owned()));
    }
    if let Some(source_agent_id) = source_agent_id {
        payload.insert(
            "cancelled_by_agent_id".to_owned(),
            Value::String(source_agent_id.to_owned()),
        );
    }
    if let Some(source_session_id) = source_session_id {
        payload.insert(
            "cancelled_by_session_id".to_owned(),
            Value::String(source_session_id.to_owned()),
        );
    }
    Event {
        id: identity.id().to_owned(),
        event_type: "runtime.deferred_publication.cancelled".to_owned(),
        source: "zeta".to_owned(),
        payload,
        idempotency_key: Some(format!(
            "deferred_publication.cancelled:{}",
            publication.handle
        )),
        caused_by: Some(publication.created_event_id.clone()),
        session_id: publication
            .source_session_id
            .as_ref()
            .map(ToString::to_string),
        run_id: publication.source_run_id.as_ref().map(ToString::to_string),
        turn_id: None,
        timestamp_ms: identity.timestamp_ms(),
        cursor: None,
    }
}

pub(super) fn index_wait(connection: &Connection, event: &Event) -> Result<(), DispatchError> {
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

pub(super) fn index_deferred_publication(
    connection: &Connection,
    event: &Event,
) -> Result<(), DispatchError> {
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

const WAIT_COLUMNS: &str = "wait.handle, wait.agent_id, wait.session_id,
    wait.event_type, wait.fields_json, wait.deadline_ms,
    wait.source_queue_item_id, wait.project_generation,
    wait.created_event_id, wait.status, wait.matched_event_id,
    wait.terminal_event_id, wait.updated_at";

fn load_wait(connection: &Connection, handle: &str) -> Result<Option<Wait>, DispatchError> {
    let sql = format!("SELECT {WAIT_COLUMNS} FROM waits AS wait WHERE wait.handle = ?1");
    let stored = connection
        .query_row(&sql, params![handle], StoredWait::from_row)
        .optional()
        .map_err(|database| database_error("read wait", database))?;
    stored.map(StoredWait::into_model).transpose()
}

pub(super) fn load_waits(connection: &Connection) -> Result<Vec<Wait>, DispatchError> {
    let sql = format!(
        "SELECT {WAIT_COLUMNS}
         FROM waits AS wait
         JOIN journal_entries AS created
           ON created.event_id = wait.created_event_id
         ORDER BY created.cursor ASC, wait.handle ASC"
    );
    let mut statement = connection
        .prepare(&sql)
        .map_err(|database| database_error("prepare wait read", database))?;
    let rows = statement
        .query_map([], StoredWait::from_row)
        .map_err(|database| database_error("read waits", database))?;
    let mut waits = Vec::new();
    for row in rows {
        let stored = row.map_err(|database| database_error("read wait", database))?;
        waits.push(stored.into_model()?);
    }
    Ok(waits)
}

struct StoredWait {
    handle: String,
    agent_id: String,
    session_id: String,
    event_type: String,
    fields_json: String,
    deadline_ms: Option<i64>,
    source_queue_item_id: String,
    project_generation: Option<String>,
    created_event_id: String,
    status: String,
    matched_event_id: Option<String>,
    terminal_event_id: Option<String>,
    updated_at: i64,
}

impl StoredWait {
    fn from_row(row: &Row<'_>) -> rusqlite::Result<Self> {
        Ok(StoredWait {
            handle: row.get(0)?,
            agent_id: row.get(1)?,
            session_id: row.get(2)?,
            event_type: row.get(3)?,
            fields_json: row.get(4)?,
            deadline_ms: row.get(5)?,
            source_queue_item_id: row.get(6)?,
            project_generation: row.get(7)?,
            created_event_id: row.get(8)?,
            status: row.get(9)?,
            matched_event_id: row.get(10)?,
            terminal_event_id: row.get(11)?,
            updated_at: row.get(12)?,
        })
    }

    fn into_model(self) -> Result<Wait, DispatchError> {
        let session_id = SessionId::from_str(&self.session_id)
            .map_err(|_error| corrupt_projection("waits", "session_id"))?;
        let source_queue_item_id = QueueItemId::from_str(&self.source_queue_item_id)
            .map_err(|_error| corrupt_projection("waits", "source_queue_item_id"))?;
        let fields = match serde_json::from_str(&self.fields_json) {
            Ok(Value::Object(fields)) => fields,
            _ => return Err(corrupt_projection("waits", "fields_json")),
        };
        let status = match self.status.as_str() {
            "active" => WaitStatus::Active,
            "matched" => WaitStatus::Matched,
            "timed_out" => WaitStatus::TimedOut,
            "cancelled" => WaitStatus::Cancelled,
            _ => return Err(corrupt_projection("waits", "status")),
        };
        Ok(Wait {
            handle: self.handle,
            agent_id: self.agent_id,
            session_id,
            event_type: self.event_type,
            fields,
            deadline_ms: self.deadline_ms,
            source_queue_item_id,
            project_generation: self.project_generation,
            created_event_id: self.created_event_id,
            status,
            matched_event_id: self.matched_event_id,
            terminal_event_id: self.terminal_event_id,
            updated_at: self.updated_at,
        })
    }
}

const DEFERRED_PUBLICATION_COLUMNS: &str = "publication.handle,
    publication.event_type, publication.payload_json, publication.publish_at_ms,
    publication.source_agent_id, publication.source_session_id,
    publication.source_run_id, publication.source_queue_item_id,
    publication.position, publication.created_event_id, publication.status,
    publication.published_event_id, publication.terminal_event_id,
    publication.updated_at";

fn load_deferred_publication(
    connection: &Connection,
    handle: &str,
) -> Result<Option<DeferredPublication>, DispatchError> {
    let sql = format!(
        "SELECT {DEFERRED_PUBLICATION_COLUMNS}
         FROM deferred_publications AS publication
         WHERE publication.handle = ?1"
    );
    let stored = connection
        .query_row(&sql, params![handle], StoredDeferredPublication::from_row)
        .optional()
        .map_err(|database| database_error("read deferred publication", database))?;
    stored
        .map(StoredDeferredPublication::into_model)
        .transpose()
}

fn load_deferred_publications(
    connection: &Connection,
) -> Result<Vec<DeferredPublication>, DispatchError> {
    let sql = format!(
        "SELECT {DEFERRED_PUBLICATION_COLUMNS}
         FROM deferred_publications AS publication
         JOIN journal_entries AS created
           ON created.event_id = publication.created_event_id
         ORDER BY publication.publish_at_ms ASC, created.cursor ASC,
                  publication.handle ASC"
    );
    let mut statement = connection
        .prepare(&sql)
        .map_err(|database| database_error("prepare deferred publication read", database))?;
    let rows = statement
        .query_map([], StoredDeferredPublication::from_row)
        .map_err(|database| database_error("read deferred publications", database))?;
    let mut publications = Vec::new();
    for row in rows {
        let stored =
            row.map_err(|database| database_error("read deferred publication", database))?;
        publications.push(stored.into_model()?);
    }
    Ok(publications)
}

struct StoredDeferredPublication {
    handle: String,
    event_type: String,
    payload_json: String,
    publish_at_ms: i64,
    source_agent_id: String,
    source_session_id: Option<String>,
    source_run_id: Option<String>,
    source_queue_item_id: String,
    position: i64,
    created_event_id: String,
    status: String,
    published_event_id: Option<String>,
    terminal_event_id: Option<String>,
    updated_at: i64,
}

impl StoredDeferredPublication {
    fn from_row(row: &Row<'_>) -> rusqlite::Result<Self> {
        Ok(StoredDeferredPublication {
            handle: row.get(0)?,
            event_type: row.get(1)?,
            payload_json: row.get(2)?,
            publish_at_ms: row.get(3)?,
            source_agent_id: row.get(4)?,
            source_session_id: row.get(5)?,
            source_run_id: row.get(6)?,
            source_queue_item_id: row.get(7)?,
            position: row.get(8)?,
            created_event_id: row.get(9)?,
            status: row.get(10)?,
            published_event_id: row.get(11)?,
            terminal_event_id: row.get(12)?,
            updated_at: row.get(13)?,
        })
    }

    fn into_model(self) -> Result<DeferredPublication, DispatchError> {
        let payload = match serde_json::from_str(&self.payload_json) {
            Ok(Value::Object(payload)) => payload,
            _ => return Err(corrupt_projection("deferred_publications", "payload_json")),
        };
        let source_session_id = self
            .source_session_id
            .map(|value| SessionId::from_str(&value))
            .transpose()
            .map_err(|_error| corrupt_projection("deferred_publications", "source_session_id"))?;
        let source_run_id = self
            .source_run_id
            .map(|value| RunId::from_str(&value))
            .transpose()
            .map_err(|_error| corrupt_projection("deferred_publications", "source_run_id"))?;
        let source_queue_item_id =
            QueueItemId::from_str(&self.source_queue_item_id).map_err(|_error| {
                corrupt_projection("deferred_publications", "source_queue_item_id")
            })?;
        let position = u64::try_from(self.position)
            .map_err(|_error| corrupt_projection("deferred_publications", "position"))?;
        let status = match self.status.as_str() {
            "pending" => DeferredPublicationStatus::Pending,
            "claimed" => DeferredPublicationStatus::Claimed,
            "published" => DeferredPublicationStatus::Published,
            "cancelled" => DeferredPublicationStatus::Cancelled,
            _ => return Err(corrupt_projection("deferred_publications", "status")),
        };
        Ok(DeferredPublication {
            handle: self.handle,
            event_type: self.event_type,
            payload,
            publish_at_ms: self.publish_at_ms,
            source_agent_id: self.source_agent_id,
            source_session_id,
            source_run_id,
            source_queue_item_id,
            position,
            created_event_id: self.created_event_id,
            status,
            published_event_id: self.published_event_id,
            terminal_event_id: self.terminal_event_id,
            updated_at: self.updated_at,
        })
    }
}
