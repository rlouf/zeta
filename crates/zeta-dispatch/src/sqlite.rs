//! Private SQLite persistence for journal-v0.

use std::collections::{BTreeMap, BTreeSet, HashSet};
use std::fmt;
use std::path::Path;
use std::str::FromStr;
use std::time::Duration;

use rusqlite::ffi::ErrorCode;
use rusqlite::{params, Connection, OptionalExtension, Row, Transaction, TransactionBehavior};
use serde_json::{Map, Value};
use time::{format_description::well_known::Rfc3339, OffsetDateTime};
use zeta_journal::{
    verify, AppendError, AppendOutcome, Event, Filter, HeadExpectation, JournalEntry,
    VerificationError, VerificationReport,
};
use zeta_substrate::Hash;

use crate::dispatch::{
    Attempt, AttemptCompletion, AttemptFailure, CancellationFinalizationIdentities,
    CancellationIdentities, CancellationOutcome, CancellationStatus, Effect,
    EffectDeliverySemantics, EffectStatus, LockLease, QueueClaim, QueueItem, RecurringSchedule,
    RecurringScheduleStatus, RecurringScheduleTick, ResourceCancellationOutcome,
    ResourceCancellationStatus, ResourceKind, RoutingOutcome, RuntimeEventIdentity,
    ScheduleTickStatus, ScheduledEvent, ScheduledEventStatus, Session, SessionActiveWait,
    SessionActivityStatus, SessionLatestRun, SessionMessageIdentities, SessionMessageRequest,
    StartedAttempt, SubmittedSessionMessage, Wait, WaitStatus,
};
use crate::identity::{
    attempt_id, pending_queue_item_id, queue_item_attempt_idempotency_key, queue_item_id,
    queue_item_idempotency_key, run_id_for_attempt, AttemptId, ClaimToken, QueueItemId, RunId,
    SessionId,
};
use crate::routing::{route_event, Route, SessionError};
use crate::state::{
    classify_error_code, AttemptStatus, DispatchErrorCode, FailureClass, QueueItemStatus,
    RetryPolicy, TransitionError,
};

const BASE_EPOCH: i64 = 1;
const PROJECTION_EPOCH: i64 = 6;
const BUSY_TIMEOUT: Duration = Duration::from_secs(5);
const ENTRY_COLUMNS: &str = "cursor, event_id, event_type, source, payload_bytes, \
    payload_address, idempotency_key, caused_by, session_id, run_id, turn_id, \
    timestamp_ms, previous_address, entry_address";

const CREATE_SCHEMA: &str = "
    CREATE TABLE dispatch_schema (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        base_epoch INTEGER NOT NULL CHECK (base_epoch > 0),
        projection_epoch INTEGER NOT NULL CHECK (projection_epoch >= 0)
    ) STRICT;
    INSERT INTO dispatch_schema (singleton, base_epoch, projection_epoch)
    VALUES (1, 1, 6);

    CREATE TABLE journal_entries (
        cursor INTEGER PRIMARY KEY CHECK (cursor > 0),
        event_id TEXT NOT NULL UNIQUE CHECK (length(event_id) > 0),
        event_type TEXT NOT NULL CHECK (length(event_type) > 0),
        source TEXT NOT NULL CHECK (length(source) > 0),
        payload_bytes BLOB NOT NULL,
        payload_address BLOB NOT NULL CHECK (length(payload_address) = 32),
        idempotency_key TEXT,
        caused_by TEXT,
        session_id TEXT,
        run_id TEXT,
        turn_id TEXT,
        timestamp_ms INTEGER NOT NULL,
        previous_address BLOB CHECK (
            previous_address IS NULL OR length(previous_address) = 32
        ),
        entry_address BLOB NOT NULL CHECK (length(entry_address) = 32)
    ) STRICT;

    CREATE UNIQUE INDEX journal_entries_idempotency
        ON journal_entries(idempotency_key)
        WHERE idempotency_key IS NOT NULL;
    CREATE INDEX journal_entries_event_type
        ON journal_entries(event_type, cursor);
    CREATE INDEX journal_entries_session
        ON journal_entries(session_id, cursor);
    CREATE INDEX journal_entries_run
        ON journal_entries(run_id, cursor);
    CREATE INDEX journal_entries_turn
        ON journal_entries(turn_id, cursor);
    CREATE INDEX journal_entries_parent
        ON journal_entries(caused_by, cursor);
";

const CREATE_PROJECTIONS: &str = "
    CREATE TABLE queue_items (
        queue_item_id TEXT PRIMARY KEY CHECK (length(queue_item_id) > 0),
        event_id TEXT NOT NULL CHECK (length(event_id) > 0),
        target_agent TEXT NOT NULL,
        project_generation TEXT,
        session_id TEXT,
        lock_keys_json TEXT NOT NULL DEFAULT '[]',
        input_cursor INTEGER NOT NULL CHECK (input_cursor > 0),
        status TEXT NOT NULL CHECK (status IN (
            'pending', 'available', 'claimed', 'completed', 'failed',
            'cancelled', 'retry_scheduled', 'dead_lettered', 'unhandled'
        )),
        available_at INTEGER,
        cancel_requested_event_id TEXT,
        cancel_requested_at INTEGER,
        cancel_reason TEXT,
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
        last_error TEXT,
        updated_at INTEGER NOT NULL,
        UNIQUE (event_id, target_agent),
        FOREIGN KEY (event_id) REFERENCES journal_entries(event_id)
    ) STRICT;
    CREATE INDEX queue_items_session_order
        ON queue_items(session_id, input_cursor, status);
    CREATE INDEX queue_items_unbound_order
        ON queue_items(target_agent, input_cursor, status);
    CREATE INDEX queue_items_claim_order
        ON queue_items(status, available_at, input_cursor, queue_item_id);
    CREATE TABLE queue_claims (
        queue_item_id TEXT PRIMARY KEY CHECK (length(queue_item_id) > 0),
        worker_name TEXT NOT NULL CHECK (length(worker_name) > 0),
        claim_token TEXT NOT NULL UNIQUE CHECK (length(claim_token) > 0),
        claimed_at INTEGER NOT NULL,
        claimed_until INTEGER NOT NULL,
        CHECK (claimed_until > claimed_at),
        FOREIGN KEY (queue_item_id) REFERENCES queue_items(queue_item_id)
            ON DELETE CASCADE
    ) STRICT;
    CREATE TABLE attempts (
        attempt_id TEXT PRIMARY KEY CHECK (length(attempt_id) > 0),
        queue_item_id TEXT NOT NULL CHECK (length(queue_item_id) > 0),
        event_id TEXT NOT NULL CHECK (length(event_id) > 0),
        attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
        target_agent TEXT NOT NULL CHECK (length(target_agent) > 0),
        worker_name TEXT,
        claim_token TEXT,
        status TEXT NOT NULL CHECK (status IN (
            'running', 'completed', 'failed', 'cancelled'
        )),
        started_at TEXT NOT NULL CHECK (length(started_at) > 0),
        heartbeat_at INTEGER,
        finished_at TEXT,
        error TEXT,
        session_id TEXT,
        run_id TEXT,
        project_generation TEXT,
        UNIQUE (queue_item_id, attempt_number),
        FOREIGN KEY (queue_item_id) REFERENCES queue_items(queue_item_id),
        FOREIGN KEY (event_id) REFERENCES journal_entries(event_id)
    ) STRICT;
    CREATE TABLE locks (
        lock_key TEXT PRIMARY KEY CHECK (length(lock_key) > 0),
        owner TEXT NOT NULL CHECK (length(owner) > 0),
        acquired_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        CHECK (expires_at > acquired_at),
        FOREIGN KEY (owner) REFERENCES queue_claims(claim_token)
            ON DELETE CASCADE
    ) STRICT;
    CREATE TABLE waits (
        handle TEXT PRIMARY KEY CHECK (length(handle) > 0),
        agent_id TEXT NOT NULL CHECK (length(agent_id) > 0),
        session_id TEXT NOT NULL CHECK (length(session_id) > 0),
        event_type TEXT NOT NULL CHECK (length(event_type) > 0),
        fields_json TEXT NOT NULL,
        deadline_ms INTEGER,
        source_queue_item_id TEXT NOT NULL CHECK (length(source_queue_item_id) > 0),
        project_generation TEXT,
        created_event_id TEXT NOT NULL UNIQUE CHECK (length(created_event_id) > 0),
        status TEXT NOT NULL CHECK (status IN (
            'active', 'matched', 'timed_out', 'cancelled'
        )),
        matched_event_id TEXT,
        terminal_event_id TEXT,
        updated_at INTEGER NOT NULL,
        FOREIGN KEY (created_event_id) REFERENCES journal_entries(event_id),
        FOREIGN KEY (matched_event_id) REFERENCES journal_entries(event_id),
        FOREIGN KEY (terminal_event_id) REFERENCES journal_entries(event_id)
    ) STRICT;
    CREATE UNIQUE INDEX waits_one_active_per_session
        ON waits(session_id) WHERE status = 'active';
    CREATE INDEX waits_match_order
        ON waits(status, event_type, created_event_id, handle);
    CREATE INDEX waits_deadline_order
        ON waits(status, deadline_ms, created_event_id, handle);
    CREATE TABLE scheduled_events (
        handle TEXT PRIMARY KEY CHECK (length(handle) > 0),
        event_type TEXT NOT NULL CHECK (length(event_type) > 0),
        payload_json TEXT NOT NULL,
        publish_at_ms INTEGER NOT NULL,
        source_agent_id TEXT NOT NULL CHECK (length(source_agent_id) > 0),
        source_session_id TEXT,
        source_run_id TEXT,
        source_queue_item_id TEXT NOT NULL CHECK (length(source_queue_item_id) > 0),
        position INTEGER NOT NULL CHECK (position >= 0),
        created_event_id TEXT NOT NULL UNIQUE CHECK (length(created_event_id) > 0),
        status TEXT NOT NULL CHECK (status IN (
            'pending', 'claimed', 'published', 'cancelled'
        )),
        published_event_id TEXT,
        terminal_event_id TEXT,
        updated_at INTEGER NOT NULL,
        FOREIGN KEY (created_event_id) REFERENCES journal_entries(event_id),
        FOREIGN KEY (published_event_id) REFERENCES journal_entries(event_id),
        FOREIGN KEY (terminal_event_id) REFERENCES journal_entries(event_id)
    ) STRICT;
    CREATE INDEX scheduled_events_due_order
        ON scheduled_events(status, publish_at_ms, created_event_id, handle);
    CREATE TABLE effects (
        effect_key TEXT PRIMARY KEY CHECK (length(effect_key) > 0),
        operation TEXT NOT NULL CHECK (length(operation) > 0),
        semantics TEXT NOT NULL CHECK (semantics IN (
            'idempotent_with_key', 'connector_deduplicated',
            'at_least_once', 'unsafe_to_retry'
        )),
        scope TEXT NOT NULL CHECK (length(scope) > 0),
        queue_item_id TEXT,
        params_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN (
            'planned', 'started', 'completed', 'failed', 'ambiguous'
        )),
        result_json TEXT,
        caused_by TEXT NOT NULL CHECK (length(caused_by) > 0),
        planned_event_id TEXT NOT NULL UNIQUE CHECK (length(planned_event_id) > 0),
        terminal_event_id TEXT,
        updated_at INTEGER NOT NULL,
        FOREIGN KEY (planned_event_id) REFERENCES journal_entries(event_id),
        FOREIGN KEY (terminal_event_id) REFERENCES journal_entries(event_id)
    ) STRICT;
    CREATE INDEX effects_retry_blocker
        ON effects(queue_item_id, semantics, status);
    CREATE TABLE recurring_schedules (
        agent_id TEXT NOT NULL CHECK (length(agent_id) > 0),
        schedule_index INTEGER NOT NULL CHECK (schedule_index >= 0),
        cron TEXT NOT NULL CHECK (length(cron) > 0),
        timezone TEXT NOT NULL,
        catchup TEXT,
        event_type TEXT NOT NULL CHECK (length(event_type) > 0),
        activation_event_id TEXT,
        status TEXT NOT NULL CHECK (status IN (
            'activated', 'published', 'skipped', 'missed'
        )),
        last_published_at TEXT,
        next_at TEXT,
        reason TEXT NOT NULL CHECK (length(reason) > 0),
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (agent_id, schedule_index, cron, timezone),
        FOREIGN KEY (activation_event_id) REFERENCES journal_entries(event_id)
    ) STRICT, WITHOUT ROWID;
    CREATE INDEX recurring_schedules_agent_order
        ON recurring_schedules(agent_id, schedule_index);
";

const DROP_PROJECTIONS: &str = "
    DROP TABLE IF EXISTS recurring_schedules;
    DROP TABLE IF EXISTS effects;
    DROP TABLE IF EXISTS scheduled_events;
    DROP TABLE IF EXISTS waits;
    DROP TABLE IF EXISTS locks;
    DROP TABLE IF EXISTS queue_claims;
    DROP TABLE IF EXISTS attempts;
    DROP TABLE IF EXISTS queue_items;
";

/// Owns one durable Dispatch database connection.
///
/// The connection is intentionally not exposed because journal writes and
/// later runtime projections must share one transaction owner.
pub struct Dispatch {
    connection: Connection,
}

impl Dispatch {
    /// Opens or creates a local Dispatch database at `path`.
    ///
    /// A non-empty database without the current Dispatch base epoch is
    /// rejected instead of being migrated as legacy runtime storage.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when the database cannot be configured, its
    /// schema epoch is missing or unsupported, or its base tables are corrupt.
    pub fn open(path: impl AsRef<Path>) -> Result<Self, DispatchError> {
        let connection = Connection::open(path).map_err(|error| database_error("open", error))?;
        open_connection(connection, true)
    }

    /// Creates an isolated in-memory Dispatch database.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when SQLite cannot initialize the schema.
    pub fn open_in_memory() -> Result<Self, DispatchError> {
        let connection = Connection::open_in_memory()
            .map_err(|error| database_error("open in-memory database", error))?;
        open_connection(connection, false)
    }

