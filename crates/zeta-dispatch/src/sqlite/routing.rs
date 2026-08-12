use super::*;

impl Dispatch {
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
            let outcome = append_in_transaction(&transaction, event)?;
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
