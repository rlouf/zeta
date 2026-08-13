use std::str::FromStr;

use rusqlite::{params, Connection, OptionalExtension, Row};
use serde_json::{Map, Value};
use zeta_journal::Event;

use super::{
    corrupt_projection, database_error, invalid_lifecycle, optional_payload_string,
    required_payload_object, required_payload_string, required_runtime_id, Dispatch, DispatchError,
};
use crate::dispatch::{Effect, EffectDeliverySemantics, EffectStatus};
use crate::identity::QueueItemId;

impl Dispatch {
    /// Returns durable external effects in planning order.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when a projection row cannot be read or
    /// rehydrated as the public typed model.
    pub fn list_effects(&self) -> Result<Vec<Effect>, DispatchError> {
        load_effects(&self.connection)
    }

    /// Returns the first unsafe effect that makes this queue item non-retryable.
    ///
    /// A `started` or `ambiguous` unsafe effect may already have reached its
    /// provider. The caller must fail the new attempt permanently instead of
    /// invoking the agent again.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when the effect projection cannot be read.
    pub fn blocking_unsafe_effect(
        &self,
        queue_item_id: &QueueItemId,
    ) -> Result<Option<String>, DispatchError> {
        self.connection
            .query_row(
                "SELECT effect.effect_key
                 FROM effects AS effect
                 JOIN journal_entries AS planned
                   ON planned.event_id = effect.planned_event_id
                 WHERE effect.queue_item_id = ?1
                   AND effect.semantics = 'unsafe_to_retry'
                   AND effect.status IN ('started', 'ambiguous')
                 ORDER BY planned.cursor ASC, effect.effect_key ASC
                 LIMIT 1",
                params![queue_item_id.as_str()],
                |row| row.get(0),
            )
            .optional()
            .map_err(|database| database_error("read blocking unsafe effect", database))
    }
}

pub(super) fn index_effect(connection: &Connection, event: &Event) -> Result<(), DispatchError> {
    let status = lifecycle_effect_status(event)?;
    let key = required_runtime_id(event, "effect_key")?;
    let operation = required_runtime_id(event, "operation")?;
    let semantics = required_runtime_id(event, "semantics")?;
    let Some(semantics) = parse_effect_semantics(&semantics) else {
        return Err(invalid_lifecycle(event, "semantics"));
    };
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
    let Some(previous) = parse_effect_status(&stored_status) else {
        return Err(invalid_lifecycle(event, "status"));
    };
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

fn lifecycle_effect_status(event: &Event) -> Result<EffectStatus, DispatchError> {
    let suffix = event.event_type.rsplit('.').next().unwrap_or_default();
    let Some(status) = parse_effect_status(suffix) else {
        return Err(invalid_lifecycle(event, "status"));
    };
    let supplied = required_payload_string(event, "status", false)?;
    if supplied != suffix {
        return Err(invalid_lifecycle(event, "status"));
    }
    Ok(status)
}

fn parse_effect_status(value: &str) -> Option<EffectStatus> {
    match value {
        "planned" => Some(EffectStatus::Planned),
        "started" => Some(EffectStatus::Started),
        "completed" => Some(EffectStatus::Completed),
        "failed" => Some(EffectStatus::Failed),
        "ambiguous" => Some(EffectStatus::Ambiguous),
        _ => None,
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

fn effect_status_is_terminal(status: EffectStatus) -> bool {
    matches!(
        status,
        EffectStatus::Completed | EffectStatus::Failed | EffectStatus::Ambiguous
    )
}

fn parse_effect_semantics(value: &str) -> Option<EffectDeliverySemantics> {
    match value {
        "idempotent_with_key" => Some(EffectDeliverySemantics::IdempotentWithKey),
        "connector_deduplicated" => Some(EffectDeliverySemantics::ConnectorDeduplicated),
        "at_least_once" => Some(EffectDeliverySemantics::AtLeastOnce),
        "unsafe_to_retry" => Some(EffectDeliverySemantics::UnsafeToRetry),
        _ => None,
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

const EFFECT_COLUMNS: &str = "effect.effect_key, effect.operation,
    effect.semantics, effect.scope, effect.queue_item_id,
    effect.params_json, effect.status, effect.result_json,
    effect.planned_event_id, effect.terminal_event_id, effect.updated_at";

fn load_effects(connection: &Connection) -> Result<Vec<Effect>, DispatchError> {
    let sql = format!(
        "SELECT {EFFECT_COLUMNS}
         FROM effects AS effect
         JOIN journal_entries AS planned
           ON planned.event_id = effect.planned_event_id
         ORDER BY planned.cursor ASC, effect.effect_key ASC"
    );
    let mut statement = connection
        .prepare(&sql)
        .map_err(|database| database_error("prepare effect read", database))?;
    let rows = statement
        .query_map([], StoredEffect::from_row)
        .map_err(|database| database_error("read effects", database))?;
    let mut effects = Vec::new();
    for row in rows {
        let stored = row.map_err(|database| database_error("read effect", database))?;
        effects.push(stored.into_model()?);
    }
    Ok(effects)
}

struct StoredEffect {
    key: String,
    operation: String,
    semantics: String,
    scope: String,
    queue_item_id: Option<String>,
    params_json: String,
    status: String,
    result_json: Option<String>,
    planned_event_id: String,
    terminal_event_id: Option<String>,
    updated_at: i64,
}

impl StoredEffect {
    fn from_row(row: &Row<'_>) -> rusqlite::Result<Self> {
        Ok(StoredEffect {
            key: row.get(0)?,
            operation: row.get(1)?,
            semantics: row.get(2)?,
            scope: row.get(3)?,
            queue_item_id: row.get(4)?,
            params_json: row.get(5)?,
            status: row.get(6)?,
            result_json: row.get(7)?,
            planned_event_id: row.get(8)?,
            terminal_event_id: row.get(9)?,
            updated_at: row.get(10)?,
        })
    }

    fn into_model(self) -> Result<Effect, DispatchError> {
        let Some(semantics) = parse_effect_semantics(&self.semantics) else {
            return Err(corrupt_projection("effects", "semantics"));
        };
        let queue_item_id = self
            .queue_item_id
            .map(|value| QueueItemId::from_str(&value))
            .transpose()
            .map_err(|_error| corrupt_projection("effects", "queue_item_id"))?;
        let params = match serde_json::from_str(&self.params_json) {
            Ok(Value::Object(params)) => params,
            _ => return Err(corrupt_projection("effects", "params_json")),
        };
        let Some(status) = parse_effect_status(&self.status) else {
            return Err(corrupt_projection("effects", "status"));
        };
        let result = self
            .result_json
            .map(|value| match serde_json::from_str(&value) {
                Ok(Value::Object(result)) => Ok(result),
                _ => Err(corrupt_projection("effects", "result_json")),
            })
            .transpose()?;
        if effect_status_is_terminal(status) != result.is_some() {
            return Err(corrupt_projection("effects", "result_json"));
        }
        Ok(Effect {
            key: self.key,
            operation: self.operation,
            semantics,
            scope: self.scope,
            queue_item_id,
            params,
            status,
            result,
            planned_event_id: self.planned_event_id,
            terminal_event_id: self.terminal_event_id,
            updated_at: self.updated_at,
        })
    }
}