    /// Appends an event or resolves its id-first duplicate atomically.
    ///
    /// Candidate payload content is intentionally validated only after both
    /// duplicate lookups, matching journal-v0 retry semantics.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] for invalid new events, storage failures, or
    /// corrupted retained journal rows.
    pub fn append_event(&mut self, event: Event) -> Result<AppendOutcome, DispatchError> {
        validate_event_identity(&event)?;
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|error| database_error("begin journal append", error))?;
        let outcome = append_in_transaction(&transaction, event)?;
        if outcome.inserted {
            index_event(&transaction, &outcome.event)?;
        }
        transaction
            .commit()
            .map_err(|error| database_error("commit journal append", error))?;
        Ok(outcome)
    }

    /// Accepts one externally authored event and creates its pending work item.
    ///
    /// Journal insertion and queue projection share one immediate transaction.
    /// An id or idempotency-key duplicate returns the retained event without
    /// creating duplicate work.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError::ReservedRuntimeEvent`] for `runtime.*` input,
    /// or another [`DispatchError`] when append or projection fails.
    pub fn ingest_event(&mut self, event: Event) -> Result<AppendOutcome, DispatchError> {
        if event.event_type.starts_with("runtime.") {
            return Err(DispatchError::ReservedRuntimeEvent {
                event_type: event.event_type,
            });
        }
        self.append_event(event)
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

    /// Returns the final retained entry address, or `None` for an empty journal.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when the last entry cannot be read or its
    /// stored address is malformed.
    pub fn head(&self) -> Result<Option<Hash>, DispatchError> {
        let anchor = last_entry_anchor(&self.connection)?;
        Ok(anchor.map(|(_cursor, address)| address))
    }

    /// Returns one exact durable event by opaque id.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when the row cannot be read as a complete
    /// journal-v0 entry.
    pub fn get_event(&self, event_id: &str) -> Result<Option<Event>, DispatchError> {
        let entry = entry_by_field(&self.connection, "event_id", event_id)?;
        Ok(entry.map(|entry| entry.event))
    }

    /// Returns cursor-ordered events matching every populated filter field.
    ///
    /// Literal prefix matching runs in Rust so SQLite wildcard and collation
    /// rules cannot alter journal-v0 behavior.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when any retained entry is malformed or a
    /// database read fails.
    pub fn list_events(&self, filter: &Filter) -> Result<Vec<Event>, DispatchError> {
        if filter.limit == Some(0) {
            return Ok(Vec::new());
        }
        let entries = load_entries(&self.connection, filter.newest_first)?;
        let mut events = Vec::new();
        for entry in entries {
            if !event_matches(&entry.event, filter) {
                continue;
            }
            events.push(entry.event);
            if filter.limit == Some(events.len()) {
                break;
            }
        }
        Ok(events)
    }

    /// Returns cursor-ordered direct causal children.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when retained entries cannot be read.
    pub fn children(
        &self,
        event_id: &str,
        limit: Option<usize>,
    ) -> Result<Vec<Event>, DispatchError> {
        let filter = Filter {
            caused_by: Some(event_id.to_owned()),
            limit,
            ..Filter::default()
        };
        self.list_events(&filter)
    }

    /// Returns the oldest reachable causal ancestor through one event.
    ///
    /// Missing parents and repeated ids terminate traversal because they are
    /// valid application metadata, not journal-chain corruption.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when a retained entry cannot be read.
    pub fn causal_chain(&self, event_id: &str) -> Result<Vec<Event>, DispatchError> {
        let mut chain = Vec::new();
        let mut seen = HashSet::new();
        let mut current = self.get_event(event_id)?;
        while let Some(event) = current {
            if !seen.insert(event.id.clone()) {
                break;
            }
            let caused_by = event.caused_by.clone();
            chain.push(event);
            let Some(caused_by) = caused_by else {
                break;
            };
            current = self.get_event(&caused_by)?;
        }
        chain.reverse();
        Ok(chain)
    }

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

    /// Returns all durable attempts in input and attempt-number order.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when a projection row cannot be read or
    /// rehydrated as the public typed model.
    pub fn list_attempts(&self) -> Result<Vec<Attempt>, DispatchError> {
        load_attempts(&self.connection)
    }

    /// Returns durable waits in creation order.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when a projection row cannot be read or
    /// rehydrated as the public typed model.
    pub fn list_waits(&self) -> Result<Vec<Wait>, DispatchError> {
        load_waits(&self.connection)
    }

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
                let outcome = append_in_transaction(&transaction, candidate)?;
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

    /// Returns durable scheduled publications in due-time order.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when a projection row cannot be read or
    /// rehydrated as the public typed model.
    pub fn list_scheduled_events(&self) -> Result<Vec<ScheduledEvent>, DispatchError> {
        load_scheduled_events(&self.connection)
    }

    /// Returns durable external effects in planning order.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when a projection row cannot be read or
    /// rehydrated as the public typed model.
    pub fn list_effects(&self) -> Result<Vec<Effect>, DispatchError> {
        load_effects(&self.connection)
    }

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

    /// Publishes the oldest pending scheduled event whose due time has passed.
    ///
    /// The published event, any wait resumptions it triggers, and the schedule
    /// terminal fact share one transaction. `None` means no schedule is due.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when explicit identities do not cover the
    /// publication, every wait continuation, and the terminal fact, or when
    /// the complete transaction cannot be persisted.
    pub fn publish_next_due_scheduled_event(
        &mut self,
        now_ms: i64,
        identities: &[RuntimeEventIdentity],
    ) -> Result<Option<Vec<Event>>, DispatchError> {
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|database| database_error("begin scheduled publication", database))?;
        let Some(scheduled) = next_due_scheduled_event(&transaction, now_ms)? else {
            if !identities.is_empty() {
                return Err(DispatchError::RuntimeEventIdentityCount {
                    expected: 0,
                    actual: identities.len(),
                });
            }
            transaction
                .commit()
                .map_err(|database| database_error("commit empty schedule poll", database))?;
            return Ok(None);
        };
        if identities.len() < 2 {
            return Err(DispatchError::RuntimeEventIdentityCount {
                expected: 2,
                actual: identities.len(),
            });
        }
        let published = scheduled_publication_event(&identities[0], &scheduled);
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
        let terminal =
            scheduled_published_event(&identities[identities.len() - 1], &scheduled, &published);
        events.push(append_runtime_event(&transaction, terminal)?.event);
        transaction
            .commit()
            .map_err(|database| database_error("commit scheduled publication", database))?;
        Ok(Some(events))
    }

    /// Claims the oldest eligible queue item and all of its authored locks.
    ///
    /// The caller supplies an opaque fresh token. Claim and lock insertion
    /// share one immediate transaction. Earlier unbound work, earlier work in
    /// the same session, or any live lock conflict makes a candidate ineligible.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] for an empty worker, invalid lease, reused
    /// token, corrupt projection, or storage failure.
    pub fn claim_next_queue_item(
        &mut self,
        worker_name: &str,
        token: ClaimToken,
        lease_ms: u64,
        now_ms: i64,
    ) -> Result<Option<QueueClaim>, DispatchError> {
        if worker_name.is_empty() {
            return Err(DispatchError::InvalidCoordinationInput {
                field: "worker_name",
            });
        }
        let claimed_until = lease_deadline(now_ms, lease_ms)?;
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|error| database_error("begin queue claim", error))?;
        reconcile_expired_in_transaction(&transaction, now_ms)?;
        if claim_token_exists(&transaction, &token)? {
            return Err(DispatchError::InvalidCoordinationInput {
                field: "claim_token",
            });
        }
        let candidates = claim_candidates(&transaction, now_ms)?;
        for candidate in candidates {
            if !locks_are_available(&transaction, &candidate.lock_keys, now_ms)? {
                continue;
            }
            transaction
                .execute(
                    "INSERT INTO queue_claims (
                        queue_item_id, worker_name, claim_token,
                        claimed_at, claimed_until
                     ) VALUES (?1, ?2, ?3, ?4, ?5)",
                    params![
                        candidate.queue_item_id.as_str(),
                        worker_name,
                        token.as_str(),
                        now_ms,
                        claimed_until,
                    ],
                )
                .map_err(|error| database_error("insert queue claim", error))?;
            for lock_key in &candidate.lock_keys {
                transaction
                    .execute(
                        "INSERT INTO locks (
                            lock_key, owner, acquired_at, expires_at
                         ) VALUES (?1, ?2, ?3, ?4)",
                        params![lock_key, token.as_str(), now_ms, claimed_until],
                    )
                    .map_err(|error| database_error("acquire queue lock", error))?;
            }
            transaction
                .commit()
                .map_err(|error| database_error("commit queue claim", error))?;
            return Ok(Some(QueueClaim {
                queue_item_id: candidate.queue_item_id,
                worker_name: worker_name.to_owned(),
                token,
                claimed_until,
            }));
        }
        transaction
            .commit()
            .map_err(|error| database_error("commit empty queue claim", error))?;
        Ok(None)
    }

    /// Reports whether a claim still owns its unexpired coordination row.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when the ownership row cannot be read.
    pub fn claim_is_current(&self, claim: &QueueClaim, now_ms: i64) -> Result<bool, DispatchError> {
        claim_is_current_in(&self.connection, claim, now_ms)
    }

    /// Renews one current claim and every lock held by its exact token.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] for an invalid lease or storage failure.
    pub fn renew_claim(
        &mut self,
        claim: &QueueClaim,
        lease_ms: u64,
        now_ms: i64,
    ) -> Result<bool, DispatchError> {
        let claimed_until = lease_deadline(now_ms, lease_ms)?;
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|error| database_error("begin claim renewal", error))?;
        let updated = transaction
            .execute(
                "UPDATE queue_claims
                 SET claimed_until = ?1
                 WHERE queue_item_id = ?2
                   AND worker_name = ?3
                   AND claim_token = ?4
                   AND claimed_until > ?5",
                params![
                    claimed_until,
                    claim.queue_item_id.as_str(),
                    &claim.worker_name,
                    claim.token.as_str(),
                    now_ms,
                ],
            )
            .map_err(|error| database_error("renew queue claim", error))?;
        if updated == 1 {
            transaction
                .execute(
                    "UPDATE locks SET expires_at = ?1 WHERE owner = ?2",
                    params![claimed_until, claim.token.as_str()],
                )
                .map_err(|error| database_error("renew queue locks", error))?;
        }
        transaction
            .commit()
            .map_err(|error| database_error("commit claim renewal", error))?;
        Ok(updated == 1)
    }

    /// Releases one exact current claim and all locks owned by its token.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when coordination rows cannot be updated.
    pub fn release_claim(
        &mut self,
        claim: &QueueClaim,
        now_ms: i64,
    ) -> Result<bool, DispatchError> {
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|error| database_error("begin claim release", error))?;
        let released = transaction
            .execute(
                "DELETE FROM queue_claims
                 WHERE queue_item_id = ?1
                   AND worker_name = ?2
                   AND claim_token = ?3
                   AND claimed_until > ?4",
                params![
                    claim.queue_item_id.as_str(),
                    &claim.worker_name,
                    claim.token.as_str(),
                    now_ms,
                ],
            )
            .map_err(|error| database_error("release queue claim", error))?;
        if released == 1 {
            transaction
                .execute(
                    "UPDATE queue_items
                     SET status = 'available'
                     WHERE queue_item_id = ?1 AND status = 'claimed'",
                    params![claim.queue_item_id.as_str()],
                )
                .map_err(|error| database_error("release claimed projection", error))?;
        }
        transaction
            .commit()
            .map_err(|error| database_error("commit claim release", error))?;
        Ok(released == 1)
    }

    /// Releases every claim whose exclusive deadline has passed.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when expired coordination cannot be reconciled.
    pub fn reconcile_expired_claims(&mut self, now_ms: i64) -> Result<usize, DispatchError> {
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|error| database_error("begin expired claim reconciliation", error))?;
        let reconciled = reconcile_expired_in_transaction(&transaction, now_ms)?;
        transaction
            .commit()
            .map_err(|error| database_error("commit expired claim reconciliation", error))?;
        Ok(reconciled)
    }

    /// Returns every live lock in key order.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when a lock row cannot be read or rehydrated.
    pub fn list_locks(&self) -> Result<Vec<LockLease>, DispatchError> {
        load_locks(&self.connection)
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
            let outcome = append_in_transaction(&transaction, event)?;
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
                project_generation: queue_item.project_generation,
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
            let outcome = append_in_transaction(&transaction, event)?;
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
        if identities.len() < 2 {
            return Err(DispatchError::RuntimeEventIdentityCount {
                expected: 2,
                actual: identities.len(),
            });
        }
        validate_distinct_runtime_identities(identities)?;
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
        if queue_item.cancellation_requested_event_id.is_some()
            || result_requests_cancellation(&completion.result)
        {
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
                Some(&completion.result),
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
        let controls = completion_controls(completion)?;
        let expected_identities = controls.len() + 2;
        if identities.len() != expected_identities {
            return Err(DispatchError::RuntimeEventIdentityCount {
                expected: expected_identities,
                actual: identities.len(),
            });
        }
        let completed_attempt =
            completed_attempt_event(&identities[0], &input, &attempt, completion);
        let completed_attempt = append_runtime_event(&transaction, completed_attempt)?.event;
        let mut events = vec![completed_attempt.clone()];
        for (control, identity) in controls.iter().zip(&identities[1..identities.len() - 1]) {
            if let CompletionControl::Cancel {
                handle,
                reason,
                source_agent_id,
                source_session_id,
                ..
            } = control
            {
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
                continue;
            }
            let event = completion_control_event(
                identity,
                control,
                &input,
                &queue_item,
                &attempt,
                &completed_attempt,
            );
            events.push(append_runtime_event(&transaction, event)?.event);
        }
        let completed_queue = completed_queue_event(
            &identities[identities.len() - 1],
            &input,
            &queue_item,
            &attempt,
            &completion.result,
        );
        events.push(append_runtime_event(&transaction, completed_queue)?.event);
        release_claim_in_transaction(&transaction, claim, "release completed attempt claim")?;
        transaction
            .commit()
            .map_err(|database| database_error("commit attempt completion", database))?;
        Ok(events)
    }

    /// Cancels the preferred queue item associated with a public run id.
    ///
    /// Nonterminal work wins over historical terminal rows when a run id was
    /// reused. The resolved item then follows the same intent-first lifecycle
    /// and optional session ownership check as [`Self::cancel_queue_item`].
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] when run resolution, queue cancellation, or
    /// its atomic journal transaction fails.
    pub fn cancel_run(
        &mut self,
        run_id: &RunId,
        expected_session_id: Option<&str>,
        reason: Option<&str>,
        identities: CancellationIdentities,
    ) -> Result<CancellationOutcome, DispatchError> {
        let queue_item_id = self
            .connection
            .query_row(
                "SELECT queue.queue_item_id
                 FROM queue_items AS queue
                 LEFT JOIN journal_entries AS input
                   ON input.event_id = queue.event_id
                 WHERE input.run_id = ?1
                    OR EXISTS (
                      SELECT 1
                      FROM attempts AS attempt
                      WHERE attempt.queue_item_id = queue.queue_item_id
                        AND attempt.run_id = ?1
                    )
                 ORDER BY CASE
                    WHEN queue.status IN (
                      'completed', 'failed', 'cancelled',
                      'dead_lettered', 'unhandled'
                    ) THEN 1 ELSE 0
                 END,
                 queue.input_cursor ASC,
                 queue.queue_item_id ASC
                 LIMIT 1",
                params![run_id.as_str()],
                |row| row.get::<_, String>(0),
            )
            .optional()
            .map_err(|database| database_error("resolve cancellation run", database))?;
        let Some(queue_item_id) = queue_item_id else {
            return Ok(CancellationOutcome {
                queue_item_id: None,
                status: CancellationStatus::Unknown,
                changed: false,
                events: Vec::new(),
            });
        };
        let queue_item_id = QueueItemId::from_str(&queue_item_id)
            .map_err(|_error| corrupt_projection("queue_items", "queue_item_id"))?;
        self.cancel_queue_item(&queue_item_id, expected_session_id, reason, identities)
    }

    /// Makes cancellation intent durable and closes queued work immediately.
    ///
    /// Claimed work retains its live claim and returns `cancelling`; the worker
    /// must observe the durable intent and record its terminal attempt. Pending,
    /// available, and retry-scheduled work records intent and cancellation in
    /// one immediate transaction. Repeated calls return stable dispositions
    /// without replacing the first reason.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError::CancellationSessionMismatch`] when an expected
    /// session does not own the item, or another [`DispatchError`] for identity,
    /// lifecycle, projection, or storage failures.
    pub fn cancel_queue_item(
        &mut self,
        queue_item_id: &QueueItemId,
        expected_session_id: Option<&str>,
        reason: Option<&str>,
        identities: CancellationIdentities,
    ) -> Result<CancellationOutcome, DispatchError> {
        if identities.requested.id() == identities.cancelled.id() {
            return Err(DispatchError::DuplicateRuntimeEventIdentity {
                event_id: identities.requested.id().to_owned(),
            });
        }
        if reason == Some("") {
            return Err(DispatchError::InvalidCoordinationInput { field: "reason" });
        }
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|database| database_error("begin queue cancellation", database))?;
        let Some(mut queue_item) = load_queue_item(&transaction, queue_item_id.as_str())? else {
            transaction
                .commit()
                .map_err(|database| database_error("commit unknown cancellation", database))?;
            return Ok(CancellationOutcome {
                queue_item_id: None,
                status: CancellationStatus::Unknown,
                changed: false,
                events: Vec::new(),
            });
        };
        if expected_session_id.is_some()
            && queue_item.session_id.as_ref().map(SessionId::as_str) != expected_session_id
        {
            return Err(DispatchError::CancellationSessionMismatch {
                expected: expected_session_id.unwrap_or_default().to_owned(),
                actual: queue_item.session_id.as_ref().map(ToString::to_string),
            });
        }
        if queue_item.status == QueueItemStatus::Cancelled {
            let events = retained_cancellation_events(&transaction, &queue_item)?;
            transaction
                .commit()
                .map_err(|database| database_error("commit repeated cancellation", database))?;
            return Ok(CancellationOutcome {
                queue_item_id: Some(queue_item.id.clone()),
                status: CancellationStatus::AlreadyCancelled,
                changed: false,
                events,
            });
        }
        if matches!(
            queue_item.status,
            QueueItemStatus::Completed
                | QueueItemStatus::Failed
                | QueueItemStatus::DeadLettered
                | QueueItemStatus::Unhandled
        ) {
            transaction
                .commit()
                .map_err(|database| database_error("commit terminal cancellation", database))?;
            return Ok(CancellationOutcome {
                queue_item_id: Some(queue_item.id.clone()),
                status: CancellationStatus::AlreadyTerminal,
                changed: false,
                events: Vec::new(),
            });
        }
        let input = entry_by_field(&transaction, "event_id", &queue_item.event_id)?
            .ok_or(DispatchError::CorruptProjection {
                table: "queue_items",
                field: "event_id",
            })?
            .event;
        let run_id = queue_run_id(&transaction, &queue_item, &input)?;
        let (requested, request_inserted) = match queue_item.cancellation_requested_event_id {
            Some(ref event_id) => {
                let event = entry_by_field(&transaction, "event_id", event_id)?
                    .ok_or(DispatchError::CorruptProjection {
                        table: "queue_items",
                        field: "cancel_requested_event_id",
                    })?
                    .event;
                (event, false)
            }
            None => {
                let event = cancellation_requested_event(
                    &identities.requested,
                    &input,
                    &queue_item,
                    queue_item.status,
                    reason,
                    run_id.as_deref(),
                );
                let outcome = append_runtime_event(&transaction, event)?;
                queue_item.cancellation_requested_event_id = Some(outcome.event.id.clone());
                queue_item.cancellation_requested_at = Some(outcome.event.timestamp_ms);
                queue_item.cancellation_reason = reason.map(str::to_owned);
                (outcome.event, outcome.inserted)
            }
        };
        if queue_item.status == QueueItemStatus::Claimed {
            transaction
                .commit()
                .map_err(|database| database_error("commit cancellation intent", database))?;
            return Ok(CancellationOutcome {
                queue_item_id: Some(queue_item.id.clone()),
                status: CancellationStatus::Cancelling,
                changed: request_inserted,
                events: vec![requested],
            });
        }
        let cancelled = cancellation_terminal_event(
            &identities.cancelled,
            &queue_item,
            &requested,
            queue_item.cancellation_reason.as_deref().or(reason),
            run_id.as_deref(),
        );
        let cancelled = append_runtime_event(&transaction, cancelled)?;
        let changed = request_inserted || cancelled.inserted;
        transaction
            .commit()
            .map_err(|database| database_error("commit queue cancellation", database))?;
        Ok(CancellationOutcome {
            queue_item_id: Some(queue_item.id),
            status: CancellationStatus::Cancelled,
            changed,
            events: vec![requested, cancelled.event],
        })
    }

    /// Finalizes the oldest unowned queue item with durable cancellation intent.
    ///
    /// Recovery records a running historical attempt as cancelled before the
    /// queue item. Rows with live claims are left to their fenced worker.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] for duplicate identities, corrupt projected
    /// intent or attempts, lifecycle failures, and storage errors.
    pub fn finalize_next_requested_cancellation(
        &mut self,
        identities: CancellationFinalizationIdentities,
        finished_at: &str,
    ) -> Result<Option<CancellationOutcome>, DispatchError> {
        if finished_at.is_empty() {
            return Err(DispatchError::InvalidCoordinationInput {
                field: "finished_at",
            });
        }
        if identities.attempt_cancelled.id() == identities.queue_cancelled.id() {
            return Err(DispatchError::DuplicateRuntimeEventIdentity {
                event_id: identities.attempt_cancelled.id().to_owned(),
            });
        }
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|database| database_error("begin cancellation recovery", database))?;
        let queue_item_id = transaction
            .query_row(
                "SELECT queue.queue_item_id
                 FROM queue_items AS queue
                 WHERE queue.cancel_requested_event_id IS NOT NULL
                   AND queue.status IN ('pending', 'available', 'retry_scheduled')
                   AND NOT EXISTS (
                     SELECT 1 FROM queue_claims AS claim
                     WHERE claim.queue_item_id = queue.queue_item_id
                   )
                 ORDER BY queue.input_cursor ASC, queue.queue_item_id ASC
                 LIMIT 1",
                [],
                |row| row.get::<_, String>(0),
            )
            .optional()
            .map_err(|database| database_error("select cancellation recovery", database))?;
        let Some(queue_item_id) = queue_item_id else {
            transaction.commit().map_err(|database| {
                database_error("commit empty cancellation recovery", database)
            })?;
            return Ok(None);
        };
        let queue_item_id = QueueItemId::from_str(&queue_item_id)
            .map_err(|_error| corrupt_projection("queue_items", "queue_item_id"))?;
        let queue_item = load_queue_item(&transaction, queue_item_id.as_str())?.ok_or(
            DispatchError::CorruptProjection {
                table: "queue_items",
                field: "queue_item_id",
            },
        )?;
        let requested_id = queue_item
            .cancellation_requested_event_id
            .as_deref()
            .ok_or(DispatchError::CorruptProjection {
                table: "queue_items",
                field: "cancel_requested_event_id",
            })?;
        let requested = entry_by_field(&transaction, "event_id", requested_id)?
            .ok_or(DispatchError::CorruptProjection {
                table: "queue_items",
                field: "cancel_requested_event_id",
            })?
            .event;
        let attempt = load_latest_running_attempt(&transaction, &queue_item_id)?;
        let mut events = Vec::new();
        if let Some(attempt) = &attempt {
            let event = recovered_attempt_cancelled_event(
                &identities.attempt_cancelled,
                &requested,
                attempt,
                finished_at,
                queue_item.cancellation_reason.as_deref(),
            );
            events.push(append_runtime_event(&transaction, event)?.event);
        }
        let input = entry_by_field(&transaction, "event_id", &queue_item.event_id)?
            .ok_or(DispatchError::CorruptProjection {
                table: "queue_items",
                field: "event_id",
            })?
            .event;
        let run_id = match &attempt {
            Some(attempt) => attempt.run_id.as_ref().map(ToString::to_string),
            None => queue_run_id(&transaction, &queue_item, &input)?,
        };
        let queue_event = cancellation_terminal_event(
            &identities.queue_cancelled,
            &queue_item,
            &requested,
            queue_item.cancellation_reason.as_deref(),
            run_id.as_deref(),
        );
        events.push(append_runtime_event(&transaction, queue_event)?.event);
        transaction
            .execute(
                "DELETE FROM queue_claims WHERE queue_item_id = ?1",
                params![queue_item_id.as_str()],
            )
            .map_err(|database| database_error("clear recovered cancellation claim", database))?;
        transaction
            .commit()
            .map_err(|database| database_error("commit cancellation recovery", database))?;
        Ok(Some(CancellationOutcome {
            queue_item_id: Some(queue_item_id),
            status: CancellationStatus::Cancelled,
            changed: true,
            events,
        }))
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

    /// Reconstructs and verifies every retained journal-v0 proof value.
    ///
    /// # Errors
    ///
    /// Returns [`DispatchError`] for unreadable stored proof fields or the
    /// first semantic divergence reported by `zeta-journal`.
    pub fn verify_journal(
        &self,
        expectation: HeadExpectation<'_>,
    ) -> Result<VerificationReport, DispatchError> {
        let entries = load_entries(&self.connection, false)?;
        verify(&entries, expectation).map_err(DispatchError::Verification)
    }
}

