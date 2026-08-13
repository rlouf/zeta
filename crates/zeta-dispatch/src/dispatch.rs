//! Public durable runtime inputs and read models.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use zeta_journal::Event;

use crate::identity::{AttemptId, ClaimToken, QueueItemId, RunId, RuntimeIdParseError, SessionId};
use crate::routing::RouteDecision;
use crate::state::{AttemptFailureCode, AttemptStatus, QueueItemStatus, RetryPolicy};

/// Supplies deterministic identity and time for one runtime-owned event.
///
/// Dispatch owns lifecycle payloads and journal metadata except for these two
/// nondeterministic values. Keeping them explicit makes retries and tests
/// independent of ambient clocks and random-number generators.
///
/// # Examples
///
/// ```
/// let identity = zeta_dispatch::RuntimeEventIdentity::new("evt_runtime_1", 42)?;
/// assert_eq!(identity.id(), "evt_runtime_1");
/// assert_eq!(identity.timestamp_ms(), 42);
/// # Ok::<(), zeta_dispatch::RuntimeIdParseError>(())
/// ```
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RuntimeEventIdentity {
    id: String,
    timestamp_ms: i64,
}

impl RuntimeEventIdentity {
    /// Creates one runtime-event identity after rejecting an empty id.
    ///
    /// # Errors
    ///
    /// Returns [`RuntimeIdParseError`] when `id` is empty.
    pub fn new(id: impl Into<String>, timestamp_ms: i64) -> Result<Self, RuntimeIdParseError> {
        let id = id.into();
        if id.is_empty() {
            return Err(RuntimeIdParseError::new("runtime event"));
        }
        Ok(RuntimeEventIdentity { id, timestamp_ms })
    }

    /// Returns the caller-supplied opaque event id.
    pub fn id(&self) -> &str {
        &self.id
    }

    /// Returns the caller-supplied Unix time in milliseconds.
    pub fn timestamp_ms(&self) -> i64 {
        self.timestamp_ms
    }
}

/// Describes one durable event-to-agent routing read model.
///
/// Claims and leases are coordination state. Their visible owner and deadline
/// are included for inspection, but claim tokens are returned only by claim
/// operations.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct QueueItem {
    #[serde(rename = "queue_item_id")]
    pub(crate) id: QueueItemId,
    pub(crate) event_id: String,
    pub(crate) target_agent: String,
    pub(crate) project_generation: Option<String>,
    pub(crate) session_id: Option<SessionId>,
    pub(crate) lock_keys: Vec<String>,
    pub(crate) input_cursor: u64,
    pub(crate) status: QueueItemStatus,
    pub(crate) available_at: Option<i64>,
    pub(crate) claimed_by: Option<String>,
    pub(crate) claimed_until: Option<i64>,
    pub(crate) cancellation_requested_event_id: Option<String>,
    pub(crate) cancellation_requested_at: Option<i64>,
    pub(crate) cancellation_reason: Option<String>,
    pub(crate) attempt_count: u32,
    pub(crate) last_error: Option<String>,
    pub(crate) updated_at: i64,
}

impl QueueItem {
    /// Returns the retry-stable queue item identity.
    pub fn id(&self) -> &QueueItemId {
        &self.id
    }

    /// Returns the triggering journal event id.
    pub fn event_id(&self) -> &str {
        &self.event_id
    }

    /// Returns the bound agent, or an empty string before routing.
    pub fn target_agent(&self) -> &str {
        &self.target_agent
    }

    /// Returns the authored project generation selected during routing.
    pub fn project_generation(&self) -> Option<&str> {
        self.project_generation.as_deref()
    }

    /// Returns the session resolved before execution.
    pub fn session_id(&self) -> Option<&SessionId> {
        self.session_id.as_ref()
    }

    /// Returns authored exclusion keys in declaration order.
    pub fn lock_keys(&self) -> &[String] {
        &self.lock_keys
    }

    /// Returns the triggering event's journal cursor.
    pub fn input_cursor(&self) -> u64 {
        self.input_cursor
    }

    /// Returns the projected lifecycle state.
    pub fn status(&self) -> QueueItemStatus {
        self.status
    }

    /// Returns the earliest execution time for available work.
    pub fn available_at(&self) -> Option<i64> {
        self.available_at
    }

    /// Returns the current coordination owner when one exists.
    pub fn claimed_by(&self) -> Option<&str> {
        self.claimed_by.as_deref()
    }

    /// Returns the current lease deadline in Unix milliseconds.
    pub fn claimed_until(&self) -> Option<i64> {
        self.claimed_until
    }

    /// Returns the durable cancellation-intent event when one exists.
    pub fn cancellation_requested_event_id(&self) -> Option<&str> {
        self.cancellation_requested_event_id.as_deref()
    }

