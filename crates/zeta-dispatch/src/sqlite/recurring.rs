use rusqlite::{Connection, Row, TransactionBehavior};
use serde_json::{Map, Value};
use time::{format_description::well_known::Rfc3339, OffsetDateTime};
use zeta_journal::Event;

use super::journal::{append_runtime_event, entry_by_field, validate_distinct_runtime_identities};
use super::{corrupt_projection, database_error, Dispatch, DispatchError};
use crate::dispatch::{
    RecurringSchedule, RecurringScheduleStatus, RecurringScheduleTick, RuntimeEventIdentity,
    ScheduleTickStatus,
};

impl Dispatch {
    /// Returns the latest durable state of every observed recurring schedule.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when a projection row cannot be read or
    /// rehydrated as the public typed model.
    pub fn list_recurring_schedules(&self) -> Result<Vec<RecurringScheduleStatus>, DispatchError> {
        load_recurring_schedules(&self.connection)
    }

    /// Atomically publishes one caller-resolved recurring occurrence.
    ///
    /// Calendar evaluation stays outside persistence: the caller selects the
    /// occurrence, observation, next occurrence, reason, and any explicit
    /// activation fact. Dispatch applies no cron or catch-up policy; it only
    /// persists the supplied batch. Repeating an already decided occurrence
    /// returns an empty vector without requiring new identities.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError::InvalidScheduleTick`] for malformed resolved
    /// input, or another [`DispatchError`] for identity collisions, projection
    /// failures, and storage failures. Every error rolls back the complete
    /// occurrence.
    pub fn publish_recurring_schedule_tick(
        &mut self,
        tick: &RecurringScheduleTick,
        identities: &[RuntimeEventIdentity],
    ) -> Result<Vec<Event>, DispatchError> {
        validate_recurring_schedule_tick(tick)?;
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|database| database_error("begin recurring schedule tick", database))?;
        let decision_key = recurring_schedule_tick_idempotency_key(tick, "published");
        if entry_by_field(&transaction, "idempotency_key", &decision_key)?.is_some() {
            if !identities.is_empty() {
                return Err(DispatchError::RuntimeEventIdentityCount {
                    expected: 0,
                    actual: identities.len(),
                });
            }
            transaction
                .commit()
                .map_err(|database| database_error("commit repeated schedule tick", database))?;
            return Ok(Vec::new());
        }
        let activation_required = tick.activation.is_some();
        let expected_identities = if activation_required { 3 } else { 2 };
        if identities.len() != expected_identities {
            return Err(DispatchError::RuntimeEventIdentityCount {
                expected: expected_identities,
                actual: identities.len(),
            });
        }
        validate_distinct_runtime_identities(identities)?;
        let mut position = 0;
        let mut events = Vec::with_capacity(expected_identities);
        if activation_required {
            let activation = recurring_schedule_activation_event(&identities[position], tick);
            events.push(append_runtime_event(&transaction, activation)?.event);
            position += 1;
        }
        let publication = recurring_schedule_publication_event(&identities[position], tick)?;
        let publication = append_runtime_event(&transaction, publication)?.event;
        position += 1;
        events.push(publication.clone());
        let decision = recurring_schedule_decision_event(
            &identities[position],
            tick,
            &publication,
            &decision_key,
        );
        events.push(append_runtime_event(&transaction, decision)?.event);
        transaction
            .commit()
            .map_err(|database| database_error("commit recurring schedule tick", database))?;
        Ok(events)
    }
}

fn validate_recurring_schedule_tick(tick: &RecurringScheduleTick) -> Result<(), DispatchError> {
    let schedule = &tick.schedule;
    if schedule.agent_id.is_empty() {
        return Err(invalid_schedule_tick("agent_id"));
    }
    if schedule.cron.is_empty() {
        return Err(invalid_schedule_tick("cron"));
    }
    if schedule.timezone.as_deref() == Some("") {
        return Err(invalid_schedule_tick("timezone"));
    }
    if schedule.schedule_index > i64::MAX as u64 {
        return Err(invalid_schedule_tick("schedule_index"));
    }
    if tick.reason.is_empty() {
        return Err(invalid_schedule_tick("reason"));
    }
    if tick
        .activation
        .as_ref()
        .is_some_and(|activation| activation.catchup.is_empty() || activation.reason.is_empty())
    {
        return Err(invalid_schedule_tick("activation"));
    }
    let scheduled_at = parse_schedule_timestamp(&tick.scheduled_at, "scheduled_at")?;
    let observed_at = parse_schedule_timestamp(&tick.observed_at, "observed_at")?;
    let next_at = parse_schedule_timestamp(&tick.next_at, "next_at")?;
    if scheduled_at.second() != 0 || scheduled_at.nanosecond() != 0 {
        return Err(invalid_schedule_tick("scheduled_at"));
    }
    if observed_at < scheduled_at {
        return Err(invalid_schedule_tick("observed_at"));
    }
    if next_at <= scheduled_at {
        return Err(invalid_schedule_tick("next_at"));
    }
    Ok(())
}