/// Reports a structured Dispatch persistence or journal failure.
#[derive(Debug)]
pub enum DispatchError {
    /// A new event violates journal-v0 append rules.
    Append(AppendError),
    /// Full verification found a semantic journal divergence.
    Verification(VerificationError),
    /// A non-empty database does not declare the Dispatch base schema.
    MissingBaseSchema,
    /// The database belongs to another clean base-schema epoch.
    BaseSchemaEpoch {
        /// Carries the only epoch understood by this crate.
        expected: i64,
        /// Carries the stored unsupported epoch.
        actual: i64,
    },
    /// The database declares a newer projection schema than this crate.
    ProjectionSchemaEpoch {
        /// Carries the only projection epoch understood by this crate.
        expected: i64,
        /// Carries the stored unsupported projection epoch.
        actual: i64,
    },
    /// External ingress tried to author runtime-owned lifecycle state.
    ReservedRuntimeEvent {
        /// Carries the rejected event type.
        event_type: String,
    },
    /// Routing could not find the triggering durable event.
    IngressEventNotFound {
        /// Carries the requested event id.
        event_id: String,
    },
    /// Routing received the wrong number of explicit lifecycle identities.
    RuntimeEventIdentityCount {
        /// Carries the number required by the deterministic route plan.
        expected: usize,
        /// Carries the number supplied by the caller.
        actual: usize,
    },
    /// One route operation reused the same generated event id.
    DuplicateRuntimeEventIdentity {
        /// Carries the repeated generated id.
        event_id: String,
    },
    /// A generated id already names a different retained journal event.
    RuntimeEventIdentityCollision {
        /// Carries the colliding generated id.
        event_id: String,
    },
    /// A different durable routing decision already closed the ingress item.
    IngressAlreadyRouted {
        /// Carries the triggering event id.
        event_id: String,
    },
    /// A runtime-owned lifecycle event is missing or mistypes one field.
    InvalidLifecycleEvent {
        /// Carries the malformed lifecycle event id.
        event_id: String,
        /// Names the rejected field.
        field: &'static str,
    },
    /// A projected lifecycle fact requested an illegal state-machine edge.
    Transition(TransitionError),
    /// A matched route could not resolve its session template.
    Session(SessionError),
    /// No durable queue, attempt, or wait record names a requested session.
    SessionNotFound {
        /// Carries the unknown session identity.
        session_id: SessionId,
    },
    /// Durable records disagree about the agent that owns a session.
    SessionOwnerConflict {
        /// Carries the ambiguous session identity.
        session_id: SessionId,
        /// Carries every observed owner in lexical order.
        agent_ids: Vec<String>,
    },
    /// A derived read-model row cannot be rehydrated as its public type.
    CorruptProjection {
        /// Names the malformed projection table.
        table: &'static str,
        /// Names the malformed column.
        field: &'static str,
    },
    /// A claim or lease input cannot identify safe live ownership.
    InvalidCoordinationInput {
        /// Names the invalid field.
        field: &'static str,
    },
    /// A fenced mutation no longer owns the queue item.
    ClaimNotCurrent {
        /// Carries the queue item whose ownership check failed.
        queue_item_id: QueueItemId,
    },
    /// A cancellation caller named a session that does not own the work.
    CancellationSessionMismatch {
        /// Carries the session asserted by the caller.
        expected: String,
        /// Carries the queue item's actual session when it has one.
        actual: Option<String>,
    },
    /// A cancellation handle does not identify a supported resource kind.
    InvalidCancellationHandle {
        /// Carries the malformed handle.
        handle: String,
    },
    /// No durable cancellable resource has the requested handle.
    CancellationResourceNotFound {
        /// Carries the unknown handle.
        handle: String,
    },
    /// An asserted agent or session does not own the resource.
    CancellationAuthorityMismatch {
        /// Carries the resource protected by the ownership check.
        handle: String,
    },
    /// A proposed successful result cannot be committed safely.
    InvalidCompletion {
        /// Names the malformed or unsupported result field.
        field: &'static str,
    },
    /// A calendar-resolved recurring occurrence is malformed.
    InvalidScheduleTick {
        /// Names the rejected occurrence or schedule field.
        field: &'static str,
    },
    /// A retained journal row cannot reconstruct its logical proof value.
    CorruptJournal {
        /// Locates the malformed row when its cursor was readable.
        cursor: Option<u64>,
        /// Names the malformed storage field.
        field: &'static str,
    },
    /// SQLite could not acquire a required lock before the configured timeout.
    StorageBusy {
        /// Names the operation that could not acquire ownership.
        operation: &'static str,
    },
    /// SQLite rejected or could not complete a storage operation.
    Database {
        /// Names the failed storage operation.
        operation: &'static str,
        /// Preserves diagnostic detail without exposing a backend error type.
        message: String,
    },
}