    /// Returns when cancellation intent first became durable.
    pub fn cancellation_requested_at(&self) -> Option<i64> {
        self.cancellation_requested_at
    }

    /// Returns the first durable cancellation reason when supplied.
    pub fn cancellation_reason(&self) -> Option<&str> {
        self.cancellation_reason.as_deref()
    }

    /// Returns the largest started attempt number.
    pub fn attempt_count(&self) -> u32 {
        self.attempt_count
    }

    /// Returns the latest projected execution error.
    pub fn last_error(&self) -> Option<&str> {
        self.last_error.as_deref()
    }

    /// Returns the lifecycle update time in Unix milliseconds.
    pub fn updated_at(&self) -> i64 {
        self.updated_at
    }
}

/// Describes one durable invocation attempt read model.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct Attempt {
    #[serde(rename = "attempt_id")]
    pub(crate) id: AttemptId,
    pub(crate) queue_item_id: QueueItemId,
    pub(crate) event_id: String,
    pub(crate) attempt_number: u32,
    pub(crate) target_agent: String,
    pub(crate) worker_name: Option<String>,
    pub(crate) status: AttemptStatus,
    pub(crate) started_at: String,
    pub(crate) finished_at: Option<String>,
    pub(crate) error: Option<String>,
    pub(crate) session_id: Option<SessionId>,
    pub(crate) run_id: Option<RunId>,
    pub(crate) project_generation: Option<String>,
}

impl Attempt {
    /// Returns the numbered attempt identity.
    pub fn id(&self) -> &AttemptId {
        &self.id
    }

    /// Returns the queue item this invocation advances.
    pub fn queue_item_id(&self) -> &QueueItemId {
        &self.queue_item_id
    }

    /// Returns the triggering journal event id.
    pub fn event_id(&self) -> &str {
        &self.event_id
    }

    /// Returns the positive attempt number.
    pub fn attempt_number(&self) -> u32 {
        self.attempt_number
    }

    /// Returns the bound authored agent id.
    pub fn target_agent(&self) -> &str {
        &self.target_agent
    }

    /// Returns the worker that started the attempt.
    pub fn worker_name(&self) -> Option<&str> {
        self.worker_name.as_deref()
    }

    /// Returns the projected attempt state.
    pub fn status(&self) -> AttemptStatus {
        self.status
    }

    /// Returns the producer-supplied start timestamp.
    pub fn started_at(&self) -> &str {
        &self.started_at
    }

    /// Returns the producer-supplied terminal timestamp.
    pub fn finished_at(&self) -> Option<&str> {
        self.finished_at.as_deref()
    }

    /// Returns the terminal error text when one was recorded.
    pub fn error(&self) -> Option<&str> {
        self.error.as_deref()
    }

    /// Returns the pre-execution session identity.
    pub fn session_id(&self) -> Option<&SessionId> {
        self.session_id.as_ref()
    }

    /// Returns the invocation run identity.
    pub fn run_id(&self) -> Option<&RunId> {
        self.run_id.as_ref()
    }

    /// Returns the authored project generation selected during routing.
    pub fn project_generation(&self) -> Option<&str> {
        self.project_generation.as_deref()
    }
}

/// Returns the decisions and durable lifecycle events from one routing pass.
#[derive(Clone, Debug, PartialEq)]
pub struct RoutingOutcome {
    pub(crate) decisions: Vec<RouteDecision>,
    pub(crate) events: Vec<Event>,
}

/// Proves one worker's current live ownership of a queue item.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct QueueClaim {
    pub(crate) queue_item_id: QueueItemId,
    pub(crate) worker_name: String,
    pub(crate) token: ClaimToken,
    pub(crate) claimed_until: i64,
}

impl QueueClaim {
    /// Returns the owned queue item.
    pub fn queue_item_id(&self) -> &QueueItemId {
        &self.queue_item_id
    }

    /// Returns the worker name included in every fencing check.
    pub fn worker_name(&self) -> &str {
        &self.worker_name
    }

    /// Returns the opaque fencing token.
    pub fn token(&self) -> &ClaimToken {
        &self.token
    }

    /// Returns the original lease deadline in Unix milliseconds.
    ///
    /// Renewing a claim updates durable coordination state, not this immutable
    /// receipt. Use [`crate::Dispatch::claim_is_current`] for authority.
    pub fn claimed_until(&self) -> i64 {
        self.claimed_until
    }

    /// Returns a receipt with another token for an explicit fencing check.
    pub fn with_token(&self, token: ClaimToken) -> Self {
        QueueClaim {
            queue_item_id: self.queue_item_id.clone(),
            worker_name: self.worker_name.clone(),
            token,
            claimed_until: self.claimed_until,
        }
    }
}

