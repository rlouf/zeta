use std::collections::{BTreeMap, BTreeSet};

use rusqlite::{Connection, TransactionBehavior};
use serde_json::{Map, Value};
use time::{format_description::well_known::Rfc3339, OffsetDateTime};
use zeta_journal::Event;

use super::attempts::load_attempts;
use super::journal::{
    append_lifecycle_candidate, append_runtime_event, entry_by_field, same_lifecycle_intention,
    validate_distinct_runtime_identities, validate_event_identity,
};
use super::projection::{index_event, load_queue_items};
use super::resources::{active_wait_for_session, cancel_resource_in_transaction, load_waits};
use super::routing::{queue_lifecycle_event, QueueLifecycleFields};
use super::{corrupt_projection, database_error, Dispatch, DispatchError};
use crate::dispatch::{
    Attempt, QueueItem, ResourceKind, RuntimeEventIdentity, Session, SessionActiveWait,
    SessionActivityStatus, SessionLatestRun, SessionMessageIdentities, SessionMessageRequest,
    SubmittedSessionMessage, Wait, WaitStatus,
};
use crate::identity::{queue_item_id, SessionId};
use crate::state::{AttemptStatus, QueueItemStatus};

impl Dispatch {
    /// Derives the current activity of every durable session.
    ///
    /// Status priority is running, queued, waiting, then idle. Sessions are
    /// ordered by latest activity and then identity, both descending. Owner
    /// conflicts remain visible in the catalog for diagnosis.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when a source projection is corrupt or its
    /// activity timestamp cannot be represented.
    pub fn list_sessions(&self) -> Result<Vec<Session>, DispatchError> {
        project_sessions(
            load_queue_items(&self.connection)?,
            load_attempts(&self.connection)?,
            load_waits(&self.connection)?,
        )
    }

    /// Returns one session only when its durable owner is unambiguous.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError::SessionNotFound`] for unknown identities,
    /// [`DispatchError::SessionOwnerConflict`] for inconsistent ownership, or
    /// another [`DispatchError`] when source projections cannot be read.
    pub fn session_status(&self, session_id: &SessionId) -> Result<Session, DispatchError> {
        let session = self
            .list_sessions()?
            .into_iter()
            .find(|session| &session.session_id == session_id)
            .ok_or_else(|| DispatchError::SessionNotFound {
                session_id: session_id.clone(),
            })?;
        if !session.conflicting_agent_ids.is_empty() {
            return Err(DispatchError::SessionOwnerConflict {
                session_id: session_id.clone(),
                agent_ids: session.conflicting_agent_ids,
            });
        }
        Ok(session)
    }

    /// Stores one addressed user turn and its executable queue binding.
    ///
    /// A newly inserted message first cancels the session's active wait. The
    /// cancellation, user fact, and queue binding share one immediate
    /// transaction. Retrying a durable key returns the retained message and
    /// binding without cancelling a later wait.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] for empty request fields, identity collisions,
    /// a conflicting wait owner, corrupt retained state, or storage failure.
    pub fn submit_session_message(
        &mut self,
        request: &SessionMessageRequest,
        identities: SessionMessageIdentities,
    ) -> Result<SubmittedSessionMessage, DispatchError> {
        validate_session_message_request(request)?;
        validate_distinct_runtime_identities(&[
            identities.wait_cancelled.clone(),
            identities.requested.clone(),
            identities.available.clone(),
        ])?;
        let candidate = session_message_requested_event(&identities.requested, request);
        validate_event_identity(&candidate)?;
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|database| database_error("begin session message", database))?;
        let retained = retained_session_message(&transaction, &candidate)?;
        let mut events = Vec::new();
        let mut changed = false;
        if retained.is_none() {
            if let Some(wait) = active_wait_for_session(&transaction, &request.session_id)? {
                let outcome = cancel_resource_in_transaction(
                    &transaction,
                    &wait.handle,
                    Some("The user continued the session."),
                    Some(&request.agent_id),
                    Some(request.session_id.as_str()),
                    &identities.wait_cancelled,
                    ResourceKind::Wait,
                )?;
                if let Some(event) = outcome.event {
                    changed = true;
                    events.push(event);
                }
            }
        }
        let (requested, requested_inserted) = match retained {
            Some(retained) => (retained, false),
            None => {
                let outcome = append_lifecycle_candidate(&transaction, candidate)?;
                if !outcome.inserted {
                    return Err(DispatchError::RuntimeEventIdentityCollision {
                        event_id: identities.requested.id().to_owned(),
                    });
                }
                index_event(&transaction, &outcome.event)?;
                (outcome.event, true)
            }
        };
        changed |= requested_inserted;
        events.push(requested.clone());
        let queue_item_id = queue_item_id(&requested.id, &request.agent_id);
        let available = queue_lifecycle_event(
            &identities.available,
            &requested,
            QueueLifecycleFields {
                queue_item_id: &queue_item_id,
                target_agent: &request.agent_id,
                status: QueueItemStatus::Available,
                session_id: Some(&request.session_id),
                project_generation: Some(&request.project_generation),
                lock_keys: &[],
            },
        );
        let available = append_runtime_event(&transaction, available)?;
        changed |= available.inserted;
        events.push(available.event);
        transaction
            .commit()
            .map_err(|database| database_error("commit session message", database))?;
        Ok(SubmittedSessionMessage {
            event_id: requested.id,
            queue_item_id,
            agent_id: request.agent_id.clone(),
            session_id: request.session_id.clone(),
            run_id: request.run_id.clone(),
            changed,
            events,
        })
    }
}