impl DispatchError {
    /// Returns a stable machine-readable failure class.
    pub fn reason(&self) -> &'static str {
        match self {
            DispatchError::Append(_error) => "append",
            DispatchError::Verification(_error) => "verification",
            DispatchError::MissingBaseSchema => "missing_base_schema",
            DispatchError::BaseSchemaEpoch {
                expected: _expected,
                actual: _actual,
            } => "base_schema_epoch",
            DispatchError::ProjectionSchemaEpoch {
                expected: _expected,
                actual: _actual,
            } => "projection_schema_epoch",
            DispatchError::ReservedRuntimeEvent {
                event_type: _event_type,
            } => "reserved_runtime_event",
            DispatchError::IngressEventNotFound {
                event_id: _event_id,
            } => "ingress_event_not_found",
            DispatchError::RuntimeEventIdentityCount {
                expected: _expected,
                actual: _actual,
            } => "runtime_event_identity_count",
            DispatchError::DuplicateRuntimeEventIdentity {
                event_id: _event_id,
            } => "duplicate_runtime_event_identity",
            DispatchError::RuntimeEventIdentityCollision {
                event_id: _event_id,
            } => "runtime_event_identity_collision",
            DispatchError::IngressAlreadyRouted {
                event_id: _event_id,
            } => "ingress_already_routed",
            DispatchError::InvalidLifecycleEvent {
                event_id: _event_id,
                field: _field,
            } => "invalid_lifecycle_event",
            DispatchError::Transition(TransitionError::Queue {
                previous: _previous,
                current: _current,
            }) => "queue_transition",
            DispatchError::Transition(TransitionError::Attempt {
                previous: _previous,
                current: _current,
            }) => "attempt_transition",
            DispatchError::Session(_error) => "session",
            DispatchError::SessionNotFound {
                session_id: _session_id,
            } => "session_not_found",
            DispatchError::SessionOwnerConflict {
                session_id: _session_id,
                agent_ids: _agent_ids,
            } => "session_owner_conflict",
            DispatchError::CorruptProjection {
                table: _table,
                field: _field,
            } => "corrupt_projection",
            DispatchError::InvalidCoordinationInput { field: _field } => {
                "invalid_coordination_input"
            }
            DispatchError::ClaimNotCurrent {
                queue_item_id: _queue_item_id,
            } => "claim_not_current",
            DispatchError::CancellationSessionMismatch {
                expected: _expected,
                actual: _actual,
            } => "cancellation_session_mismatch",
            DispatchError::InvalidCancellationHandle { handle: _handle } => {
                "invalid_cancellation_handle"
            }
            DispatchError::CancellationResourceNotFound { handle: _handle } => {
                "cancellation_resource_not_found"
            }
            DispatchError::CancellationAuthorityMismatch { handle: _handle } => {
                "cancellation_authority_mismatch"
            }
            DispatchError::InvalidCompletion { field: _field } => "invalid_completion",
            DispatchError::InvalidScheduleTick { field: _field } => "invalid_schedule_tick",
            DispatchError::CorruptJournal {
                cursor: _cursor,
                field: _field,
            } => "corrupt_journal",
            DispatchError::StorageBusy {
                operation: _operation,
            } => "storage_busy",
            DispatchError::Database {
                operation: _operation,
                message: _message,
            } => "database",
        }
    }
}

impl fmt::Display for DispatchError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            DispatchError::Append(error) => error.fmt(formatter),
            DispatchError::Verification(error) => error.fmt(formatter),
            DispatchError::MissingBaseSchema => {
                formatter.write_str("non-empty database has no Dispatch base schema")
            }
            DispatchError::BaseSchemaEpoch { expected, actual } => write!(
                formatter,
                "unsupported Dispatch base schema epoch {actual}; expected {expected}"
            ),
            DispatchError::ProjectionSchemaEpoch { expected, actual } => write!(
                formatter,
                "unsupported Dispatch projection schema epoch {actual}; expected at most {expected}"
            ),
            DispatchError::ReservedRuntimeEvent { event_type } => write!(
                formatter,
                "external event ingress cannot accept runtime-owned type {event_type:?}"
            ),
            DispatchError::IngressEventNotFound { event_id } => {
                write!(formatter, "ingress event {event_id:?} was not found")
            }
            DispatchError::RuntimeEventIdentityCount { expected, actual } => write!(
                formatter,
                "routing requires {expected} runtime event identities, got {actual}"
            ),
            DispatchError::DuplicateRuntimeEventIdentity { event_id } => write!(
                formatter,
                "route operation repeats runtime event identity {event_id:?}"
            ),
            DispatchError::RuntimeEventIdentityCollision { event_id } => write!(
                formatter,
                "runtime event identity {event_id:?} already names different content"
            ),
            DispatchError::IngressAlreadyRouted { event_id } => write!(
                formatter,
                "ingress event {event_id:?} already has a different durable route"
            ),
            DispatchError::InvalidLifecycleEvent { event_id, field } => write!(
                formatter,
                "runtime lifecycle event {event_id:?} has invalid field {field:?}"
            ),
            DispatchError::Transition(error) => error.fmt(formatter),
            DispatchError::Session(error) => error.fmt(formatter),
            DispatchError::SessionNotFound { session_id } => {
                write!(formatter, "unknown session {session_id:?}")
            }
            DispatchError::SessionOwnerConflict {
                session_id,
                agent_ids,
            } => write!(
                formatter,
                "session {session_id:?} has conflicting owners: {}",
                agent_ids.join(", ")
            ),
            DispatchError::CorruptProjection { table, field } => {
                write!(formatter, "corrupt projection field {table}.{field}")
            }
            DispatchError::InvalidCoordinationInput { field } => {
                write!(formatter, "invalid coordination input {field:?}")
            }
            DispatchError::ClaimNotCurrent { queue_item_id } => {
                write!(
                    formatter,
                    "claim for queue item {queue_item_id} is not current"
                )
            }
            DispatchError::CancellationSessionMismatch { expected, actual } => write!(
                formatter,
                "cancellation session {expected:?} does not own queue session {actual:?}"
            ),
            DispatchError::InvalidCancellationHandle { handle } => write!(
                formatter,
                "cancellation handle {handle:?} must identify a wait or scheduled event"
            ),
            DispatchError::CancellationResourceNotFound { handle } => {
                write!(formatter, "unknown cancellation resource {handle:?}")
            }
            DispatchError::CancellationAuthorityMismatch { handle } => {
                write!(formatter, "cancellation caller does not own {handle:?}")
            }
            DispatchError::InvalidCompletion { field } => {
                write!(formatter, "invalid attempt completion field {field:?}")
            }
            DispatchError::InvalidScheduleTick { field } => {
                write!(formatter, "invalid recurring schedule tick field {field:?}")
            }
            DispatchError::CorruptJournal { cursor, field } => {
                write!(
                    formatter,
                    "corrupt journal field {field:?} at cursor {cursor:?}"
                )
            }
            DispatchError::StorageBusy { operation } => {
                write!(
                    formatter,
                    "Dispatch storage remained busy during {operation}"
                )
            }
            DispatchError::Database { operation, message } => {
                write!(
                    formatter,
                    "Dispatch database failed during {operation}: {message}"
                )
            }
        }
    }
}

impl std::error::Error for DispatchError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            DispatchError::Append(error) => Some(error),
            DispatchError::Verification(error) => Some(error),
            DispatchError::MissingBaseSchema => None,
            DispatchError::BaseSchemaEpoch {
                expected: _expected,
                actual: _actual,
            } => None,
            DispatchError::ProjectionSchemaEpoch {
                expected: _expected,
                actual: _actual,
            } => None,
            DispatchError::ReservedRuntimeEvent {
                event_type: _event_type,
            } => None,
            DispatchError::IngressEventNotFound {
                event_id: _event_id,
            } => None,
            DispatchError::RuntimeEventIdentityCount {
                expected: _expected,
                actual: _actual,
            } => None,
            DispatchError::DuplicateRuntimeEventIdentity {
                event_id: _event_id,
            } => None,
            DispatchError::RuntimeEventIdentityCollision {
                event_id: _event_id,
            } => None,
            DispatchError::IngressAlreadyRouted {
                event_id: _event_id,
            } => None,
            DispatchError::InvalidLifecycleEvent {
                event_id: _event_id,
                field: _field,
            } => None,
            DispatchError::Transition(error) => Some(error),
            DispatchError::Session(error) => Some(error),
            DispatchError::SessionNotFound {
                session_id: _session_id,
            } => None,
            DispatchError::SessionOwnerConflict {
                session_id: _session_id,
                agent_ids: _agent_ids,
            } => None,
            DispatchError::CorruptProjection {
                table: _table,
                field: _field,
            } => None,
            DispatchError::InvalidCoordinationInput { field: _field } => None,
            DispatchError::ClaimNotCurrent {
                queue_item_id: _queue_item_id,
            } => None,
            DispatchError::CancellationSessionMismatch {
                expected: _expected,
                actual: _actual,
            } => None,
            DispatchError::InvalidCancellationHandle { handle: _handle } => None,
            DispatchError::CancellationResourceNotFound { handle: _handle } => None,
            DispatchError::CancellationAuthorityMismatch { handle: _handle } => None,
            DispatchError::InvalidCompletion { field: _field } => None,
            DispatchError::InvalidScheduleTick { field: _field } => None,
            DispatchError::CorruptJournal {
                cursor: _cursor,
                field: _field,
            } => None,
            DispatchError::StorageBusy {
                operation: _operation,
            } => None,
            DispatchError::Database {
                operation: _operation,
                message: _message,
            } => None,
        }
    }
}

impl From<AppendError> for DispatchError {
    fn from(error: AppendError) -> Self {
        DispatchError::Append(error)
    }
}

impl From<TransitionError> for DispatchError {
    fn from(error: TransitionError) -> Self {
        DispatchError::Transition(error)
    }
}

impl From<SessionError> for DispatchError {
    fn from(error: SessionError) -> Self {
        DispatchError::Session(error)
    }
}

fn open_connection(
    mut connection: Connection,
    file_backed: bool,
) -> Result<Dispatch, DispatchError> {
    connection
        .busy_timeout(BUSY_TIMEOUT)
        .map_err(|error| database_error("configure busy timeout", error))?;
    connection
        .pragma_update(None, "foreign_keys", true)
        .map_err(|error| database_error("enable foreign keys", error))?;
    if file_backed {
        let mode: String = connection
            .pragma_query_value(None, "journal_mode", |row| row.get(0))
            .map_err(|error| database_error("read journal mode", error))?;
        if mode.to_lowercase() != "wal" {
            connection
                .pragma_update(None, "journal_mode", "WAL")
                .map_err(|error| database_error("enable WAL", error))?;
            let mode: String = connection
                .pragma_query_value(None, "journal_mode", |row| row.get(0))
                .map_err(|error| database_error("confirm WAL", error))?;
            if mode.to_lowercase() != "wal" {
                return Err(DispatchError::Database {
                    operation: "enable WAL",
                    message: format!("SQLite selected journal mode {mode:?}"),
                });
            }
        }
    }
    connection
        .pragma_update(None, "synchronous", "FULL")
        .map_err(|error| database_error("configure synchronous writes", error))?;
    initialize_schema(&mut connection)?;
    Ok(Dispatch { connection })
}

fn initialize_schema(connection: &mut Connection) -> Result<(), DispatchError> {
    let transaction = connection
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|error| database_error("begin schema check", error))?;
    let mut statement = transaction
        .prepare(
            "SELECT name FROM sqlite_schema
             WHERE type = 'table' AND substr(name, 1, 7) != 'sqlite_'
             ORDER BY name",
        )
        .map_err(|error| database_error("inspect schema", error))?;
    let rows = statement
        .query_map([], |row| row.get::<_, String>(0))
        .map_err(|error| database_error("inspect schema", error))?;
    let mut tables = Vec::new();
    for row in rows {
        tables.push(row.map_err(|error| database_error("inspect schema", error))?);
    }
    drop(statement);

    if tables.is_empty() {
        transaction
            .execute_batch(CREATE_SCHEMA)
            .map_err(|error| database_error("create schema", error))?;
        transaction
            .execute_batch(CREATE_PROJECTIONS)
            .map_err(|error| database_error("create projections", error))?;
        transaction
            .commit()
            .map_err(|error| database_error("commit schema", error))?;
        return Ok(());
    }
    if !has_table(&tables, "dispatch_schema") {
        return Err(DispatchError::MissingBaseSchema);
    }
    let epochs = transaction
        .query_row(
            "SELECT base_epoch, projection_epoch
             FROM dispatch_schema WHERE singleton = 1",
            [],
            |row| Ok((row.get::<_, i64>(0)?, row.get::<_, i64>(1)?)),
        )
        .optional()
        .map_err(|error| database_error("read schema epoch", error))?;
    let Some((base_epoch, projection_epoch)) = epochs else {
        return Err(DispatchError::MissingBaseSchema);
    };
    if base_epoch != BASE_EPOCH {
        return Err(DispatchError::BaseSchemaEpoch {
            expected: BASE_EPOCH,
            actual: base_epoch,
        });
    }
    if !has_table(&tables, "journal_entries") {
        return Err(DispatchError::CorruptJournal {
            cursor: None,
            field: "journal_entries",
        });
    }
    if projection_epoch > PROJECTION_EPOCH {
        return Err(DispatchError::ProjectionSchemaEpoch {
            expected: PROJECTION_EPOCH,
            actual: projection_epoch,
        });
    }
    let mut projections_present = true;
    for expected in [
        "queue_items",
        "attempts",
        "queue_claims",
        "locks",
        "waits",
        "scheduled_events",
        "effects",
        "recurring_schedules",
    ] {
        if !has_table(&tables, expected) {
            projections_present = false;
            break;
        }
    }
    if projection_epoch < PROJECTION_EPOCH || !projections_present {
        rebuild_projections_in_transaction(&transaction)?;
        transaction
            .execute(
                "UPDATE dispatch_schema SET projection_epoch = ?1 WHERE singleton = 1",
                params![PROJECTION_EPOCH],
            )
            .map_err(|error| database_error("record projection epoch", error))?;
    }
    transaction
        .commit()
        .map_err(|error| database_error("commit schema check", error))?;
    Ok(())
}