/// Describes one currently held mutual-exclusion key.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct LockLease {
    pub(crate) key: String,
    pub(crate) owner: ClaimToken,
    pub(crate) acquired_at: i64,
    pub(crate) expires_at: i64,
}

impl LockLease {
    /// Returns the authored exclusion key.
    pub fn key(&self) -> &str {
        &self.key
    }

    /// Returns the claim token that owns the lock.
    pub fn owner(&self) -> &ClaimToken {
        &self.owner
    }

    /// Returns the acquisition time in Unix milliseconds.
    pub fn acquired_at(&self) -> i64 {
        self.acquired_at
    }

    /// Returns the exclusive lease deadline in Unix milliseconds.
    pub fn expires_at(&self) -> i64 {
        self.expires_at
    }
}

/// Returns the durable facts committed when a fenced attempt begins.
#[derive(Clone, Debug, PartialEq)]
pub struct StartedAttempt {
    pub(crate) attempt: Attempt,
    pub(crate) events: Vec<Event>,
}

/// Describes the durable lifecycle of one event wait.
#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum WaitStatus {
    /// A matching event or deadline may consume the wait.
    Active,
    /// One event resumed the waiting session.
    Matched,
    /// The deadline resumed the waiting session.
    TimedOut,
    /// An authorized request terminated the wait.
    Cancelled,
}

/// Describes one durable wait read model.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct Wait {
    pub(crate) handle: String,
    pub(crate) agent_id: String,
    pub(crate) session_id: SessionId,
    pub(crate) event_type: String,
    pub(crate) fields: Map<String, Value>,
    pub(crate) deadline_ms: Option<i64>,
    pub(crate) source_queue_item_id: QueueItemId,
    pub(crate) project_generation: Option<String>,
    pub(crate) created_event_id: String,
    pub(crate) status: WaitStatus,
    pub(crate) matched_event_id: Option<String>,
    pub(crate) terminal_event_id: Option<String>,
    pub(crate) updated_at: i64,
}

impl Wait {
    /// Returns the retry-stable request handle.
    pub fn handle(&self) -> &str {
        &self.handle
    }

    /// Returns the agent resumed by this wait.
    pub fn agent_id(&self) -> &str {
        &self.agent_id
    }

    /// Returns the single session that owns this wait.
    pub fn session_id(&self) -> &SessionId {
        &self.session_id
    }

    /// Returns the exact accepted event type.
    pub fn event_type(&self) -> &str {
        &self.event_type
    }

    /// Returns required top-level payload fields.
    pub fn fields(&self) -> &Map<String, Value> {
        &self.fields
    }

    /// Returns the optional Unix deadline in milliseconds.
    pub fn deadline_ms(&self) -> Option<i64> {
        self.deadline_ms
    }

    /// Returns the queue item whose completion created the wait.
    pub fn source_queue_item_id(&self) -> &QueueItemId {
        &self.source_queue_item_id
    }

    /// Returns the generation used for its continuation.
    pub fn project_generation(&self) -> Option<&str> {
        self.project_generation.as_deref()
    }

    /// Returns the lifecycle fact that created the wait.
    pub fn created_event_id(&self) -> &str {
        &self.created_event_id
    }

    /// Returns the current event-sourced state.
    pub fn status(&self) -> WaitStatus {
        self.status
    }

    /// Returns the external event that matched this wait.
    pub fn matched_event_id(&self) -> Option<&str> {
        self.matched_event_id.as_deref()
    }

    /// Returns the matched, timeout, or cancellation fact.
    pub fn terminal_event_id(&self) -> Option<&str> {
        self.terminal_event_id.as_deref()
    }

    /// Returns the latest lifecycle time in Unix milliseconds.
    pub fn updated_at(&self) -> i64 {
        self.updated_at
    }
}

/// Describes the highest-priority current activity in one session.
#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SessionActivityStatus {
    /// An attempt is currently running.
    Running,
    /// At least one turn is queued without a running attempt.
    Queued,
    /// No turn is executable and an active wait exists.
    Waiting,
    /// No running, queued, or waiting work remains.
    Idle,
}

/// Summarizes the active wait shown in a session catalog.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct SessionActiveWait {
    pub(crate) handle: String,
    pub(crate) event_type: String,
    pub(crate) fields: Map<String, Value>,
    pub(crate) deadline_ms: Option<i64>,
}

impl SessionActiveWait {
    /// Returns the retry-stable wait handle.
    pub fn handle(&self) -> &str {
        &self.handle
    }

    /// Returns the exact event type awaited.
    pub fn event_type(&self) -> &str {
        &self.event_type
    }

    /// Returns the required top-level payload fields.
    pub fn fields(&self) -> &Map<String, Value> {
        &self.fields
    }

    /// Returns the optional deadline in Unix milliseconds.
    pub fn deadline_ms(&self) -> Option<i64> {
        self.deadline_ms
    }
}

