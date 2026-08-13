//! Private SQLite persistence for journal-v0.

mod attempts;
mod cancellation;
mod coordination;
mod effects;
mod journal;
mod projection;
mod resources;
mod routing;
mod sessions;

use self::projection::rebuild_projections_in_transaction;

use std::fmt;
use std::path::Path;
use std::time::Duration;

use rusqlite::ffi::ErrorCode;
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde_json::{Map, Value};
use zeta_journal::{AppendError, Event, VerificationError};

use crate::identity::{QueueItemId, SessionId};
use crate::routing::SessionError;
use crate::state::TransitionError;

const BASE_EPOCH: i64 = 2;
const PROJECTION_EPOCH: i64 = 7;
const BUSY_TIMEOUT: Duration = Duration::from_secs(5);

const CREATE_SCHEMA: &str = "
    CREATE TABLE dispatch_schema (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        base_epoch INTEGER NOT NULL CHECK (base_epoch > 0),
        projection_epoch INTEGER NOT NULL CHECK (projection_epoch >= 0)
    ) STRICT;
    INSERT INTO dispatch_schema (singleton, base_epoch, projection_epoch)
    VALUES (1, 2, 7);

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
        project_revision TEXT,
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
        project_revision TEXT,
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
        project_revision TEXT,
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
    CREATE TABLE deferred_publications (
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
    CREATE INDEX deferred_publications_due_order
        ON deferred_publications(status, publish_at_ms, created_event_id, handle);
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
";

const DROP_PROJECTIONS: &str = "
    DROP TABLE IF EXISTS recurring_schedules;
    DROP TABLE IF EXISTS effects;
    DROP TABLE IF EXISTS scheduled_events;
    DROP TABLE IF EXISTS deferred_publications;
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
                "cancellation handle {handle:?} must identify a wait or deferred publication"
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
        "deferred_publications",
        "effects",
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
        return Err(invalid_lifecycle(event, field));
    };
    if !allow_empty && value.is_empty() {
        return Err(invalid_lifecycle(event, field));
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
        Some(_value) => Err(invalid_lifecycle(event, field)),
        None => Ok(None),
    }
}

fn validate_optional_runtime_id(
    event: &Event,
    field: &'static str,
    value: Option<&str>,
) -> Result<(), DispatchError> {
    if value == Some("") {
        return Err(invalid_lifecycle(event, field));
    }
    Ok(())
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