fn has_table(tables: &[String], expected: &str) -> bool {
    for table in tables {
        if table == expected {
            return true;
        }
    }
    false
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

struct QueueLifecycleFields<'a> {
    queue_item_id: &'a QueueItemId,
    target_agent: &'a str,
    status: QueueItemStatus,
    session_id: Option<&'a SessionId>,
    project_generation: Option<&'a str>,
    lock_keys: &'a [String],
}

fn queue_lifecycle_event(
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

fn same_logical_event(candidate: &Event, retained: &Event) -> bool {
    let mut retained = retained.clone();
    retained.cursor = candidate.cursor;
    candidate == &retained
}

fn same_lifecycle_intention(candidate: &Event, retained: &Event) -> bool {
    candidate.event_type == retained.event_type
        && candidate.source == retained.source
        && candidate.payload == retained.payload
        && candidate.idempotency_key == retained.idempotency_key
        && candidate.caused_by == retained.caused_by
        && candidate.session_id == retained.session_id
        && candidate.run_id == retained.run_id
        && candidate.turn_id == retained.turn_id
}

fn append_runtime_event(
    transaction: &Transaction<'_>,
    event: Event,
) -> Result<AppendOutcome, DispatchError> {
    validate_event_identity(&event)?;
    let candidate = event.clone();
    let outcome = append_in_transaction(transaction, event)?;
    if !outcome.inserted && !same_lifecycle_intention(&candidate, &outcome.event) {
        return Err(DispatchError::RuntimeEventIdentityCollision {
            event_id: candidate.id,
        });
    }
    if outcome.inserted {
        index_event(transaction, &outcome.event)?;
    }
    Ok(outcome)
}

fn queue_run_id(
    connection: &Connection,
    queue_item: &QueueItem,
    input: &Event,
) -> Result<Option<String>, DispatchError> {
    if input.run_id.is_some() {
        return Ok(input.run_id.clone());
    }
    connection
        .query_row(
            "SELECT run_id FROM attempts
             WHERE queue_item_id = ?1 AND run_id IS NOT NULL
             ORDER BY attempt_number DESC LIMIT 1",
            params![queue_item.id.as_str()],
            |row| row.get::<_, String>(0),
        )
        .optional()
        .map_err(|database| database_error("resolve queue run", database))
}

fn cancellation_requested_event(
    identity: &RuntimeEventIdentity,
    input: &Event,
    queue_item: &QueueItem,
    status: QueueItemStatus,
    reason: Option<&str>,
    run_id: Option<&str>,
) -> Event {
    let mut payload = queue_item_payload(queue_item, status);
    if let Some(reason) = reason {
        payload.insert("reason".to_owned(), Value::String(reason.to_owned()));
    }
    Event {
        id: identity.id().to_owned(),
        event_type: "runtime.queue_item.cancel_requested".to_owned(),
        source: "zeta".to_owned(),
        payload,
        idempotency_key: Some(format!(
            "queue_item:{}:{}:cancel_requested",
            queue_item.event_id, queue_item.target_agent
        )),
        caused_by: Some(input.id.clone()),
        session_id: queue_item.session_id.as_ref().map(ToString::to_string),
        run_id: run_id.map(str::to_owned),
        turn_id: None,
        timestamp_ms: identity.timestamp_ms(),
        cursor: None,
    }
}

fn cancellation_terminal_event(
    identity: &RuntimeEventIdentity,
    queue_item: &QueueItem,
    requested: &Event,
    reason: Option<&str>,
    run_id: Option<&str>,
) -> Event {
    let mut payload = queue_item_payload(queue_item, QueueItemStatus::Cancelled);
    let mut result = Map::new();
    result.insert("outcome".to_owned(), Value::String("cancelled".to_owned()));
    if let Some(reason) = reason {
        payload.insert("reason".to_owned(), Value::String(reason.to_owned()));
        result.insert("reason".to_owned(), Value::String(reason.to_owned()));
    }
    payload.insert("result".to_owned(), Value::Object(result));
    Event {
        id: identity.id().to_owned(),
        event_type: "runtime.queue_item.cancelled".to_owned(),
        source: "zeta".to_owned(),
        payload,
        idempotency_key: Some(queue_item_idempotency_key(
            &queue_item.event_id,
            &queue_item.target_agent,
            QueueItemStatus::Cancelled,
        )),
        caused_by: Some(requested.id.clone()),
        session_id: queue_item.session_id.as_ref().map(ToString::to_string),
        run_id: run_id.map(str::to_owned),
        turn_id: None,
        timestamp_ms: identity.timestamp_ms(),
        cursor: None,
    }
}

fn live_cancelled_events(
    identities: &[RuntimeEventIdentity; 2],
    input: &Event,
    queue_item: &QueueItem,
    attempt: &Attempt,
    finished_at: &str,
    raw_result: Option<&Map<String, Value>>,
) -> [Event; 2] {
    let mut result = raw_result.cloned().unwrap_or_default();
    result.insert("outcome".to_owned(), Value::String("cancelled".to_owned()));
    result.insert(
        "stop_reason".to_owned(),
        Value::String("aborted".to_owned()),
    );
    let mut terminal_attempt = attempt_payload(attempt, AttemptStatus::Cancelled);
    terminal_attempt.insert(
        "finished_at".to_owned(),
        Value::String(finished_at.to_owned()),
    );
    terminal_attempt.insert("result".to_owned(), Value::Object(result.clone()));
    let attempt_event = Event {
        id: identities[0].id().to_owned(),
        event_type: "runtime.attempt.cancelled".to_owned(),
        source: "zeta".to_owned(),
        payload: terminal_attempt,
        idempotency_key: Some(crate::identity::attempt_idempotency_key(
            &attempt.queue_item_id,
            attempt.attempt_number,
            AttemptStatus::Cancelled,
        )),
        caused_by: Some(input.id.clone()),
        session_id: attempt.session_id.as_ref().map(ToString::to_string),
        run_id: attempt.run_id.as_ref().map(ToString::to_string),
        turn_id: input.turn_id.clone(),
        timestamp_ms: identities[0].timestamp_ms(),
        cursor: None,
    };
    let mut terminal_queue = queue_item_payload(queue_item, QueueItemStatus::Cancelled);
    terminal_queue.insert("result".to_owned(), Value::Object(result));
    let queue_event = Event {
        id: identities[1].id().to_owned(),
        event_type: "runtime.queue_item.cancelled".to_owned(),
        source: "zeta".to_owned(),
        payload: terminal_queue,
        idempotency_key: Some(queue_item_idempotency_key(
            &queue_item.event_id,
            &queue_item.target_agent,
            QueueItemStatus::Cancelled,
        )),
        caused_by: Some(input.id.clone()),
        session_id: queue_item.session_id.as_ref().map(ToString::to_string),
        run_id: attempt.run_id.as_ref().map(ToString::to_string),
        turn_id: input.turn_id.clone(),
        timestamp_ms: identities[1].timestamp_ms(),
        cursor: None,
    };
    [attempt_event, queue_event]
}

fn recovered_attempt_cancelled_event(
    identity: &RuntimeEventIdentity,
    requested: &Event,
    attempt: &Attempt,
    finished_at: &str,
    reason: Option<&str>,
) -> Event {
    let mut result = Map::new();
    result.insert("outcome".to_owned(), Value::String("cancelled".to_owned()));
    if let Some(reason) = reason {
        result.insert("reason".to_owned(), Value::String(reason.to_owned()));
    }
    let mut payload = attempt_payload(attempt, AttemptStatus::Cancelled);
    payload.remove("error");
    payload.remove("project_generation");
    payload.insert(
        "finished_at".to_owned(),
        Value::String(finished_at.to_owned()),
    );
    payload
        .entry("worker_name".to_owned())
        .or_insert(Value::Null);
    payload.insert("result".to_owned(), Value::Object(result));
    Event {
        id: identity.id().to_owned(),
        event_type: "runtime.attempt.cancelled".to_owned(),
        source: "zeta".to_owned(),
        payload,
        idempotency_key: Some(crate::identity::attempt_idempotency_key(
            &attempt.queue_item_id,
            attempt.attempt_number,
            AttemptStatus::Cancelled,
        )),
        caused_by: Some(requested.id.clone()),
        session_id: attempt.session_id.as_ref().map(ToString::to_string),
        run_id: attempt.run_id.as_ref().map(ToString::to_string),
        turn_id: None,
        timestamp_ms: identity.timestamp_ms(),
        cursor: None,
    }
}

fn retained_cancellation_events(
    connection: &Connection,
    queue_item: &QueueItem,
) -> Result<Vec<Event>, DispatchError> {
    let Some(requested_id) = &queue_item.cancellation_requested_event_id else {
        return Ok(Vec::new());
    };
    let requested = entry_by_field(connection, "event_id", requested_id)?
        .ok_or(DispatchError::CorruptProjection {
            table: "queue_items",
            field: "cancel_requested_event_id",
        })?
        .event;
    let cancelled_key = queue_item_idempotency_key(
        &queue_item.event_id,
        &queue_item.target_agent,
        QueueItemStatus::Cancelled,
    );
    let cancelled = entry_by_field(connection, "idempotency_key", &cancelled_key)?
        .ok_or(DispatchError::CorruptProjection {
            table: "queue_items",
            field: "cancelled_event",
        })?
        .event;
    Ok(vec![requested, cancelled])
}

struct ClaimCandidate {
    queue_item_id: QueueItemId,
    lock_keys: Vec<String>,
}

fn claim_candidates(
    connection: &Connection,
    now_ms: i64,
) -> Result<Vec<ClaimCandidate>, DispatchError> {
    let mut statement = connection
        .prepare(
            "SELECT queue.queue_item_id, queue.lock_keys_json
             FROM queue_items AS queue
             WHERE queue.status = 'available'
               AND COALESCE(queue.available_at, queue.updated_at) <= ?1
               AND NOT EXISTS (
                 SELECT 1 FROM queue_claims AS claim
                 WHERE claim.queue_item_id = queue.queue_item_id
               )
               AND queue.cancel_requested_event_id IS NULL
               AND NOT EXISTS (
                 SELECT 1 FROM queue_items AS barrier
                 WHERE barrier.input_cursor < queue.input_cursor
                   AND barrier.target_agent = ''
                   AND barrier.status = 'pending'
               )
               AND (
                 queue.session_id IS NULL OR NOT EXISTS (
                   SELECT 1 FROM queue_items AS earlier
                   WHERE earlier.session_id = queue.session_id
                     AND earlier.input_cursor < queue.input_cursor
                     AND earlier.status NOT IN (
                       'completed', 'cancelled', 'dead_lettered', 'unhandled'
                     )
                 )
               )
             ORDER BY queue.input_cursor ASC, queue.queue_item_id ASC",
        )
        .map_err(|error| database_error("prepare claim candidates", error))?;
    let rows = statement
        .query_map(params![now_ms], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })
        .map_err(|error| database_error("read claim candidates", error))?;
    let mut candidates = Vec::new();
    for row in rows {
        let (queue_item_id, lock_keys_json) =
            row.map_err(|error| database_error("read claim candidate", error))?;
        let queue_item_id = QueueItemId::from_str(&queue_item_id)
            .map_err(|_error| corrupt_projection("queue_items", "queue_item_id"))?;
        let lock_keys: Vec<String> = serde_json::from_str(&lock_keys_json)
            .map_err(|_error| corrupt_projection("queue_items", "lock_keys_json"))?;
        let mut seen = HashSet::new();
        for lock_key in &lock_keys {
            if lock_key.is_empty() || !seen.insert(lock_key) {
                return Err(corrupt_projection("queue_items", "lock_keys_json"));
            }
        }
        candidates.push(ClaimCandidate {
            queue_item_id,
            lock_keys,
        });
    }
    Ok(candidates)
}

fn locks_are_available(
    connection: &Connection,
    lock_keys: &[String],
    now_ms: i64,
) -> Result<bool, DispatchError> {
    for lock_key in lock_keys {
        let held = connection
            .query_row(
                "SELECT 1 FROM locks
                 WHERE lock_key = ?1 AND expires_at > ?2",
                params![lock_key, now_ms],
                |_row| Ok(()),
            )
            .optional()
            .map_err(|error| database_error("check queue lock", error))?;
        if held.is_some() {
            return Ok(false);
        }
    }
    Ok(true)
}

fn claim_token_exists(connection: &Connection, token: &ClaimToken) -> Result<bool, DispatchError> {
    let existing = connection
        .query_row(
            "SELECT 1 FROM queue_claims WHERE claim_token = ?1",
            params![token.as_str()],
            |_row| Ok(()),
        )
        .optional()
        .map_err(|error| database_error("check claim token", error))?;
    Ok(existing.is_some())
}

fn claim_is_current_in(
    connection: &Connection,
    claim: &QueueClaim,
    now_ms: i64,
) -> Result<bool, DispatchError> {
    let current = connection
        .query_row(
            "SELECT 1 FROM queue_claims
             WHERE queue_item_id = ?1
               AND worker_name = ?2
               AND claim_token = ?3
               AND claimed_until > ?4",
            params![
                claim.queue_item_id.as_str(),
                &claim.worker_name,
                claim.token.as_str(),
                now_ms,
            ],
            |_row| Ok(()),
        )
        .optional()
        .map_err(|error| database_error("check queue claim", error))?;
    Ok(current.is_some())
}