/// Summarizes the latest attempt associated with a session.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct SessionLatestAttempt {
    pub(crate) run_id: Option<RunId>,
    pub(crate) status: AttemptStatus,
}

impl SessionLatestAttempt {
    /// Returns the invocation id when the attempt declared one.
    pub fn run_id(&self) -> Option<&RunId> {
        self.run_id.as_ref()
    }

    /// Returns the attempt's latest durable status.
    pub fn status(&self) -> AttemptStatus {
        self.status
    }
}

/// Describes current activity derived from durable session-scoped records.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct Session {
    pub(crate) session_id: SessionId,
    pub(crate) agent_id: Option<String>,
    pub(crate) status: SessionActivityStatus,
    pub(crate) cancellation_requested: bool,
    pub(crate) active_run_id: Option<RunId>,
    pub(crate) queued_turns: u64,
    pub(crate) active_wait: Option<SessionActiveWait>,
    #[serde(rename = "latest_run")]
    pub(crate) latest_attempt: Option<SessionLatestAttempt>,
    pub(crate) updated_at: String,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub(crate) conflicting_agent_ids: Vec<String>,
}

impl Session {
    /// Returns the durable timeline identity.
    pub fn session_id(&self) -> &SessionId {
        &self.session_id
    }

    /// Returns the single owner, or `None` for missing or conflicting owners.
    pub fn agent_id(&self) -> Option<&str> {
        self.agent_id.as_deref()
    }

    /// Returns the priority-derived current activity.
    pub fn status(&self) -> SessionActivityStatus {
        self.status
    }

    /// Reports whether unfinished work carries durable cancellation intent.
    pub fn cancellation_requested(&self) -> bool {
        self.cancellation_requested
    }

    /// Returns the newest currently running invocation.
    pub fn active_run_id(&self) -> Option<&RunId> {
        self.active_run_id.as_ref()
    }

    /// Returns unfinished turns excluding queue items with running attempts.
    pub fn queued_turns(&self) -> u64 {
        self.queued_turns
    }

    /// Returns the newest active wait when one exists.
    pub fn active_wait(&self) -> Option<&SessionActiveWait> {
        self.active_wait.as_ref()
    }

    /// Returns the newest attempt regardless of terminal state.
    pub fn latest_attempt(&self) -> Option<&SessionLatestAttempt> {
        self.latest_attempt.as_ref()
    }

    /// Returns the latest activity timestamp normalized to UTC.
    pub fn updated_at(&self) -> &str {
        &self.updated_at
    }

    /// Returns lexically ordered owners when history is inconsistent.
    pub fn conflicting_agent_ids(&self) -> &[String] {
        &self.conflicting_agent_ids
    }
}

/// Carries one user-authored turn addressed directly to an existing session.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SessionMessageRequest {
    pub(crate) message: String,
    pub(crate) agent_id: String,
    pub(crate) session_id: SessionId,
    pub(crate) project_generation: String,
    pub(crate) run_id: RunId,
    pub(crate) idempotency_key: Option<String>,
}

impl SessionMessageRequest {
    /// Creates a direct session turn without a retry key.
    pub fn new(
        message: impl Into<String>,
        agent_id: impl Into<String>,
        session_id: SessionId,
        project_generation: impl Into<String>,
        run_id: RunId,
    ) -> Self {
        SessionMessageRequest {
            message: message.into(),
            agent_id: agent_id.into(),
            session_id,
            project_generation: project_generation.into(),
            run_id,
            idempotency_key: None,
        }
    }

    /// Adds the final durable idempotency key used across retries.
    pub fn with_idempotency_key(mut self, idempotency_key: impl Into<String>) -> Self {
        self.idempotency_key = Some(idempotency_key.into());
        self
    }

    /// Returns the user-authored message.
    pub fn message(&self) -> &str {
        &self.message
    }

    /// Returns the agent that owns the addressed session.
    pub fn agent_id(&self) -> &str {
        &self.agent_id
    }

    /// Returns the addressed durable timeline.
    pub fn session_id(&self) -> &SessionId {
        &self.session_id
    }

    /// Returns the authored generation used by the queued turn.
    pub fn project_generation(&self) -> &str {
        &self.project_generation
    }

    /// Returns the public invocation identity reserved for this turn.
    pub fn run_id(&self) -> &RunId {
        &self.run_id
    }

    /// Returns the optional final durable retry key.
    pub fn idempotency_key(&self) -> Option<&str> {
        self.idempotency_key.as_deref()
    }
}

/// Supplies identities for every possible fact in direct session submission.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SessionMessageIdentities {
    pub(crate) wait_cancelled: RuntimeEventIdentity,
    pub(crate) requested: RuntimeEventIdentity,
    pub(crate) available: RuntimeEventIdentity,
}

