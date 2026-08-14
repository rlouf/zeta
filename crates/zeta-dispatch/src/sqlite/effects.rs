use std::str::FromStr;

use rusqlite::{params, Connection, OptionalExtension, Row, TransactionBehavior};
use serde_json::{Map, Value};
use zeta_journal::Event;

use super::journal::append_runtime_event;
use super::{
    corrupt_projection, database_error, invalid_lifecycle, optional_payload_string,
    required_payload_object, required_payload_string, required_runtime_id, Dispatch, DispatchError,
};
use crate::dispatch::{
    Effect, EffectDeliverySemantics, EffectStatus, EgressDeliveryClaim, RuntimeEventIdentity,
};
use crate::identity::{ClaimToken, QueueItemId};

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

    /// Claims one ready connector effect in durable planning order.
    ///
    /// The claim only admits planned work or a retry-safe failed effect whose
    /// retry time has passed. It never claims an unsafe ambiguous effect.
    pub fn claim_next_egress_delivery(
        &mut self,
        worker_name: &str,
        token: ClaimToken,
        lease_ms: u64,
        now_ms: i64,
    ) -> Result<Option<(EgressDeliveryClaim, Effect)>, DispatchError> {
        if worker_name.is_empty() || lease_ms == 0 {
            return Err(DispatchError::InvalidCoordinationInput {
                field: "egress_claim",
            });
        }
        let lease_ms =
            i64::try_from(lease_ms).map_err(|_error| DispatchError::InvalidCoordinationInput {
                field: "egress_claim",
            })?;
        let Some(claimed_until) = now_ms.checked_add(lease_ms) else {
            return Err(DispatchError::InvalidCoordinationInput {
                field: "egress_claim",
            });
        };
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|database| database_error("begin egress delivery claim", database))?;
        let sql = format!(
            "SELECT {EFFECT_COLUMNS}
             FROM effects AS effect
             JOIN journal_entries AS planned
               ON planned.event_id = effect.planned_event_id
             LEFT JOIN effect_claims AS claim
               ON claim.effect_key = effect.effect_key
             WHERE claim.effect_key IS NULL
               AND effect.operation LIKE 'connector:%'
               AND (
                    effect.status = 'planned'
                    OR (effect.status = 'failed' AND effect.available_at <= ?1)
                    OR (
                        effect.status = 'started'
                        AND effect.semantics != 'unsafe_to_retry'
                    )
               )
             ORDER BY planned.cursor ASC, effect.effect_key ASC
             LIMIT 1"
        );
        let stored = transaction
            .query_row(&sql, params![now_ms], StoredEffect::from_row)
            .optional()
            .map_err(|database| database_error("read ready egress delivery", database))?;
        let Some(stored) = stored else {
            transaction.commit().map_err(|database| {
                database_error("commit empty egress delivery claim", database)
            })?;
            return Ok(None);
        };
        let effect = stored.into_model()?;
        transaction
            .execute(
                "INSERT INTO effect_claims (
                    effect_key, worker_name, claim_token, claimed_at, claimed_until
                 ) VALUES (?1, ?2, ?3, ?4, ?5)",
                params![
                    effect.key(),
                    worker_name,
                    token.as_str(),
                    now_ms,
                    claimed_until,
                ],
            )
            .map_err(|database| database_error("create egress delivery claim", database))?;
        transaction
            .commit()
            .map_err(|database| database_error("commit egress delivery claim", database))?;
        Ok(Some((
            EgressDeliveryClaim {
                effect_key: effect.key().to_owned(),
                worker_name: worker_name.to_owned(),
                token,
                claimed_until,
            },
            effect,
        )))
    }

    /// Releases expired connector delivery claims for recovery.
    pub fn reconcile_expired_egress_delivery_claims(
        &mut self,
        now_ms: i64,
    ) -> Result<usize, DispatchError> {
        let changed = self
            .connection
            .execute(
                "DELETE FROM effect_claims WHERE claimed_until < ?1",
                params![now_ms],
            )
            .map_err(|database| {
                database_error("recover expired egress delivery claims", database)
            })?;
        Ok(changed)
    }

    /// Returns the next egress retry or claim-expiry deadline.
    pub fn next_egress_deadline_ms(&self, now_ms: i64) -> Result<Option<i64>, DispatchError> {
        let ready = self
            .connection
            .query_row(
                "SELECT MIN(effect.available_at)
                 FROM effects AS effect
                 LEFT JOIN effect_claims AS claim
                   ON claim.effect_key = effect.effect_key
                 WHERE claim.effect_key IS NULL
                   AND effect.operation LIKE 'connector:%'
                   AND effect.status = 'failed'
                   AND effect.available_at > ?1",
                params![now_ms],
                |row| row.get::<_, Option<i64>>(0),
            )
            .map_err(|database| database_error("read next egress retry", database))?;
        let claim = self
            .connection
            .query_row("SELECT MIN(claimed_until) FROM effect_claims", [], |row| {
                row.get::<_, Option<i64>>(0)
            })
            .map_err(|database| database_error("read next egress claim expiry", database))?;
        Ok([ready, claim].into_iter().flatten().min())
    }

    /// Records the external delivery barrier under one live egress claim.
    pub fn start_claimed_egress_delivery(
        &mut self,
        claim: &EgressDeliveryClaim,
        now_ms: i64,
        identity: RuntimeEventIdentity,
    ) -> Result<Effect, DispatchError> {
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|database| database_error("begin egress delivery start", database))?;
        let effect = claimed_effect(&transaction, claim, now_ms)?;
        if effect.status() == EffectStatus::Started {
            transaction
                .commit()
                .map_err(|database| database_error("commit recovered egress delivery", database))?;
            return Ok(effect);
        }
        let attempt = effect.delivery_attempts().checked_add(1).ok_or(
            DispatchError::InvalidCoordinationInput {
                field: "delivery_attempt",
            },
        )?;
        let event = effect_event(
            &effect,
            EffectStatus::Started,
            &identity,
            attempt,
            None,
            None,
        );
        let _event = append_runtime_event(&transaction, event)?.event;
        let updated =
            load_effect(&transaction, effect.key())?.ok_or(DispatchError::CorruptProjection {
                table: "effects",
                field: "effect_key",
            })?;
        transaction
            .commit()
            .map_err(|database| database_error("commit egress delivery start", database))?;
        Ok(updated)
    }

    /// Renews one live connector delivery claim.
    pub fn renew_egress_delivery_claim(
        &mut self,
        claim: &EgressDeliveryClaim,
        lease_ms: u64,
        now_ms: i64,
    ) -> Result<bool, DispatchError> {
        let lease_ms =
            i64::try_from(lease_ms).map_err(|_error| DispatchError::InvalidCoordinationInput {
                field: "egress_claim",
            })?;
        let Some(claimed_until) = now_ms.checked_add(lease_ms) else {
            return Err(DispatchError::InvalidCoordinationInput {
                field: "egress_claim",
            });
        };
        let changed = self
            .connection
            .execute(
                "UPDATE effect_claims
                 SET claimed_until = ?1
                 WHERE effect_key = ?2 AND claim_token = ?3 AND claimed_until >= ?4",
                params![
                    claimed_until,
                    claim.effect_key(),
                    claim.token().as_str(),
                    now_ms
                ],
            )
            .map_err(|database| database_error("renew egress delivery claim", database))?;
        Ok(changed > 0)
    }

    /// Completes one started connector effect and releases its claim.
    pub fn complete_claimed_egress_delivery(
        &mut self,
        claim: &EgressDeliveryClaim,
        now_ms: i64,
        identity: RuntimeEventIdentity,
        result: Map<String, Value>,
    ) -> Result<(), DispatchError> {
        self.finish_claimed_egress_delivery(
            claim,
            now_ms,
            identity,
            EffectStatus::Completed,
            result,
            None,
        )
    }

    /// Fails one started connector effect and optionally records its retry time.
    pub fn fail_claimed_egress_delivery(
        &mut self,
        claim: &EgressDeliveryClaim,
        now_ms: i64,
        identity: RuntimeEventIdentity,
        result: Map<String, Value>,
        retry_at: Option<i64>,
    ) -> Result<(), DispatchError> {
        self.finish_claimed_egress_delivery(
            claim,
            now_ms,
            identity,
            EffectStatus::Failed,
            result,
            retry_at,
        )
    }

    /// Marks one unsafe connector effect ambiguous and releases its claim.
    pub fn mark_claimed_egress_delivery_ambiguous(
        &mut self,
        claim: &EgressDeliveryClaim,
        now_ms: i64,
        identity: RuntimeEventIdentity,
        result: Map<String, Value>,
    ) -> Result<(), DispatchError> {
        self.finish_claimed_egress_delivery(
            claim,
            now_ms,
            identity,
            EffectStatus::Ambiguous,
            result,
            None,
        )
    }

    fn finish_claimed_egress_delivery(
        &mut self,
        claim: &EgressDeliveryClaim,
        now_ms: i64,
        identity: RuntimeEventIdentity,
        status: EffectStatus,
        result: Map<String, Value>,
        retry_at: Option<i64>,
    ) -> Result<(), DispatchError> {
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|database| database_error("begin egress delivery finish", database))?;
        let effect = claimed_effect(&transaction, claim, now_ms)?;
        if effect.status() != EffectStatus::Started {
            return Err(DispatchError::InvalidCoordinationInput {
                field: "egress_effect_status",
            });
        }
        if status == EffectStatus::Failed
            && effect.semantics() == EffectDeliverySemantics::UnsafeToRetry
        {
            return Err(DispatchError::InvalidCoordinationInput {
                field: "egress_effect_status",
            });
        }
        let event = effect_event(
            &effect,
            status,
            &identity,
            effect.delivery_attempts(),
            Some(result),
            retry_at,
        );
        let _event = append_runtime_event(&transaction, event)?.event;
        transaction
            .execute(
                "DELETE FROM effect_claims WHERE effect_key = ?1 AND claim_token = ?2",
                params![claim.effect_key(), claim.token().as_str()],
            )
            .map_err(|database| database_error("release egress delivery claim", database))?;
        transaction
            .commit()
            .map_err(|database| database_error("commit egress delivery finish", database))?;
        Ok(())
    }
}