fn parse_schedule_timestamp(
    value: &str,
    field: &'static str,
) -> Result<OffsetDateTime, DispatchError> {
    OffsetDateTime::parse(value, &Rfc3339).map_err(|_error| invalid_schedule_tick(field))
}

fn invalid_schedule_tick(field: &'static str) -> DispatchError {
    DispatchError::InvalidScheduleTick { field }
}

fn recurring_schedule_event_type(schedule: &RecurringSchedule) -> String {
    format!("agent.{}.scheduled", schedule.agent_id)
}

fn recurring_schedule_timezone(schedule: &RecurringSchedule) -> &str {
    schedule.timezone.as_deref().unwrap_or("")
}

fn recurring_schedule_activation_key(tick: &RecurringScheduleTick) -> String {
    let activation = tick
        .activation
        .as_ref()
        .expect("validated explicit activation metadata");
    format!(
        "scheduler:activated:{}:{}:{}:{}:{}",
        tick.schedule.agent_id,
        tick.schedule.schedule_index,
        tick.schedule.cron,
        recurring_schedule_timezone(&tick.schedule),
        activation.catchup
    )
}

fn recurring_schedule_tick_idempotency_key(tick: &RecurringScheduleTick, status: &str) -> String {
    format!(
        "scheduler:{status}:{}:{}:{}:{}:{}",
        tick.schedule.agent_id,
        tick.schedule.schedule_index,
        tick.schedule.cron,
        recurring_schedule_timezone(&tick.schedule),
        tick.scheduled_at
    )
}

fn recurring_schedule_publication_idempotency_key(tick: &RecurringScheduleTick) -> String {
    format!(
        "schedule:{}:{}:{}",
        tick.schedule.agent_id, tick.schedule.cron, tick.scheduled_at
    )
}

fn recurring_schedule_activation_event(
    identity: &RuntimeEventIdentity,
    tick: &RecurringScheduleTick,
) -> Event {
    let schedule = &tick.schedule;
    let activation = tick
        .activation
        .as_ref()
        .expect("validated explicit activation metadata");
    let mut payload = Map::new();
    payload.insert("agent".to_owned(), Value::String(schedule.agent_id.clone()));
    payload.insert(
        "schedule_index".to_owned(),
        Value::from(schedule.schedule_index),
    );
    payload.insert(
        "event_type".to_owned(),
        Value::String(recurring_schedule_event_type(schedule)),
    );
    payload.insert("cron".to_owned(), Value::String(schedule.cron.clone()));
    payload.insert(
        "timezone".to_owned(),
        schedule
            .timezone
            .clone()
            .map(Value::String)
            .unwrap_or(Value::Null),
    );
    payload.insert(
        "catchup".to_owned(),
        Value::String(activation.catchup.clone()),
    );
    payload.insert(
        "observed_at".to_owned(),
        Value::String(tick.observed_at.clone()),
    );
    payload.insert("status".to_owned(), Value::String("activated".to_owned()));
    payload.insert(
        "reason".to_owned(),
        Value::String(activation.reason.clone()),
    );
    Event {
        id: identity.id().to_owned(),
        event_type: "scheduler.tick.activated".to_owned(),
        source: "zeta:scheduler".to_owned(),
        payload,
        idempotency_key: Some(recurring_schedule_activation_key(tick)),
        caused_by: None,
        session_id: None,
        run_id: None,
        turn_id: None,
        timestamp_ms: identity.timestamp_ms(),
        cursor: None,
    }
}

fn recurring_schedule_publication_event(
    identity: &RuntimeEventIdentity,
    tick: &RecurringScheduleTick,
) -> Result<Event, DispatchError> {
    let scheduled_at = parse_schedule_timestamp(&tick.scheduled_at, "scheduled_at")?;
    let mut payload = Map::new();
    payload.insert(
        "date".to_owned(),
        Value::String(scheduled_at.date().to_string()),
    );
    payload.insert(
        "timestamp".to_owned(),
        Value::String(tick.scheduled_at.clone()),
    );
    Ok(Event {
        id: identity.id().to_owned(),
        event_type: recurring_schedule_event_type(&tick.schedule),
        source: "zeta:scheduler".to_owned(),
        payload,
        idempotency_key: Some(recurring_schedule_publication_idempotency_key(tick)),
        caused_by: None,
        session_id: None,
        run_id: None,
        turn_id: None,
        timestamp_ms: identity.timestamp_ms(),
        cursor: None,
    })
}

