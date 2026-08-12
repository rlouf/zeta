use std::str::FromStr;

use rusqlite::{params, Connection, OptionalExtension, Row};
use serde_json::Value;

use super::projection::effect_status_is_terminal;
use super::{corrupt_projection, database_error, Dispatch, DispatchError};
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
        let semantics = match self.semantics.as_str() {
            "idempotent_with_key" => EffectDeliverySemantics::IdempotentWithKey,
            "connector_deduplicated" => EffectDeliverySemantics::ConnectorDeduplicated,
            "at_least_once" => EffectDeliverySemantics::AtLeastOnce,
            "unsafe_to_retry" => EffectDeliverySemantics::UnsafeToRetry,
            _ => return Err(corrupt_projection("effects", "semantics")),
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
        let status = match self.status.as_str() {
            "planned" => EffectStatus::Planned,
            "started" => EffectStatus::Started,
            "completed" => EffectStatus::Completed,
            "failed" => EffectStatus::Failed,
            "ambiguous" => EffectStatus::Ambiguous,
            _ => return Err(corrupt_projection("effects", "status")),
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