fn claimed_effect(
    connection: &Connection,
    claim: &EgressDeliveryClaim,
    now_ms: i64,
) -> Result<Effect, DispatchError> {
    let current = connection
        .query_row(
            "SELECT 1 FROM effect_claims
             WHERE effect_key = ?1 AND claim_token = ?2 AND claimed_until >= ?3",
            params![claim.effect_key(), claim.token().as_str(), now_ms],
            |_row| Ok(()),
        )
        .optional()
        .map_err(|database| database_error("read egress delivery claim", database))?;
    if current.is_none() {
        return Err(DispatchError::InvalidCoordinationInput {
            field: "egress_claim",
        });
    }
    load_effect(connection, claim.effect_key())?.ok_or(DispatchError::CorruptProjection {
        table: "effects",
        field: "effect_key",
    })
}

fn load_effect(connection: &Connection, key: &str) -> Result<Option<Effect>, DispatchError> {
    let sql = format!(
        "SELECT {EFFECT_COLUMNS}
         FROM effects AS effect
         WHERE effect.effect_key = ?1"
    );
    let stored = connection
        .query_row(&sql, params![key], StoredEffect::from_row)
        .optional()
        .map_err(|database| database_error("read effect", database))?;
    stored.map(StoredEffect::into_model).transpose()
}