impl SessionMessageIdentities {
    /// Creates identities in possible journal order.
    ///
    /// `wait_cancelled` remains unused when the session has no active wait.
    pub fn new(
        wait_cancelled: RuntimeEventIdentity,
        requested: RuntimeEventIdentity,
        available: RuntimeEventIdentity,
    ) -> Self {
        SessionMessageIdentities {
            wait_cancelled,
            requested,
            available,
        }
    }
}

/// Returns the durable binding created or retained for a direct session turn.
#[derive(Clone, Debug, PartialEq)]
pub struct SubmittedSessionMessage {
    pub(crate) event_id: String,
    pub(crate) queue_item_id: QueueItemId,
    pub(crate) agent_id: String,
    pub(crate) session_id: SessionId,
    pub(crate) run_id: RunId,
    pub(crate) changed: bool,
    pub(crate) events: Vec<Event>,
}

impl SubmittedSessionMessage {
    /// Returns the retained user-message event identity.
    pub fn event_id(&self) -> &str {
        &self.event_id
    }

    /// Returns the stable queue binding for the addressed agent.
    pub fn queue_item_id(&self) -> &QueueItemId {
        &self.queue_item_id
    }

    /// Returns the owner recorded by the retained user-message fact.
    pub fn agent_id(&self) -> &str {
        &self.agent_id
    }

    /// Returns the timeline recorded by the retained user-message fact.
    pub fn session_id(&self) -> &SessionId {
        &self.session_id
    }

    /// Returns the invocation identity recorded by the retained fact.
    pub fn run_id(&self) -> &RunId {
        &self.run_id
    }

    /// Reports whether this call committed any new fact.
    pub fn changed(&self) -> bool {
        self.changed
    }

    /// Returns retained or inserted facts in transaction order.
    pub fn events(&self) -> &[Event] {
        &self.events
    }
}

/// Describes the durable lifecycle of a future publication.
#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum DeferredPublicationStatus {
    /// The publication time has not been consumed.
    Pending,
    /// A transaction is currently publishing the event.
    Claimed,
    /// The requested event and terminal fact committed.
    Published,
    /// An authorized request prevented publication.
    Cancelled,
}

/// Describes one durable future publication read model.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct DeferredPublication {
    pub(crate) handle: String,
    pub(crate) event_type: String,
    pub(crate) payload: Map<String, Value>,
    pub(crate) publish_at_ms: i64,
    pub(crate) source_agent_id: String,
    pub(crate) source_session_id: Option<SessionId>,
    pub(crate) source_run_id: Option<RunId>,
    pub(crate) source_queue_item_id: QueueItemId,
    pub(crate) position: u64,
    pub(crate) created_event_id: String,
    pub(crate) status: DeferredPublicationStatus,
    pub(crate) published_event_id: Option<String>,
    pub(crate) terminal_event_id: Option<String>,
    pub(crate) updated_at: i64,
}

impl DeferredPublication {
    /// Returns the retry-stable publication handle.
    pub fn handle(&self) -> &str {
        &self.handle
    }

    /// Returns the future event type.
    pub fn event_type(&self) -> &str {
        &self.event_type
    }

    /// Returns the future event payload.
    pub fn payload(&self) -> &Map<String, Value> {
        &self.payload
    }

    /// Returns the due time in Unix milliseconds.
    pub fn publish_at_ms(&self) -> i64 {
        self.publish_at_ms
    }

    /// Returns the agent that requested publication.
    pub fn source_agent_id(&self) -> &str {
        &self.source_agent_id
    }

    /// Returns the originating session when one exists.
    pub fn source_session_id(&self) -> Option<&SessionId> {
        self.source_session_id.as_ref()
    }

    /// Returns the originating run when one exists.
    pub fn source_run_id(&self) -> Option<&RunId> {
        self.source_run_id.as_ref()
    }

    /// Returns the queue item that requested publication.
    pub fn source_queue_item_id(&self) -> &QueueItemId {
        &self.source_queue_item_id
    }

    /// Returns the request's global control position.
    pub fn position(&self) -> u64 {
        self.position
    }

    /// Returns the lifecycle fact that created the publication.
    pub fn created_event_id(&self) -> &str {
        &self.created_event_id
    }

    /// Returns the current event-sourced state.
    pub fn status(&self) -> DeferredPublicationStatus {
        self.status
    }

    /// Returns the published event identity after success.
    pub fn published_event_id(&self) -> Option<&str> {
        self.published_event_id.as_deref()
    }

    /// Returns the publication or cancellation fact.
    pub fn terminal_event_id(&self) -> Option<&str> {
        self.terminal_event_id.as_deref()
    }