fn release_claim_in_transaction(
    connection: &Connection,
    claim: &QueueClaim,
    operation: &'static str,
) -> Result<(), DispatchError> {
    let deleted = connection
        .execute(
            "DELETE FROM queue_claims
             WHERE queue_item_id = ?1
               AND worker_name = ?2
               AND claim_token = ?3",
            params![
                claim.queue_item_id.as_str(),
                &claim.worker_name,
                claim.token.as_str(),
            ],
        )
        .map_err(|database| database_error(operation, database))?;
    if deleted != 1 {
        return Err(DispatchError::ClaimNotCurrent {
            queue_item_id: claim.queue_item_id.clone(),
        });
    }
    Ok(())
}

fn lease_deadline(now_ms: i64, lease_ms: u64) -> Result<i64, DispatchError> {
    if lease_ms == 0 || lease_ms > i64::MAX as u64 {
        return Err(DispatchError::InvalidCoordinationInput { field: "lease_ms" });
    }
    let lease_ms = lease_ms as i64;
    now_ms
        .checked_add(lease_ms)
        .ok_or(DispatchError::InvalidCoordinationInput { field: "lease_ms" })
}

fn reconcile_expired_in_transaction(
    connection: &Connection,
    now_ms: i64,
) -> Result<usize, DispatchError> {
    connection
        .execute(
            "UPDATE queue_items
             SET status = 'available'
             WHERE status = 'claimed'
               AND queue_item_id IN (
                 SELECT queue_item_id FROM queue_claims
                 WHERE claimed_until <= ?1
               )",
            params![now_ms],
        )
        .map_err(|error| database_error("recover expired queue projection", error))?;
    let deleted = connection
        .execute(
            "DELETE FROM queue_claims WHERE claimed_until <= ?1",
            params![now_ms],
        )
        .map_err(|error| database_error("delete expired queue claims", error))?;
    Ok(deleted)
}

fn load_locks(connection: &Connection) -> Result<Vec<LockLease>, DispatchError> {
    let mut statement = connection
        .prepare(
            "SELECT lock_key, owner, acquired_at, expires_at
             FROM locks ORDER BY lock_key ASC",
        )
        .map_err(|error| database_error("prepare lock read", error))?;
    let rows = statement
        .query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, i64>(2)?,
                row.get::<_, i64>(3)?,
            ))
        })
        .map_err(|error| database_error("read locks", error))?;
    let mut locks = Vec::new();
    for row in rows {
        let (key, owner, acquired_at, expires_at) =
            row.map_err(|error| database_error("read lock", error))?;
        let owner =
            ClaimToken::from_str(&owner).map_err(|_error| corrupt_projection("locks", "owner"))?;
        if key.is_empty() || expires_at <= acquired_at {
            return Err(corrupt_projection("locks", "lease"));
        }
        locks.push(LockLease {
            key,
            owner,
            acquired_at,
            expires_at,
        });
    }
    Ok(locks)
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
    if let Some(project_generation) = &queue_item.project_generation {
        payload.insert(
            "project_generation".to_owned(),
            Value::String(project_generation.clone()),
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

enum CompletionControl {
    Publish {
        position: u64,
        handle: String,
        event_type: String,
        payload: Map<String, Value>,
        publish_at: Option<String>,
        immediate: bool,
    },
    Wait {
        position: u64,
        handle: String,
        event_type: String,
        fields: Map<String, Value>,
        deadline: Option<String>,
    },
    Cancel {
        position: u64,
        handle: String,
        reason: Option<String>,
        source_agent_id: String,
        source_session_id: String,
    },
}

impl CompletionControl {
    fn position(&self) -> u64 {
        match self {
            CompletionControl::Publish { position, .. }
            | CompletionControl::Wait { position, .. }
            | CompletionControl::Cancel { position, .. } => *position,
        }
    }
}

fn completion_controls(
    completion: &AttemptCompletion,
) -> Result<Vec<CompletionControl>, DispatchError> {
    let completed_at = parse_completion_timestamp(&completion.finished_at, "finished_at")?;
    validate_optional_completion_string(&completion.result, "final_answer")?;
    validate_optional_completion_array(&completion.result, "events")?;
    validate_optional_completion_array(&completion.result, "tool_calls")?;
    if let Some(value) = completion.result.get("usage") {
        if !value.is_object() {
            return Err(invalid_completion("usage"));
        }
    }
    let mut controls = Vec::new();
    let mut positions = HashSet::new();
    for value in completion_array(&completion.result, "publish_event_requests")? {
        let Some(request) = value.as_object() else {
            return Err(invalid_completion("publish_event_requests"));
        };
        let position = completion_position(request, "publish_event_requests.position")?;
        if !positions.insert(position) {
            return Err(invalid_completion("control.position"));
        }
        let handle = completion_string(request, "handle", "publish_event_requests.handle")?;
        let event_type =
            completion_string(request, "event_type", "publish_event_requests.event_type")?;
        let payload = completion_object(request, "payload", "publish_event_requests.payload")?;
        let publish_at = completion_optional_string(request, "at", "publish_event_requests.at")?;
        let immediate = match &publish_at {
            Some(publish_at) => {
                parse_completion_timestamp(publish_at, "publish_event_requests.at")? <= completed_at
            }
            None => true,
        };
        controls.push(CompletionControl::Publish {
            position,
            handle,
            event_type,
            payload,
            publish_at,
            immediate,
        });
    }
    for value in completion_array(&completion.result, "wait_requests")? {
        let Some(request) = value.as_object() else {
            return Err(invalid_completion("wait_requests"));
        };
        let position = completion_position(request, "wait_requests.position")?;
        if !positions.insert(position) {
            return Err(invalid_completion("control.position"));
        }
        let handle = completion_string(request, "handle", "wait_requests.handle")?;
        let event_type = completion_string(request, "event_type", "wait_requests.event_type")?;
        let fields = completion_object(request, "fields", "wait_requests.fields")?;
        let deadline = completion_optional_string(request, "deadline", "wait_requests.deadline")?;
        if let Some(deadline) = &deadline {
            parse_completion_timestamp(deadline, "wait_requests.deadline")?;
        }
        controls.push(CompletionControl::Wait {
            position,
            handle,
            event_type,
            fields,
            deadline,
        });
    }
    for value in completion_array(&completion.result, "cancel_requests")? {
        let Some(request) = value.as_object() else {
            return Err(invalid_completion("cancel_requests"));
        };
        let position = completion_position(request, "cancel_requests.position")?;
        if !positions.insert(position) {
            return Err(invalid_completion("control.position"));
        }
        let handle = completion_string(request, "handle", "cancel_requests.handle")?;
        let reason = completion_optional_string(request, "reason", "cancel_requests.reason")?;
        let source_agent_id = completion_string(
            request,
            "source_agent_id",
            "cancel_requests.source_agent_id",
        )?;
        let source_session_id = completion_string(
            request,
            "source_session_id",
            "cancel_requests.source_session_id",
        )?;
        controls.push(CompletionControl::Cancel {
            position,
            handle,
            reason,
            source_agent_id,
            source_session_id,
        });
    }
    if !completion_array(&completion.result, "content_promotions")?.is_empty() {
        return Err(invalid_completion("content_promotions"));
    }
    controls.sort_by_key(CompletionControl::position);
    Ok(controls)
}

fn completion_array<'a>(
    result: &'a Map<String, Value>,
    field: &'static str,
) -> Result<&'a [Value], DispatchError> {
    match result.get(field) {
        Some(Value::Array(values)) => Ok(values),
        Some(_value) => Err(invalid_completion(field)),
        None => Ok(&[]),
    }
}

fn validate_optional_completion_array(
    result: &Map<String, Value>,
    field: &'static str,
) -> Result<(), DispatchError> {
    completion_array(result, field).map(|_values| ())
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

fn completion_position(
    request: &Map<String, Value>,
    field: &'static str,
) -> Result<u64, DispatchError> {
    request
        .get("position")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_completion(field))
}

fn completion_string(
    request: &Map<String, Value>,
    key: &str,
    field: &'static str,
) -> Result<String, DispatchError> {
    match request.get(key) {
        Some(Value::String(value)) if !value.is_empty() => Ok(value.clone()),
        _ => Err(invalid_completion(field)),
    }
}

fn completion_optional_string(
    request: &Map<String, Value>,
    key: &str,
    field: &'static str,
) -> Result<Option<String>, DispatchError> {
    match request.get(key) {
        Some(Value::String(value)) if !value.is_empty() => Ok(Some(value.clone())),
        Some(Value::Null) => Ok(None),
        _ => Err(invalid_completion(field)),
    }
}