fn validate_session_message_request(request: &SessionMessageRequest) -> Result<(), DispatchError> {
    for (field, value) in [
        ("message", request.message.as_str()),
        ("agent_id", request.agent_id.as_str()),
        ("project_generation", request.project_generation.as_str()),
    ] {
        if value.is_empty() {
            return Err(DispatchError::InvalidCoordinationInput { field });
        }
    }
    if request.idempotency_key.as_deref() == Some("") {
        return Err(DispatchError::InvalidCoordinationInput {
            field: "idempotency_key",
        });
    }
    Ok(())
}

fn session_message_requested_event(
    identity: &RuntimeEventIdentity,
    request: &SessionMessageRequest,
) -> Event {
    let mut payload = Map::new();
    payload.insert("message".to_owned(), Value::String(request.message.clone()));
    payload.insert(
        "agent_id".to_owned(),
        Value::String(request.agent_id.clone()),
    );
    payload.insert(
        "session_id".to_owned(),
        Value::String(request.session_id.to_string()),
    );
    payload.insert(
        "run_id".to_owned(),
        Value::String(request.run_id.to_string()),
    );
    Event {
        id: identity.id().to_owned(),
        event_type: "session.message.requested".to_owned(),
        source: "user".to_owned(),
        payload,
        idempotency_key: request.idempotency_key.clone(),
        caused_by: None,
        session_id: Some(request.session_id.to_string()),
        run_id: Some(request.run_id.to_string()),
        turn_id: None,
        timestamp_ms: identity.timestamp_ms(),
        cursor: None,
    }
}

fn retained_session_message(
    connection: &Connection,
    candidate: &Event,
) -> Result<Option<Event>, DispatchError> {
    let by_id = entry_by_field(connection, "event_id", &candidate.id)?.map(|entry| entry.event);
    let by_key = match &candidate.idempotency_key {
        Some(key) => entry_by_field(connection, "idempotency_key", key)?.map(|entry| entry.event),
        None => None,
    };
    if by_id
        .as_ref()
        .zip(by_key.as_ref())
        .is_some_and(|(by_id, by_key)| by_id.id != by_key.id)
    {
        return Err(DispatchError::RuntimeEventIdentityCollision {
            event_id: candidate.id.clone(),
        });
    }
    let retained = by_key.or(by_id);
    if retained
        .as_ref()
        .is_some_and(|retained| !same_lifecycle_intention(candidate, retained))
    {
        return Err(DispatchError::RuntimeEventIdentityCollision {
            event_id: candidate.id.clone(),
        });
    }
    Ok(retained)
}

#[derive(Default)]
struct SessionSources {
    owners: BTreeSet<String>,
    queue_items: Vec<QueueItem>,
    attempts: Vec<Attempt>,
    waits: Vec<Wait>,
}

fn project_sessions(
    queue_items: Vec<QueueItem>,
    attempts: Vec<Attempt>,
    waits: Vec<Wait>,
) -> Result<Vec<Session>, DispatchError> {
    let mut sources = BTreeMap::<SessionId, SessionSources>::new();
    for item in queue_items {
        let Some(session_id) = item.session_id.clone() else {
            continue;
        };
        let source = sources.entry(session_id).or_default();
        if !item.target_agent.is_empty() {
            source.owners.insert(item.target_agent.clone());
        }
        source.queue_items.push(item);
    }
    for attempt in attempts {
        let Some(session_id) = attempt.session_id.clone() else {
            continue;
        };
        let source = sources.entry(session_id).or_default();
        if !attempt.target_agent.is_empty() {
            source.owners.insert(attempt.target_agent.clone());
        }
        source.attempts.push(attempt);
    }
    for wait in waits {
        let source = sources.entry(wait.session_id.clone()).or_default();
        if !wait.agent_id.is_empty() {
            source.owners.insert(wait.agent_id.clone());
        }
        source.waits.push(wait);
    }

    let mut sessions = Vec::with_capacity(sources.len());
    for (session_id, source) in sources {
        sessions.push(session_from_sources(session_id, source)?);
    }
    sessions.sort_by(|(left_time, left), (right_time, right)| {
        right_time
            .cmp(left_time)
            .then_with(|| right.session_id.cmp(&left.session_id))
    });
    Ok(sessions
        .into_iter()
        .map(|(_updated_at, session)| session)
        .collect())
}