    /// Returns the latest lifecycle time in Unix milliseconds.
    pub fn updated_at(&self) -> i64 {
        self.updated_at
    }
}

/// Identifies the durable resource addressed by a cancellation handle.
#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ResourceKind {
    /// An event wait owned by one agent session.
    Wait,
    /// A pending one-shot publication.
    DeferredPublication,
}

/// Describes the terminal winner observed by resource cancellation.
#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ResourceCancellationStatus {
    /// Cancellation won while the resource was active.
    Cancelled,
    /// An external event already consumed the wait.
    Matched,
    /// The deadline already consumed the wait.
    TimedOut,
    /// The deferred publication already committed.
    Published,
}

/// Returns the terminal state observed by one resource cancellation request.
#[derive(Clone, Debug, PartialEq)]
pub struct ResourceCancellationOutcome {
    pub(crate) handle: String,
    pub(crate) resource_kind: ResourceKind,
    pub(crate) status: ResourceCancellationStatus,
    pub(crate) changed: bool,
    pub(crate) event: Option<Event>,
}

impl ResourceCancellationOutcome {
    /// Returns the retry-stable resource handle.
    pub fn handle(&self) -> &str {
        &self.handle
    }

    /// Returns whether the handle names a wait or one-shot publication.
    pub fn resource_kind(&self) -> ResourceKind {
        self.resource_kind
    }

    /// Returns the terminal winner after this request.
    pub fn status(&self) -> ResourceCancellationStatus {
        self.status
    }

    /// Reports whether this call appended the cancellation fact.
    pub fn changed(&self) -> bool {
        self.changed
    }

    /// Returns the newly appended cancellation fact when this call won.
    pub fn event(&self) -> Option<&Event> {
        self.event.as_ref()
    }
}

/// Names the retry guarantee declared for an external effect.
#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum EffectDeliverySemantics {
    /// A stable provider key deduplicates repeated delivery.
    IdempotentWithKey,
    /// The connector owns deduplication across retries.
    ConnectorDeduplicated,
    /// Repeated delivery is explicitly permitted.
    AtLeastOnce,
    /// An interrupted delivery requires manual resolution.
    UnsafeToRetry,
}

/// Describes the journaled lifecycle of one external effect.
#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum EffectStatus {
    /// The operation is durable but has not crossed its delivery barrier.
    Planned,
    /// External delivery may have begun.
    Started,
    /// External delivery succeeded.
    Completed,
    /// A retry-safe external delivery failed.
    Failed,
    /// An unsafe delivery may have happened and must not be retried.
    Ambiguous,
}

/// Describes one durable external-effect read model.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct Effect {
    pub(crate) key: String,
    pub(crate) operation: String,
    pub(crate) semantics: EffectDeliverySemantics,
    pub(crate) scope: String,
    pub(crate) queue_item_id: Option<QueueItemId>,
    pub(crate) params: Map<String, Value>,
    pub(crate) status: EffectStatus,
    pub(crate) result: Option<Map<String, Value>>,
    pub(crate) planned_event_id: String,
    pub(crate) terminal_event_id: Option<String>,
    pub(crate) updated_at: i64,
}

impl Effect {
    /// Returns the attempt-independent logical operation key.
    pub fn key(&self) -> &str {
        &self.key
    }

    /// Returns the provider-qualified operation name.
    pub fn operation(&self) -> &str {
        &self.operation
    }

    /// Returns the declared retry guarantee.
    pub fn semantics(&self) -> EffectDeliverySemantics {
        self.semantics
    }

    /// Returns the caller-defined identity scope.
    pub fn scope(&self) -> &str {
        &self.scope
    }

    /// Returns the owning queue item when the scope identifies one.
    pub fn queue_item_id(&self) -> Option<&QueueItemId> {
        self.queue_item_id.as_ref()
    }

    /// Returns the canonical operation parameters.
    pub fn params(&self) -> &Map<String, Value> {
        &self.params
    }

    /// Returns the latest durable lifecycle state.
    pub fn status(&self) -> EffectStatus {
        self.status
    }

    /// Returns the provider result after a terminal observation.
    pub fn result(&self) -> Option<&Map<String, Value>> {
        self.result.as_ref()
    }

    /// Returns the first durable planning fact.
    pub fn planned_event_id(&self) -> &str {
        &self.planned_event_id
    }

    /// Returns the completed, failed, or ambiguous fact when terminal.
    pub fn terminal_event_id(&self) -> Option<&str> {
        self.terminal_event_id.as_deref()
    }

    /// Returns the latest lifecycle time in Unix milliseconds.
    pub fn updated_at(&self) -> i64 {
        self.updated_at
    }
}