fn recurring_schedule_decision_event(
    identity: &RuntimeEventIdentity,
    tick: &RecurringScheduleTick,
    published: &Event,
    idempotency_key: &str,
) -> Event {
    let schedule = &tick.schedule;
    let mut payload = Map::new();
    payload.insert("agent".to_owned(), Value::String(schedule.agent_id.clone()));
    payload.insert(
        "schedule_index".to_owned(),
        Value::from(schedule.schedule_index),
    );
    payload.insert(
        "event_type".to_owned(),
        Value::String(recurring_schedule_event_type(schedule)),
    );
    payload.insert("cron".to_owned(), Value::String(schedule.cron.clone()));
    payload.insert(
        "timezone".to_owned(),
        schedule
            .timezone
            .clone()
            .map(Value::String)
            .unwrap_or(Value::Null),
    );
    payload.insert(
        "scheduled_at".to_owned(),
        Value::String(tick.scheduled_at.clone()),
    );
    payload.insert(
        "observed_at".to_owned(),
        Value::String(tick.observed_at.clone()),
    );
    payload.insert("next_at".to_owned(), Value::String(tick.next_at.clone()));
    payload.insert("status".to_owned(), Value::String("published".to_owned()));
    payload.insert("reason".to_owned(), Value::String(tick.reason.clone()));
    payload.insert(
        "published_event_id".to_owned(),
        Value::String(published.id.clone()),
    );
    Event {
        id: identity.id().to_owned(),
        event_type: "scheduler.tick.published".to_owned(),
        source: "zeta:scheduler".to_owned(),
        payload,
        idempotency_key: Some(idempotency_key.to_owned()),
        caused_by: Some(published.id.clone()),
        session_id: None,
        run_id: None,
        turn_id: None,
        timestamp_ms: identity.timestamp_ms(),
        cursor: None,
    }
}

fn load_recurring_schedules(
    connection: &Connection,
) -> Result<Vec<RecurringScheduleStatus>, DispatchError> {
    let mut statement = connection
        .prepare(
            "SELECT agent_id, schedule_index, cron, timezone,
                    event_type, status, last_published_at, next_at,
                    reason, updated_at
             FROM recurring_schedules
             ORDER BY agent_id ASC, schedule_index ASC, cron ASC, timezone ASC",
        )
        .map_err(|database| database_error("prepare recurring schedule read", database))?;
    let rows = statement
        .query_map([], StoredRecurringSchedule::from_row)
        .map_err(|database| database_error("read recurring schedules", database))?;
    let mut schedules = Vec::new();
    for row in rows {
        let stored = row.map_err(|database| database_error("read recurring schedule", database))?;
        schedules.push(stored.into_model()?);
    }
    Ok(schedules)
}

struct StoredRecurringSchedule {
    agent_id: String,
    schedule_index: i64,
    cron: String,
    timezone: String,
    event_type: String,
    status: String,
    last_published_at: Option<String>,
    next_at: Option<String>,
    reason: String,
    updated_at: i64,
}

impl StoredRecurringSchedule {
    fn from_row(row: &Row<'_>) -> rusqlite::Result<Self> {
        Ok(StoredRecurringSchedule {
            agent_id: row.get(0)?,
            schedule_index: row.get(1)?,
            cron: row.get(2)?,
            timezone: row.get(3)?,
            event_type: row.get(4)?,
            status: row.get(5)?,
            last_published_at: row.get(6)?,
            next_at: row.get(7)?,
            reason: row.get(8)?,
            updated_at: row.get(9)?,
        })
    }

    fn into_model(self) -> Result<RecurringScheduleStatus, DispatchError> {
        let schedule_index = u64::try_from(self.schedule_index)
            .map_err(|_error| corrupt_projection("recurring_schedules", "schedule_index"))?;
        let timezone = (!self.timezone.is_empty()).then_some(self.timezone);
        let status = match self.status.as_str() {
            "activated" => ScheduleTickStatus::Activated,
            "published" => ScheduleTickStatus::Published,
            "skipped" => ScheduleTickStatus::Skipped,
            "missed" => ScheduleTickStatus::Missed,
            _ => return Err(corrupt_projection("recurring_schedules", "status")),
        };
        if self.agent_id.is_empty()
            || self.cron.is_empty()
            || self.event_type != format!("agent.{}.scheduled", self.agent_id)
            || self.reason.is_empty()
        {
            return Err(corrupt_projection(
                "recurring_schedules",
                "schedule_identity",
            ));
        }
        Ok(RecurringScheduleStatus {
            schedule: RecurringSchedule {
                agent_id: self.agent_id,
                schedule_index,
                cron: self.cron,
                timezone,
            },
            event_type: self.event_type,
            status,
            last_published_at: self.last_published_at,
            next_at: self.next_at,
            reason: self.reason,
            updated_at: self.updated_at,
        })
    }
}