fn session_from_sources(
    session_id: SessionId,
    source: SessionSources,
) -> Result<(i64, Session), DispatchError> {
    let running: Vec<&Attempt> = source
        .attempts
        .iter()
        .filter(|attempt| attempt.status == AttemptStatus::Running)
        .collect();
    let running_queue_ids: BTreeSet<&str> = running
        .iter()
        .map(|attempt| attempt.queue_item_id.as_str())
        .collect();
    let queued: Vec<&QueueItem> = source
        .queue_items
        .iter()
        .filter(|item| {
            !queue_status_is_session_terminal(item.status)
                && !running_queue_ids.contains(item.id.as_str())
        })
        .collect();
    let active_waits: Vec<&Wait> = source
        .waits
        .iter()
        .filter(|wait| wait.status == WaitStatus::Active)
        .collect();
    let latest_attempt = first_latest_attempt(source.attempts.iter());
    let active_attempt = first_latest_attempt(running.into_iter());
    let active_wait = first_latest_wait(active_waits.into_iter());
    let status = if active_attempt.is_some() {
        SessionActivityStatus::Running
    } else if !queued.is_empty() {
        SessionActivityStatus::Queued
    } else if active_wait.is_some() {
        SessionActivityStatus::Waiting
    } else {
        SessionActivityStatus::Idle
    };
    let cancellation_requested = source.queue_items.iter().any(|item| {
        item.cancellation_requested_event_id.is_some()
            && !queue_status_is_session_terminal(item.status)
    });
    let active_run_id = active_attempt.and_then(|attempt| attempt.run_id.clone());
    let active_wait = active_wait.map(|wait| SessionActiveWait {
        handle: wait.handle.clone(),
        event_type: wait.event_type.clone(),
        fields: wait.fields.clone(),
        deadline_ms: wait.deadline_ms,
    });
    let latest_run = latest_attempt.map(|attempt| SessionLatestRun {
        run_id: attempt.run_id.clone(),
        status: attempt.status,
    });
    let mut update_times = source
        .queue_items
        .iter()
        .map(|item| item.updated_at)
        .chain(source.attempts.iter().map(attempt_session_time))
        .chain(source.waits.iter().map(|wait| wait.updated_at));
    let updated_at_ms = update_times.next().map_or(0, |first| {
        update_times.fold(first, |latest, current| latest.max(current))
    });
    let owners: Vec<String> = source.owners.into_iter().collect();
    let agent_id = (owners.len() == 1).then(|| owners[0].clone());
    let conflicting_agent_ids = if owners.len() > 1 { owners } else { Vec::new() };
    let queued_turns = u64::try_from(queued.len())
        .map_err(|_error| corrupt_projection("sessions", "queued_turns"))?;
    Ok((
        updated_at_ms,
        Session {
            session_id,
            agent_id,
            status,
            cancellation_requested,
            active_run_id,
            queued_turns,
            active_wait,
            latest_run,
            updated_at: format_session_timestamp(updated_at_ms)?,
            conflicting_agent_ids,
        },
    ))
}

fn queue_status_is_session_terminal(status: QueueItemStatus) -> bool {
    matches!(
        status,
        QueueItemStatus::Completed
            | QueueItemStatus::Failed
            | QueueItemStatus::Cancelled
            | QueueItemStatus::DeadLettered
            | QueueItemStatus::Unhandled
    )
}

fn first_latest_attempt<'a>(attempts: impl Iterator<Item = &'a Attempt>) -> Option<&'a Attempt> {
    let mut latest = None;
    let mut latest_time = i64::MIN;
    for attempt in attempts {
        let current_time = attempt_session_time(attempt);
        if latest.is_none() || current_time > latest_time {
            latest = Some(attempt);
            latest_time = current_time;
        }
    }
    latest
}

fn first_latest_wait<'a>(waits: impl Iterator<Item = &'a Wait>) -> Option<&'a Wait> {
    let mut latest = None;
    let mut latest_time = i64::MIN;
    for wait in waits {
        if latest.is_none() || wait.updated_at > latest_time {
            latest = Some(wait);
            latest_time = wait.updated_at;
        }
    }
    latest
}

fn attempt_session_time(attempt: &Attempt) -> i64 {
    let timestamp = attempt
        .finished_at
        .as_deref()
        .filter(|timestamp| !timestamp.is_empty())
        .unwrap_or(&attempt.started_at);
    let Ok(timestamp) = OffsetDateTime::parse(timestamp, &Rfc3339) else {
        return 0;
    };
    i64::try_from(timestamp.unix_timestamp_nanos() / 1_000_000).unwrap_or(0)
}

fn format_session_timestamp(timestamp_ms: i64) -> Result<String, DispatchError> {
    let timestamp = OffsetDateTime::from_unix_timestamp_nanos(i128::from(timestamp_ms) * 1_000_000)
        .map_err(|_error| corrupt_projection("sessions", "updated_at"))?;
    let year = timestamp.year();
    let month = u8::from(timestamp.month());
    let day = timestamp.day();
    let hour = timestamp.hour();
    let minute = timestamp.minute();
    let second = timestamp.second();
    let microsecond = timestamp.nanosecond() / 1_000;
    if microsecond == 0 {
        return Ok(format!(
            "{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}Z"
        ));
    }
    Ok(format!(
        "{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}.{microsecond:06}Z"
    ))
}