/// Describes a failed execution before Dispatch chooses retry or dead letter.
#[derive(Clone, Debug, PartialEq)]
pub struct AttemptFailure {
    pub(crate) finished_at: String,
    pub(crate) error: String,
    pub(crate) error_code: AttemptFailureCode,
    pub(crate) retry_policy: RetryPolicy,
}

/// Names the terminal state proposed for one claimed attempt.
///
/// # Examples
///
/// ```
/// let disposition = zeta_dispatch::AttemptCompletionDisposition::Succeeded;
/// assert_eq!(
///     disposition,
///     zeta_dispatch::AttemptCompletionDisposition::Succeeded,
/// );
/// ```
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum AttemptCompletionDisposition {
    /// Completes the attempt and its queue item successfully.
    Succeeded,
    /// Cancels the attempt and its queue item without applying controls.
    Cancelled,
}

/// Carries one typed control proposal in global tool-call order.
///
/// # Examples
///
/// ```
/// let control = zeta_dispatch::AttemptControl::publish(
///     "pub-example",
///     "example.created",
///     serde_json::Map::new(),
///     None,
///     2,
/// );
/// assert_eq!(control.position(), 2);
/// ```
#[derive(Clone, Debug, PartialEq)]
pub enum AttemptControl {
    /// Proposes immediate or deferred publication of a durable event.
    Publish {
        /// Identifies the retry-stable proposal.
        handle: String,
        /// Names the event vocabulary entry.
        event_type: String,
        /// Carries the event payload.
        payload: Map<String, Value>,
        /// Schedules publication at an optional RFC 3339 timestamp.
        at: Option<String>,
        /// Preserves global tool-call order.
        position: u64,
    },
    /// Proposes waiting for a matching durable event.
    Wait {
        /// Identifies the retry-stable wait.
        handle: String,
        /// Names the event vocabulary entry to match.
        event_type: String,
        /// Narrows the match to exact payload fields.
        fields: Map<String, Value>,
        /// Stops waiting at an optional RFC 3339 timestamp.
        deadline: Option<String>,
        /// Preserves global tool-call order.
        position: u64,
    },
    /// Proposes cancelling an existing wait or deferred publication.
    Cancel {
        /// Identifies the resource to cancel.
        handle: String,
        /// Explains the cancellation when supplied.
        reason: Option<String>,
        /// Associates the request with an agent.
        source_agent_id: String,
        /// Associates the request with a session.
        source_session_id: String,
        /// Preserves global tool-call order.
        position: u64,
    },
}

impl AttemptControl {
    /// Creates a typed event-publication proposal.
    pub fn publish(
        handle: impl Into<String>,
        event_type: impl Into<String>,
        payload: Map<String, Value>,
        at: Option<String>,
        position: u64,
    ) -> Self {
        AttemptControl::Publish {
            handle: handle.into(),
            event_type: event_type.into(),
            payload,
            at,
            position,
        }
    }

    /// Creates a typed event-wait proposal.
    pub fn wait(
        handle: impl Into<String>,
        event_type: impl Into<String>,
        fields: Map<String, Value>,
        deadline: Option<String>,
        position: u64,
    ) -> Self {
        AttemptControl::Wait {
            handle: handle.into(),
            event_type: event_type.into(),
            fields,
            deadline,
            position,
        }
    }

    /// Creates a typed resource-cancellation proposal.
    pub fn cancel(
        handle: impl Into<String>,
        reason: Option<String>,
        source_agent_id: impl Into<String>,
        source_session_id: impl Into<String>,
        position: u64,
    ) -> Self {
        AttemptControl::Cancel {
            handle: handle.into(),
            reason,
            source_agent_id: source_agent_id.into(),
            source_session_id: source_session_id.into(),
            position,
        }
    }

    /// Returns the global tool-call position.
    pub fn position(&self) -> u64 {
        match self {
            AttemptControl::Publish { position, .. } => *position,
            AttemptControl::Wait { position, .. } => *position,
            AttemptControl::Cancel { position, .. } => *position,
        }
    }
}

/// Carries one typed attempt terminal proposal for an atomic commit.
///
/// # Examples
///
/// ```
/// let completion = zeta_dispatch::AttemptCompletion::new(
///     "2026-08-12T10:00:01Z",
///     zeta_dispatch::AttemptCompletionDisposition::Succeeded,
///     serde_json::Map::new(),
///     Vec::new(),
/// );
/// assert!(completion.controls().is_empty());
/// ```
#[derive(Clone, Debug, PartialEq)]
pub struct AttemptCompletion {
    pub(crate) finished_at: String,
    pub(crate) disposition: AttemptCompletionDisposition,
    pub(crate) metadata: Map<String, Value>,
    pub(crate) controls: Vec<AttemptControl>,
}