fn effect_event(
    effect: &Effect,
    status: EffectStatus,
    identity: &RuntimeEventIdentity,
    delivery_attempt: u32,
    result: Option<Map<String, Value>>,
    retry_at: Option<i64>,
) -> Event {
    let mut payload = Map::new();
    payload.insert("effect_key".to_owned(), Value::String(effect.key.clone()));
    payload.insert(
        "operation".to_owned(),
        Value::String(effect.operation.clone()),
    );
    payload.insert(
        "semantics".to_owned(),
        Value::String(effect_semantics_str(effect.semantics).to_owned()),
    );
    payload.insert("scope".to_owned(), Value::String(effect.scope.clone()));
    payload.insert(
        "queue_item_id".to_owned(),
        effect
            .queue_item_id
            .as_ref()
            .map(|value| Value::String(value.to_string()))
            .unwrap_or(Value::Null),
    );
    payload.insert("params".to_owned(), Value::Object(effect.params.clone()));
    payload.insert(
        "status".to_owned(),
        Value::String(effect_status_str(status).to_owned()),
    );
    payload.insert("delivery_attempt".to_owned(), Value::from(delivery_attempt));
    if let Some(result) = result {
        payload.insert("result".to_owned(), Value::Object(result));
    }
    if let Some(retry_at) = retry_at {
        payload.insert("retry_at".to_owned(), Value::from(retry_at));
    }
    Event {
        id: identity.id().to_owned(),
        event_type: format!("runtime.effect.{}", effect_status_str(status)),
        source: "zeta".to_owned(),
        payload,
        idempotency_key: Some(effect_idempotency_key(
            status,
            effect.key(),
            Some(delivery_attempt),
        )),
        caused_by: Some(effect.scope.clone()),
        session_id: None,
        run_id: None,
        turn_id: None,
        timestamp_ms: identity.timestamp_ms(),
        cursor: None,
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
    let delivery_attempt = optional_payload_u32(event, "delivery_attempt")?;
    let retry_at = optional_payload_i64(event, "retry_at")?;
    let caused_by = event
        .caused_by
        .as_deref()
        .filter(|caused_by| !caused_by.is_empty())
        .ok_or_else(|| invalid_lifecycle(event, "caused_by"))?;
    let expected_idempotency_key = effect_idempotency_key(status, &key, delivery_attempt);
    if event.idempotency_key.as_deref() != Some(expected_idempotency_key.as_str()) {
        return Err(invalid_lifecycle(event, "idempotency_key"));
    }
    validate_effect_result(event, status, semantics, result.as_ref())?;

    if status == EffectStatus::Planned {
        if delivery_attempt.is_some() || retry_at.is_some() {
            return Err(invalid_lifecycle(event, "delivery_attempt"));
        }
        connection
            .execute(
                "INSERT INTO effects (
                    effect_key, operation, semantics, scope, queue_item_id,
                    params_json, status, result_json, caused_by,
                    planned_event_id, terminal_event_id, updated_at,
                    delivery_attempts, available_at
                 ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, 'planned', NULL,
                           ?7, ?8, NULL, ?9, 0, ?9)",
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
                    params_json, status, caused_by, delivery_attempts
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
                    row.get::<_, i64>(7)?,
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
        stored_delivery_attempts,
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
    let stored_delivery_attempts = u32::try_from(stored_delivery_attempts)
        .map_err(|_error| invalid_lifecycle(event, "delivery_attempt"))?;
    let next_delivery_attempts = if status == EffectStatus::Started {
        match delivery_attempt {
            Some(delivery_attempt) => {
                if delivery_attempt != stored_delivery_attempts.saturating_add(1) {
                    return Err(invalid_lifecycle(event, "delivery_attempt"));
                }
                delivery_attempt
            }
            None => stored_delivery_attempts,
        }
    } else {
        if delivery_attempt.is_some_and(|value| value != stored_delivery_attempts) {
            return Err(invalid_lifecycle(event, "delivery_attempt"));
        }
        stored_delivery_attempts
    };
    if status != EffectStatus::Failed && retry_at.is_some() {
        return Err(invalid_lifecycle(event, "retry_at"));
    }
    let result_json = result
        .map(|result| serde_json::to_string(&result))
        .transpose()
        .map_err(|_error| invalid_lifecycle(event, "result"))?;
    let terminal_event_id = effect_status_is_terminal(status).then_some(event.id.as_str());
    connection
        .execute(
            "UPDATE effects
             SET status = ?1, result_json = ?2,
                 terminal_event_id = ?3, updated_at = ?4,
                 delivery_attempts = ?5, available_at = ?6
             WHERE effect_key = ?7",
            params![
                effect_status_str(status),
                result_json,
                terminal_event_id,
                event.timestamp_ms,
                next_delivery_attempts,
                retry_at,
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
            | (EffectStatus::Failed, EffectStatus::Started)
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

fn optional_payload_i64(event: &Event, field: &'static str) -> Result<Option<i64>, DispatchError> {
    match event.payload.get(field) {
        Some(Value::Number(value)) => value
            .as_i64()
            .map(Some)
            .ok_or_else(|| invalid_lifecycle(event, field)),
        Some(Value::Null) | None => Ok(None),
        Some(_value) => Err(invalid_lifecycle(event, field)),
    }
}

fn optional_payload_u32(event: &Event, field: &'static str) -> Result<Option<u32>, DispatchError> {
    let Some(value) = optional_payload_i64(event, field)? else {
        return Ok(None);
    };
    let value = u32::try_from(value).map_err(|_error| invalid_lifecycle(event, field))?;
    if value == 0 {
        return Err(invalid_lifecycle(event, field));
    }
    Ok(Some(value))
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

fn effect_idempotency_key(
    status: EffectStatus,
    key: &str,
    delivery_attempt: Option<u32>,
) -> String {
    let base = format!("runtime.effect.{}:{key}", effect_status_str(status));
    match delivery_attempt {
        Some(delivery_attempt) => format!("{base}:{delivery_attempt}"),
        None => base,
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
    effect.planned_event_id, effect.terminal_event_id, effect.updated_at,
    effect.delivery_attempts, effect.available_at";

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
    delivery_attempts: i64,
    available_at: Option<i64>,
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
            delivery_attempts: row.get(11)?,
            available_at: row.get(12)?,
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
        let delivery_attempts = u32::try_from(self.delivery_attempts)
            .map_err(|_error| corrupt_projection("effects", "delivery_attempts"))?;
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
            delivery_attempts,
            available_at: self.available_at,
        })
    }
}