fn completion_object(
    request: &Map<String, Value>,
    key: &str,
    field: &'static str,
) -> Result<Map<String, Value>, DispatchError> {
    match request.get(key) {
        Some(Value::Object(value)) => Ok(value.clone()),
        _ => Err(invalid_completion(field)),
    }
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

fn validate_distinct_runtime_identities(
    identities: &[RuntimeEventIdentity],
) -> Result<(), DispatchError> {
    let mut seen = HashSet::new();
    for identity in identities {
        if !seen.insert(identity.id()) {
            return Err(DispatchError::DuplicateRuntimeEventIdentity {
                event_id: identity.id().to_owned(),
            });
        }
    }
    Ok(())
}

fn result_requests_cancellation(result: &Map<String, Value>) -> bool {
    matches!(
        result.get("outcome"),
        Some(Value::String(outcome)) if outcome == "aborted" || outcome == "cancelled"
    ) || result.get("stop_reason") == Some(&Value::String("aborted".to_owned()))
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

fn active_wait_for_session(
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

fn resource_kind_for_handle(handle: &str) -> Result<ResourceKind, DispatchError> {
    if handle.starts_with("wait_") {
        return Ok(ResourceKind::Wait);
    }
    if handle.starts_with("pub_") {
        return Ok(ResourceKind::ScheduledEvent);
    }
    Err(DispatchError::InvalidCancellationHandle {
        handle: handle.to_owned(),
    })
}

fn cancel_resource_in_transaction(
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
        ResourceKind::ScheduledEvent => {
            let Some(scheduled) = load_scheduled_event(transaction, handle)? else {
                return Err(DispatchError::CancellationResourceNotFound {
                    handle: handle.to_owned(),
                });
            };
            authorize_resource_cancellation(
                handle,
                &scheduled.source_agent_id,
                scheduled.source_session_id.as_ref().map(SessionId::as_str),
                source_agent_id,
                source_session_id,
            )?;
            match scheduled.status {
                ScheduledEventStatus::Pending => {
                    let event = scheduled_cancelled_event(
                        identity,
                        &scheduled,
                        reason,
                        source_agent_id,
                        source_session_id,
                    );
                    let event = append_runtime_event(transaction, event)?;
                    if !event.inserted {
                        return Err(corrupt_projection("scheduled_events", "status"));
                    }
                    Ok(ResourceCancellationOutcome {
                        handle: handle.to_owned(),
                        resource_kind,
                        status: ResourceCancellationStatus::Cancelled,
                        changed: true,
                        event: Some(event.event),
                    })
                }
                ScheduledEventStatus::Published => Ok(terminal_resource_cancellation(
                    handle,
                    resource_kind,
                    ResourceCancellationStatus::Published,
                )),
                ScheduledEventStatus::Cancelled => Ok(terminal_resource_cancellation(
                    handle,
                    resource_kind,
                    ResourceCancellationStatus::Cancelled,
                )),
                ScheduledEventStatus::Claimed => {
                    Err(corrupt_projection("scheduled_events", "status"))
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

fn next_due_scheduled_event(
    connection: &Connection,
    now_ms: i64,
) -> Result<Option<ScheduledEvent>, DispatchError> {
    let sql = format!(
        "SELECT {SCHEDULED_EVENT_COLUMNS}
         FROM scheduled_events AS scheduled
         JOIN journal_entries AS created
           ON created.event_id = scheduled.created_event_id
         WHERE scheduled.status = 'pending' AND scheduled.publish_at_ms <= ?1
         ORDER BY scheduled.publish_at_ms ASC, created.cursor ASC,
                  scheduled.handle ASC
         LIMIT 1"
    );
    let stored = connection
        .query_row(&sql, params![now_ms], StoredScheduledEvent::from_row)
        .optional()
        .map_err(|database| database_error("read next due scheduled event", database))?;
    stored.map(StoredScheduledEvent::into_model).transpose()
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

fn scheduled_publication_event(
    identity: &RuntimeEventIdentity,
    scheduled: &ScheduledEvent,
) -> Event {
    Event {
        id: identity.id().to_owned(),
        event_type: scheduled.event_type.clone(),
        source: format!("agent:{}", scheduled.source_agent_id),
        payload: scheduled.payload.clone(),
        idempotency_key: Some(format!(
            "agent.publish:{}:{}",
            scheduled.source_queue_item_id, scheduled.position
        )),
        caused_by: Some(scheduled.created_event_id.clone()),
        session_id: scheduled
            .source_session_id
            .as_ref()
            .map(ToString::to_string),
        run_id: scheduled.source_run_id.as_ref().map(ToString::to_string),
        turn_id: None,
        timestamp_ms: identity.timestamp_ms(),
        cursor: None,
    }
}

fn scheduled_published_event(
    identity: &RuntimeEventIdentity,
    scheduled: &ScheduledEvent,
    published: &Event,
) -> Event {
    let mut payload = Map::new();
    payload.insert("handle".to_owned(), Value::String(scheduled.handle.clone()));
    payload.insert(
        "source_agent_id".to_owned(),
        Value::String(scheduled.source_agent_id.clone()),
    );
    payload.insert(
        "source_queue_item_id".to_owned(),
        Value::String(scheduled.source_queue_item_id.to_string()),
    );
    payload.insert("position".to_owned(), Value::from(scheduled.position));
    payload.insert(
        "published_event_id".to_owned(),
        Value::String(published.id.clone()),
    );
    Event {
        id: identity.id().to_owned(),
        event_type: "runtime.scheduled_event.published".to_owned(),
        source: "zeta".to_owned(),
        payload,
        idempotency_key: Some(format!("scheduled_event.published:{}", scheduled.handle)),
        caused_by: Some(published.id.clone()),
        session_id: scheduled
            .source_session_id
            .as_ref()
            .map(ToString::to_string),
        run_id: scheduled.source_run_id.as_ref().map(ToString::to_string),
        turn_id: published.turn_id.clone(),
        timestamp_ms: identity.timestamp_ms(),
        cursor: None,
    }
}

fn scheduled_cancelled_event(
    identity: &RuntimeEventIdentity,
    scheduled: &ScheduledEvent,
    reason: Option<&str>,
    source_agent_id: Option<&str>,
    source_session_id: Option<&str>,
) -> Event {
    let mut payload = Map::new();
    payload.insert("handle".to_owned(), Value::String(scheduled.handle.clone()));
    payload.insert(
        "source_agent_id".to_owned(),
        Value::String(scheduled.source_agent_id.clone()),
    );
    payload.insert(
        "source_queue_item_id".to_owned(),
        Value::String(scheduled.source_queue_item_id.to_string()),
    );
    payload.insert("position".to_owned(), Value::from(scheduled.position));
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
        event_type: "runtime.scheduled_event.cancelled".to_owned(),
        source: "zeta".to_owned(),
        payload,
        idempotency_key: Some(format!("scheduled_event.cancelled:{}", scheduled.handle)),
        caused_by: Some(scheduled.created_event_id.clone()),
        session_id: scheduled
            .source_session_id
            .as_ref()
            .map(ToString::to_string),
        run_id: scheduled.source_run_id.as_ref().map(ToString::to_string),
        turn_id: None,
        timestamp_ms: identity.timestamp_ms(),
        cursor: None,
    }
}

fn completed_attempt_event(
    identity: &RuntimeEventIdentity,
    input: &Event,
    attempt: &Attempt,
    completion: &AttemptCompletion,
) -> Event {
    let mut payload = attempt_payload(attempt, AttemptStatus::Completed);
    payload.insert(
        "finished_at".to_owned(),
        Value::String(completion.finished_at.clone()),
    );
    payload.insert(
        "result".to_owned(),
        Value::Object(completion.result.clone()),
    );
    let summary = completion
        .result
        .get("summary")
        .or_else(|| completion.result.get("final_answer"));
    if let Some(Value::String(summary)) = summary {
        payload.insert("summary".to_owned(), Value::String(summary.clone()));
    }
    for key in ["events", "tool_calls", "usage"] {
        if let Some(value) = completion.result.get(key) {
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
    control: &CompletionControl,
    input: &Event,
    queue_item: &QueueItem,
    attempt: &Attempt,
    completed_attempt: &Event,
) -> Event {
    let session_id = attempt.session_id.as_ref().map(ToString::to_string);
    let run_id = attempt.run_id.as_ref().map(ToString::to_string);
    match control {
        CompletionControl::Publish {
            position,
            handle,
            event_type,
            payload,
            publish_at,
            immediate,
        } => {
            if *immediate {
                return Event {
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
                };
            }
            let mut scheduled = Map::new();
            scheduled.insert("handle".to_owned(), Value::String(handle.clone()));
            scheduled.insert("event_type".to_owned(), Value::String(event_type.clone()));
            scheduled.insert("payload".to_owned(), Value::Object(payload.clone()));
            scheduled.insert(
                "publish_at".to_owned(),
                publish_at
                    .as_ref()
                    .map(|at| Value::String(at.clone()))
                    .unwrap_or(Value::Null),
            );
            scheduled.insert(
                "source_agent_id".to_owned(),
                Value::String(attempt.target_agent.clone()),
            );
            scheduled.insert(
                "source_session_id".to_owned(),
                session_id.clone().map(Value::String).unwrap_or(Value::Null),
            );
            scheduled.insert(
                "source_queue_item_id".to_owned(),
                Value::String(queue_item.id.to_string()),
            );
            scheduled.insert("position".to_owned(), Value::from(*position));
            Event {
                id: identity.id().to_owned(),
                event_type: "runtime.scheduled_event.created".to_owned(),
                source: "zeta".to_owned(),
                payload: scheduled,
                idempotency_key: Some(format!("agent.schedule:{}:{position}", queue_item.id)),
                caused_by: Some(completed_attempt.id.clone()),
                session_id,
                run_id,
                turn_id: input.turn_id.clone(),
                timestamp_ms: identity.timestamp_ms(),
                cursor: None,
            }
        }
        CompletionControl::Wait {
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
                "project_generation".to_owned(),
                queue_item
                    .project_generation
                    .as_ref()
                    .map(|value| Value::String(value.clone()))
                    .unwrap_or(Value::Null),
            );
            Event {
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
            }
        }
        CompletionControl::Cancel { .. } => {
            unreachable!("cancel controls are applied against projected resources")
        }
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
    error_code: DispatchErrorCode,
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
    let failure_class = classify_error_code(failure.error_code);
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

fn attempt_payload(attempt: &Attempt, status: AttemptStatus) -> Map<String, Value> {
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
    if let Some(project_generation) = &attempt.project_generation {
        payload.insert(
            "project_generation".to_owned(),
            Value::String(project_generation.clone()),
        );
    }
    payload
}

fn queue_item_payload(queue_item: &QueueItem, status: QueueItemStatus) -> Map<String, Value> {
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

fn index_event(connection: &Connection, event: &Event) -> Result<(), DispatchError> {
    if event.event_type.starts_with("scheduler.tick.") {
        return index_recurring_schedule_tick(connection, event);
    }
    if event.event_type.starts_with("runtime.effect.") {
        return index_effect(connection, event);
    }
    if event.event_type.starts_with("runtime.wait.") {
        return index_wait(connection, event);
    }
    if event.event_type.starts_with("runtime.scheduled_event.") {
        return index_scheduled_event(connection, event);
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

fn index_recurring_schedule_tick(
    connection: &Connection,
    event: &Event,
) -> Result<(), DispatchError> {
    if event.source != "zeta:scheduler" {
        return Err(invalid_lifecycle(event, "source"));
    }
    let suffix = event.event_type.rsplit('.').next().unwrap_or_default();
    let status = parse_schedule_tick_status(event, suffix)?;
    if required_payload_string(event, "status", false)? != suffix {
        return Err(invalid_lifecycle(event, "status"));
    }
    let agent_id = required_runtime_id(event, "agent")?;
    let schedule_index = required_nonnegative_u64(event, "schedule_index")?;
    let schedule_index = i64::try_from(schedule_index)
        .map_err(|_error| invalid_lifecycle(event, "schedule_index"))?;
    let event_type = required_runtime_id(event, "event_type")?;
    if event_type != format!("agent.{agent_id}.scheduled") {
        return Err(invalid_lifecycle(event, "event_type"));
    }
    let cron = required_runtime_id(event, "cron")?;
    let timezone = optional_payload_string(event, "timezone")?.unwrap_or_default();
    let reason = required_runtime_id(event, "reason")?;
    let observed_at = required_runtime_id(event, "observed_at")?;
    lifecycle_timestamp_ms(event, "observed_at", &observed_at)?;

    if status == ScheduleTickStatus::Activated {
        let catchup = required_runtime_id(event, "catchup")?;
        if event.caused_by.is_some() {
            return Err(invalid_lifecycle(event, "caused_by"));
        }
        let expected_key =
            format!("scheduler:activated:{agent_id}:{schedule_index}:{cron}:{timezone}:{catchup}");
        if event.idempotency_key.as_deref() != Some(expected_key.as_str()) {
            return Err(invalid_lifecycle(event, "idempotency_key"));
        }
        connection
            .execute(
                "INSERT INTO recurring_schedules (
                    agent_id, schedule_index, cron, timezone, catchup,
                    event_type, activation_event_id, status,
                    last_published_at, next_at, reason, updated_at
                 ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7,
                           'activated', NULL, NULL, ?8, ?9)",
                params![
                    agent_id,
                    schedule_index,
                    cron,
                    timezone,
                    catchup,
                    event_type,
                    &event.id,
                    reason,
                    event.timestamp_ms,
                ],
            )
            .map_err(|database| database_error("project schedule activation", database))?;
        return Ok(());
    }

    let scheduled_at = required_runtime_id(event, "scheduled_at")?;
    let next_at = required_runtime_id(event, "next_at")?;
    lifecycle_timestamp_ms(event, "scheduled_at", &scheduled_at)?;
    lifecycle_timestamp_ms(event, "next_at", &next_at)?;
    let published_event_id = optional_payload_string(event, "published_event_id")?;
    if status == ScheduleTickStatus::Published || status == ScheduleTickStatus::Skipped {
        let published_event_id = published_event_id
            .as_deref()
            .ok_or_else(|| invalid_lifecycle(event, "published_event_id"))?;
        if event.caused_by.as_deref() != Some(published_event_id) {
            return Err(invalid_lifecycle(event, "published_event_id"));
        }
        let published = entry_by_field(connection, "event_id", published_event_id)?
            .ok_or_else(|| invalid_lifecycle(event, "published_event_id"))?
            .event;
        let expected_publication_key = format!("schedule:{agent_id}:{cron}:{scheduled_at}");
        if published.event_type != event_type
            || published.idempotency_key.as_deref() != Some(expected_publication_key.as_str())
            || published.payload.get("timestamp") != Some(&Value::String(scheduled_at.clone()))
        {
            return Err(invalid_lifecycle(event, "published_event_id"));
        }
    } else if published_event_id.is_some() || event.caused_by.is_some() {
        return Err(invalid_lifecycle(event, "published_event_id"));
    }
    let expected_key =
        format!("scheduler:{suffix}:{agent_id}:{schedule_index}:{cron}:{timezone}:{scheduled_at}");
    if event.idempotency_key.as_deref() != Some(expected_key.as_str()) {
        return Err(invalid_lifecycle(event, "idempotency_key"));
    }
    let existing = connection
        .query_row(
            "SELECT event_type FROM recurring_schedules
             WHERE agent_id = ?1 AND schedule_index = ?2
               AND cron = ?3 AND timezone = ?4",
            params![agent_id, schedule_index, cron, timezone],
            |row| row.get::<_, String>(0),
        )
        .optional()
        .map_err(|database| database_error("read recurring schedule decision", database))?;
    if existing
        .as_deref()
        .is_some_and(|stored| stored != event_type)
    {
        return Err(invalid_lifecycle(event, "event_type"));
    }
    let last_published_at =
        (status == ScheduleTickStatus::Published).then_some(scheduled_at.as_str());
    connection
        .execute(
            "INSERT INTO recurring_schedules (
                agent_id, schedule_index, cron, timezone, catchup,
                event_type, activation_event_id, status,
                last_published_at, next_at, reason, updated_at
             ) VALUES (?1, ?2, ?3, ?4, NULL, ?5, NULL, ?6,
                       ?7, ?8, ?9, ?10)
             ON CONFLICT(agent_id, schedule_index, cron, timezone) DO UPDATE SET
                status = excluded.status,
                last_published_at = COALESCE(
                    excluded.last_published_at,
                    recurring_schedules.last_published_at
                ),
                next_at = excluded.next_at,
                reason = excluded.reason,
                updated_at = excluded.updated_at",
            params![
                agent_id,
                schedule_index,
                cron,
                timezone,
                event_type,
                schedule_tick_status_str(status),
                last_published_at,
                next_at,
                reason,
                event.timestamp_ms,
            ],
        )
        .map_err(|database| database_error("project recurring schedule decision", database))?;
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

fn index_scheduled_event(connection: &Connection, event: &Event) -> Result<(), DispatchError> {
    match event.event_type.as_str() {
        "runtime.scheduled_event.created" => index_scheduled_event_created(connection, event),
        "runtime.scheduled_event.published" => {
            index_scheduled_event_terminal(connection, event, ScheduledEventStatus::Published)
        }
        "runtime.scheduled_event.cancelled" => {
            index_scheduled_event_terminal(connection, event, ScheduledEventStatus::Cancelled)
        }
        _ => Err(invalid_lifecycle(event, "event_type")),
    }
}

fn index_scheduled_event_created(
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
            "INSERT INTO scheduled_events (
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
        .map_err(|database| database_error("project scheduled event creation", database))?;
    Ok(())
}

fn index_scheduled_event_terminal(
    connection: &Connection,
    event: &Event,
    status: ScheduledEventStatus,
) -> Result<(), DispatchError> {
    let handle = required_runtime_id(event, "handle")?;
    let published_event_id = if status == ScheduledEventStatus::Published {
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
            "UPDATE scheduled_events
             SET status = ?1, published_event_id = ?2,
                 terminal_event_id = ?3, updated_at = ?4
             WHERE handle = ?5 AND status IN ('pending', 'claimed')",
            params![
                scheduled_event_status_str(status),
                published_event_id,
                &event.id,
                event.timestamp_ms,
                handle,
            ],
        )
        .map_err(|database| database_error("project scheduled event terminal", database))?;
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
    for prefix in ["runtime.", "zeta.", "scheduler.tick."] {
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

fn scheduled_event_status_str(status: ScheduledEventStatus) -> &'static str {
    match status {
        ScheduledEventStatus::Pending => "pending",
        ScheduledEventStatus::Claimed => "claimed",
        ScheduledEventStatus::Published => "published",
        ScheduledEventStatus::Cancelled => "cancelled",
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

fn effect_status_is_terminal(status: EffectStatus) -> bool {
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

fn parse_schedule_tick_status(
    event: &Event,
    value: &str,
) -> Result<ScheduleTickStatus, DispatchError> {
    match value {
        "activated" => Ok(ScheduleTickStatus::Activated),
        "published" => Ok(ScheduleTickStatus::Published),
        "skipped" => Ok(ScheduleTickStatus::Skipped),
        "missed" => Ok(ScheduleTickStatus::Missed),
        _ => Err(invalid_lifecycle(event, "status")),
    }
}

fn schedule_tick_status_str(status: ScheduleTickStatus) -> &'static str {
    match status {
        ScheduleTickStatus::Activated => "activated",
        ScheduleTickStatus::Published => "published",
        ScheduleTickStatus::Skipped => "skipped",
        ScheduleTickStatus::Missed => "missed",
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

fn rebuild_projections_in_transaction(connection: &Connection) -> Result<usize, DispatchError> {
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
            "UPDATE scheduled_events SET status = 'pending' WHERE status = 'claimed'",
            [],
        )
        .map_err(|error| database_error("recover claimed scheduled events", error))?;
    Ok(entries.len())
}

const QUEUE_ITEM_COLUMNS: &str = "queue.queue_item_id, queue.event_id,
    queue.target_agent, queue.project_generation, queue.session_id,
    queue.lock_keys_json, queue.input_cursor,
    CASE WHEN claim.queue_item_id IS NULL THEN queue.status ELSE 'claimed' END,
    queue.available_at, claim.worker_name, claim.claimed_until,
    queue.cancel_requested_event_id, queue.cancel_requested_at,
    queue.cancel_reason, queue.attempt_count, queue.last_error, queue.updated_at";

fn load_queue_item(
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

fn load_queue_items(connection: &Connection) -> Result<Vec<QueueItem>, DispatchError> {
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

const ATTEMPT_COLUMNS: &str = "attempt.attempt_id, attempt.queue_item_id,
    attempt.event_id, attempt.attempt_number, attempt.target_agent,
    attempt.worker_name, attempt.status, attempt.started_at,
    attempt.finished_at, attempt.error, attempt.session_id, attempt.run_id,
    attempt.project_generation";

fn load_attempts(connection: &Connection) -> Result<Vec<Attempt>, DispatchError> {
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

fn load_waits(connection: &Connection) -> Result<Vec<Wait>, DispatchError> {
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

const SCHEDULED_EVENT_COLUMNS: &str = "scheduled.handle,
    scheduled.event_type, scheduled.payload_json, scheduled.publish_at_ms,
    scheduled.source_agent_id, scheduled.source_session_id,
    scheduled.source_run_id, scheduled.source_queue_item_id,
    scheduled.position, scheduled.created_event_id, scheduled.status,
    scheduled.published_event_id, scheduled.terminal_event_id,
    scheduled.updated_at";

fn load_scheduled_event(
    connection: &Connection,
    handle: &str,
) -> Result<Option<ScheduledEvent>, DispatchError> {
    let sql = format!(
        "SELECT {SCHEDULED_EVENT_COLUMNS}
         FROM scheduled_events AS scheduled
         WHERE scheduled.handle = ?1"
    );
    let stored = connection
        .query_row(&sql, params![handle], StoredScheduledEvent::from_row)
        .optional()
        .map_err(|database| database_error("read scheduled event", database))?;
    stored.map(StoredScheduledEvent::into_model).transpose()
}

fn load_scheduled_events(connection: &Connection) -> Result<Vec<ScheduledEvent>, DispatchError> {
    let sql = format!(
        "SELECT {SCHEDULED_EVENT_COLUMNS}
         FROM scheduled_events AS scheduled
         JOIN journal_entries AS created
           ON created.event_id = scheduled.created_event_id
         ORDER BY scheduled.publish_at_ms ASC, created.cursor ASC,
                  scheduled.handle ASC"
    );
    let mut statement = connection
        .prepare(&sql)
        .map_err(|database| database_error("prepare scheduled event read", database))?;
    let rows = statement
        .query_map([], StoredScheduledEvent::from_row)
        .map_err(|database| database_error("read scheduled events", database))?;
    let mut scheduled_events = Vec::new();
    for row in rows {
        let stored = row.map_err(|database| database_error("read scheduled event", database))?;
        scheduled_events.push(stored.into_model()?);
    }
    Ok(scheduled_events)
}

struct StoredScheduledEvent {
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

impl StoredScheduledEvent {
    fn from_row(row: &Row<'_>) -> rusqlite::Result<Self> {
        Ok(StoredScheduledEvent {
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

    fn into_model(self) -> Result<ScheduledEvent, DispatchError> {
        let payload = match serde_json::from_str(&self.payload_json) {
            Ok(Value::Object(payload)) => payload,
            _ => return Err(corrupt_projection("scheduled_events", "payload_json")),
        };
        let source_session_id = self
            .source_session_id
            .map(|value| SessionId::from_str(&value))
            .transpose()
            .map_err(|_error| corrupt_projection("scheduled_events", "source_session_id"))?;
        let source_run_id = self
            .source_run_id
            .map(|value| RunId::from_str(&value))
            .transpose()
            .map_err(|_error| corrupt_projection("scheduled_events", "source_run_id"))?;
        let source_queue_item_id = QueueItemId::from_str(&self.source_queue_item_id)
            .map_err(|_error| corrupt_projection("scheduled_events", "source_queue_item_id"))?;
        let position = u64::try_from(self.position)
            .map_err(|_error| corrupt_projection("scheduled_events", "position"))?;
        let status = match self.status.as_str() {
            "pending" => ScheduledEventStatus::Pending,
            "claimed" => ScheduledEventStatus::Claimed,
            "published" => ScheduledEventStatus::Published,
            "cancelled" => ScheduledEventStatus::Cancelled,
            _ => return Err(corrupt_projection("scheduled_events", "status")),
        };
        Ok(ScheduledEvent {
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

fn load_latest_running_attempt(
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
    project_generation: Option<String>,
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
            project_generation: row.get(12)?,
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
            project_generation: self.project_generation,
        })
    }
}

fn corrupt_projection(table: &'static str, field: &'static str) -> DispatchError {
    DispatchError::CorruptProjection { table, field }
}

fn positive_u64_projection(
    value: i64,
    table: &'static str,
    field: &'static str,
) -> Result<u64, DispatchError> {
    if value <= 0 {
        return Err(corrupt_projection(table, field));
    }
    Ok(value as u64)
}

fn nonnegative_u32_projection(
    value: i64,
    table: &'static str,
    field: &'static str,
) -> Result<u32, DispatchError> {
    if value < 0 || value > i64::from(u32::MAX) {
        return Err(corrupt_projection(table, field));
    }
    Ok(value as u32)
}

fn validate_event_identity(event: &Event) -> Result<(), DispatchError> {
    if event.id.is_empty() {
        return Err(AppendError::EmptyId.into());
    }
    if event.event_type.is_empty() {
        return Err(AppendError::EmptyEventType.into());
    }
    if event.source.is_empty() {
        return Err(AppendError::EmptySource.into());
    }
    Ok(())
}

fn append_in_transaction(
    transaction: &Transaction<'_>,
    event: Event,
) -> Result<AppendOutcome, DispatchError> {
    if let Some(entry) = entry_by_field(transaction, "event_id", &event.id)? {
        return Ok(AppendOutcome {
            event: entry.event,
            inserted: false,
        });
    }
    if let Some(idempotency_key) = &event.idempotency_key {
        if let Some(entry) = entry_by_field(transaction, "idempotency_key", idempotency_key)? {
            return Ok(AppendOutcome {
                event: entry.event,
                inserted: false,
            });
        }
    }

    let anchor = last_entry_anchor(transaction)?;
    let (cursor, previous_address) = match anchor {
        Some((cursor, address)) => {
            let Some(cursor) = cursor.checked_add(1) else {
                return Err(AppendError::CursorExhausted.into());
            };
            (cursor, Some(address))
        }
        None => (1, None),
    };
    if cursor > i64::MAX as u64 {
        return Err(AppendError::CursorExhausted.into());
    }
    let entry = JournalEntry::new(event, cursor, previous_address)?;
    let previous_address = entry
        .previous_address
        .map(|address| address.as_bytes().to_vec());
    transaction
        .execute(
            "INSERT INTO journal_entries (
                cursor, event_id, event_type, source, payload_bytes,
                payload_address, idempotency_key, caused_by, session_id,
                run_id, turn_id, timestamp_ms, previous_address, entry_address
             ) VALUES (
                ?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14
             )",
            params![
                cursor as i64,
                &entry.event.id,
                &entry.event.event_type,
                &entry.event.source,
                &entry.payload_bytes,
                entry.payload_address.as_bytes().as_slice(),
                entry.event.idempotency_key.as_deref(),
                entry.event.caused_by.as_deref(),
                entry.event.session_id.as_deref(),
                entry.event.run_id.as_deref(),
                entry.event.turn_id.as_deref(),
                entry.event.timestamp_ms,
                previous_address,
                entry.entry_address.as_bytes().as_slice(),
            ],
        )
        .map_err(|error| database_error("insert journal entry", error))?;
    Ok(AppendOutcome {
        event: entry.event,
        inserted: true,
    })
}

fn entry_by_field(
    connection: &Connection,
    field: &'static str,
    value: &str,
) -> Result<Option<JournalEntry>, DispatchError> {
    let sql = if field == "event_id" {
        format!("SELECT {ENTRY_COLUMNS} FROM journal_entries WHERE event_id = ?1")
    } else if field == "idempotency_key" {
        format!("SELECT {ENTRY_COLUMNS} FROM journal_entries WHERE idempotency_key = ?1")
    } else {
        return Err(DispatchError::Database {
            operation: "select journal entry",
            message: format!("unsupported lookup field {field:?}"),
        });
    };
    let stored = connection
        .query_row(&sql, params![value], StoredEntry::from_row)
        .optional()
        .map_err(|error| database_error("select journal entry", error))?;
    stored.map(StoredEntry::into_entry).transpose()
}

fn last_entry_anchor(connection: &Connection) -> Result<Option<(u64, Hash)>, DispatchError> {
    let stored = connection
        .query_row(
            "SELECT cursor, entry_address
             FROM journal_entries ORDER BY cursor DESC LIMIT 1",
            [],
            |row| Ok((row.get::<_, i64>(0)?, row.get::<_, Vec<u8>>(1)?)),
        )
        .optional()
        .map_err(|error| database_error("read journal head", error))?;
    let Some((cursor, address)) = stored else {
        return Ok(None);
    };
    let cursor = decode_cursor(cursor)?;
    let address = decode_hash(Some(cursor), "entry_address", address)?;
    Ok(Some((cursor, address)))
}

fn load_entries(
    connection: &Connection,
    newest_first: bool,
) -> Result<Vec<JournalEntry>, DispatchError> {
    let order = if newest_first { "DESC" } else { "ASC" };
    let sql = format!("SELECT {ENTRY_COLUMNS} FROM journal_entries ORDER BY cursor {order}");
    let mut statement = connection
        .prepare(&sql)
        .map_err(|error| database_error("prepare journal read", error))?;
    let rows = statement
        .query_map([], StoredEntry::from_row)
        .map_err(|error| database_error("read journal entries", error))?;
    let mut entries = Vec::new();
    for row in rows {
        let stored = row.map_err(|error| database_error("read journal entry", error))?;
        entries.push(stored.into_entry()?);
    }
    Ok(entries)
}

fn event_matches(event: &Event, filter: &Filter) -> bool {
    if let Some(expected) = &filter.event_type {
        if &event.event_type != expected {
            return false;
        }
    }
    if let Some(prefix) = &filter.event_type_prefix {
        if !event.event_type.starts_with(prefix) {
            return false;
        }
    }
    if let Some(expected) = &filter.session_id {
        if event.session_id.as_ref() != Some(expected) {
            return false;
        }
    }
    if let Some(expected) = &filter.run_id {
        if event.run_id.as_ref() != Some(expected) {
            return false;
        }
    }
    if let Some(expected) = &filter.turn_id {
        if event.turn_id.as_ref() != Some(expected) {
            return false;
        }
    }
    if let Some(expected) = &filter.caused_by {
        if event.caused_by.as_ref() != Some(expected) {
            return false;
        }
    }
    if let Some(after_cursor) = filter.after_cursor {
        let Some(cursor) = event.cursor else {
            return false;
        };
        if cursor <= after_cursor {
            return false;
        }
    }
    true
}

struct StoredEntry {
    cursor: i64,
    event_id: String,
    event_type: String,
    source: String,
    payload_bytes: Vec<u8>,
    payload_address: Vec<u8>,
    idempotency_key: Option<String>,
    caused_by: Option<String>,
    session_id: Option<String>,
    run_id: Option<String>,
    turn_id: Option<String>,
    timestamp_ms: i64,
    previous_address: Option<Vec<u8>>,
    entry_address: Vec<u8>,
}

impl StoredEntry {
    fn from_row(row: &Row<'_>) -> rusqlite::Result<Self> {
        Ok(StoredEntry {
            cursor: row.get(0)?,
            event_id: row.get(1)?,
            event_type: row.get(2)?,
            source: row.get(3)?,
            payload_bytes: row.get(4)?,
            payload_address: row.get(5)?,
            idempotency_key: row.get(6)?,
            caused_by: row.get(7)?,
            session_id: row.get(8)?,
            run_id: row.get(9)?,
            turn_id: row.get(10)?,
            timestamp_ms: row.get(11)?,
            previous_address: row.get(12)?,
            entry_address: row.get(13)?,
        })
    }

    fn into_entry(self) -> Result<JournalEntry, DispatchError> {
        let cursor = decode_cursor(self.cursor)?;
        let payload: Map<String, Value> =
            serde_json::from_slice(&self.payload_bytes).map_err(|_error| {
                DispatchError::CorruptJournal {
                    cursor: Some(cursor),
                    field: "payload_bytes",
                }
            })?;
        let payload_address = decode_hash(Some(cursor), "payload_address", self.payload_address)?;
        let previous_address = self
            .previous_address
            .map(|address| decode_hash(Some(cursor), "previous_address", address))
            .transpose()?;
        let entry_address = decode_hash(Some(cursor), "entry_address", self.entry_address)?;
        Ok(JournalEntry {
            event: Event {
                id: self.event_id,
                event_type: self.event_type,
                source: self.source,
                payload,
                idempotency_key: self.idempotency_key,
                caused_by: self.caused_by,
                session_id: self.session_id,
                run_id: self.run_id,
                turn_id: self.turn_id,
                timestamp_ms: self.timestamp_ms,
                cursor: Some(cursor),
            },
            payload_bytes: self.payload_bytes,
            payload_address,
            previous_address,
            entry_address,
        })
    }
}

fn decode_cursor(cursor: i64) -> Result<u64, DispatchError> {
    if cursor <= 0 {
        return Err(DispatchError::CorruptJournal {
            cursor: None,
            field: "cursor",
        });
    }
    Ok(cursor as u64)
}

fn decode_hash(
    cursor: Option<u64>,
    field: &'static str,
    bytes: Vec<u8>,
) -> Result<Hash, DispatchError> {
    let bytes: Result<[u8; 32], Vec<u8>> = bytes.try_into();
    let Ok(bytes) = bytes else {
        return Err(DispatchError::CorruptJournal { cursor, field });
    };
    Ok(Hash::from_bytes(bytes))
}

fn database_error(operation: &'static str, error: rusqlite::Error) -> DispatchError {
    let code = error.sqlite_error_code();
    if code == Some(ErrorCode::DatabaseBusy) || code == Some(ErrorCode::DatabaseLocked) {
        return DispatchError::StorageBusy { operation };
    }
    DispatchError::Database {
        operation,
        message: error.to_string(),
    }
}