impl AttemptCompletion {
    /// Creates a completion proposal without mutating durable state.
    pub fn new(
        finished_at: impl Into<String>,
        disposition: AttemptCompletionDisposition,
        metadata: Map<String, Value>,
        controls: Vec<AttemptControl>,
    ) -> Self {
        AttemptCompletion {
            finished_at: finished_at.into(),
            disposition,
            metadata,
            controls,
        }
    }

    /// Returns the producer-supplied terminal timestamp.
    pub fn finished_at(&self) -> &str {
        &self.finished_at
    }

    /// Returns the proposed terminal disposition.
    pub fn disposition(&self) -> AttemptCompletionDisposition {
        self.disposition
    }

    /// Returns the open durable result metadata.
    pub fn metadata(&self) -> &Map<String, Value> {
        &self.metadata
    }

    /// Returns typed control proposals in caller order.
    pub fn controls(&self) -> &[AttemptControl] {
        &self.controls
    }
}

/// Supplies event identities for cancellation intent and terminal closure.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CancellationIdentities {
    pub(crate) requested: RuntimeEventIdentity,
    pub(crate) cancelled: RuntimeEventIdentity,
}

/// Supplies terminal identities when recovery closes requested cancellation.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CancellationFinalizationIdentities {
    pub(crate) attempt_cancelled: RuntimeEventIdentity,
    pub(crate) queue_cancelled: RuntimeEventIdentity,
}

impl CancellationFinalizationIdentities {
    /// Creates attempt-then-queue identities in journal commit order.
    pub fn new(
        attempt_cancelled: RuntimeEventIdentity,
        queue_cancelled: RuntimeEventIdentity,
    ) -> Self {
        CancellationFinalizationIdentities {
            attempt_cancelled,
            queue_cancelled,
        }
    }
}

impl CancellationIdentities {
    /// Creates the two identities in journal commit order.
    pub fn new(requested: RuntimeEventIdentity, cancelled: RuntimeEventIdentity) -> Self {
        CancellationIdentities {
            requested,
            cancelled,
        }
    }
}

/// Describes the durable disposition of one cancellation request.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CancellationStatus {
    /// No queue item had the requested identity.
    Unknown,
    /// Intent is durable while a claimed attempt cooperatively stops.
    Cancelling,
    /// Queued work became terminal in the same transaction as intent.
    Cancelled,
    /// The item was already cancelled by the retained request.
    AlreadyCancelled,
    /// Another terminal outcome won before cancellation.
    AlreadyTerminal,
}

/// Returns cancellation status and any facts retained by this operation.
#[derive(Clone, Debug, PartialEq)]
pub struct CancellationOutcome {
    pub(crate) queue_item_id: Option<QueueItemId>,
    pub(crate) status: CancellationStatus,
    pub(crate) changed: bool,
    pub(crate) events: Vec<Event>,
}

impl CancellationOutcome {
    /// Returns the resolved queue item, or `None` for an unknown identity.
    pub fn queue_item_id(&self) -> Option<&QueueItemId> {
        self.queue_item_id.as_ref()
    }

    /// Returns the operation's stable disposition.
    pub fn status(&self) -> CancellationStatus {
        self.status
    }

    /// Reports whether this call committed any new fact.
    pub fn changed(&self) -> bool {
        self.changed
    }

    /// Returns newly inserted or retained cancellation facts in journal order.
    pub fn events(&self) -> &[Event] {
        &self.events
    }
}

impl AttemptFailure {
    /// Creates one structured attempt failure.
    pub fn new(
        finished_at: impl Into<String>,
        error: impl Into<String>,
        error_code: AttemptFailureCode,
        retry_policy: RetryPolicy,
    ) -> Self {
        AttemptFailure {
            finished_at: finished_at.into(),
            error: error.into(),
            error_code,
            retry_policy,
        }
    }

    /// Returns the producer-supplied terminal timestamp.
    pub fn finished_at(&self) -> &str {
        &self.finished_at
    }

    /// Returns the stable diagnostic text.
    pub fn error(&self) -> &str {
        &self.error
    }

    /// Returns the structured failure code used for retry classification.
    pub fn error_code(&self) -> AttemptFailureCode {
        self.error_code
    }

    /// Returns the policy applied to this attempt number.
    pub fn retry_policy(&self) -> RetryPolicy {
        self.retry_policy
    }
}

impl StartedAttempt {
    /// Returns the running attempt read model.
    pub fn attempt(&self) -> &Attempt {
        &self.attempt
    }

    /// Returns the queue-claimed and attempt-started facts in journal order.
    pub fn events(&self) -> &[Event] {
        &self.events
    }
}

impl RoutingOutcome {
    /// Returns decisions in normalized route declaration order.
    pub fn decisions(&self) -> &[RouteDecision] {
        &self.decisions
    }

    /// Returns retained lifecycle events in journal order.
    pub fn events(&self) -> &[Event] {
        &self.events
    }
}
