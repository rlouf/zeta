//! Deterministic identities below a durable event.

use std::fmt;
use std::str::FromStr;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use zeta_substrate::{canonical_json, derive, CanonicalJsonError, Domain};

use crate::state::{AttemptStatus, QueueItemStatus};

/// Identifies one durable event-to-agent binding.
///
/// # Examples
///
/// ```
/// let id = zeta_dispatch::queue_item_id("evt_1", "reviewer");
/// assert_eq!(id.as_str(), "qi_evt_1_reviewer");
/// ```
#[derive(Clone, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(transparent)]
pub struct QueueItemId(String);

impl QueueItemId {
    /// Returns the opaque identifier text.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl FromStr for QueueItemId {
    type Err = RuntimeIdParseError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        parse_runtime_id("queue item", text).map(QueueItemId)
    }
}

impl fmt::Display for QueueItemId {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.0.fmt(formatter)
    }
}

/// Identifies one numbered execution try.
#[derive(Clone, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(transparent)]
pub struct AttemptId(String);

impl AttemptId {
    /// Returns the opaque identifier text.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl FromStr for AttemptId {
    type Err = RuntimeIdParseError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        parse_runtime_id("attempt", text).map(AttemptId)
    }
}

impl fmt::Display for AttemptId {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.0.fmt(formatter)
    }
}

/// Identifies one agent invocation.
#[derive(Clone, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(transparent)]
pub struct RunId(String);

impl RunId {
    /// Returns the opaque identifier text.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl FromStr for RunId {
    type Err = RuntimeIdParseError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        parse_runtime_id("run", text).map(RunId)
    }
}

impl fmt::Display for RunId {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.0.fmt(formatter)
    }
}

/// Identifies one durable authored-agent timeline.
#[derive(Clone, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(transparent)]
pub struct SessionId(String);

impl SessionId {
    /// Returns the opaque identifier text.
    pub fn as_str(&self) -> &str {
        &self.0
    }

    pub(crate) fn for_agent(agent_id: &str, suffix: Option<&str>) -> Self {
        let value = match suffix {
            Some(suffix) => format!("agent/{agent_id}/{suffix}"),
            None => format!("agent/{agent_id}"),
        };
        SessionId(value)
    }
}

impl FromStr for SessionId {
    type Err = RuntimeIdParseError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        parse_runtime_id("session", text).map(SessionId)
    }
}

impl fmt::Display for SessionId {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.0.fmt(formatter)
    }
}

/// Identifies one ordered event publication request across retries.
#[derive(Clone, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(transparent)]
pub struct PublishHandle(String);

impl PublishHandle {
    /// Returns the opaque identifier text.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl FromStr for PublishHandle {
    type Err = RuntimeIdParseError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        parse_runtime_id("publish handle", text).map(PublishHandle)
    }
}

impl fmt::Display for PublishHandle {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.0.fmt(formatter)
    }
}

/// Identifies one ordered wait request across retries.
#[derive(Clone, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(transparent)]
pub struct WaitHandle(String);

impl WaitHandle {
    /// Returns the opaque identifier text.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl FromStr for WaitHandle {
    type Err = RuntimeIdParseError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        parse_runtime_id("wait handle", text).map(WaitHandle)
    }
}

impl fmt::Display for WaitHandle {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.0.fmt(formatter)
    }
}

/// Fences one live queue claim from every earlier or unrelated owner.
#[derive(Clone, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(transparent)]
pub struct ClaimToken(String);

impl ClaimToken {
    /// Creates an opaque caller-generated claim token.
    ///
    /// # Errors
    ///
    /// Returns [`RuntimeIdParseError`] when the token is empty.
    pub fn new(token: impl Into<String>) -> Result<Self, RuntimeIdParseError> {
        let token = token.into();
        parse_runtime_id("claim token", &token).map(ClaimToken)
    }

    /// Returns the opaque token text.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl FromStr for ClaimToken {
    type Err = RuntimeIdParseError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        parse_runtime_id("claim token", text).map(ClaimToken)
    }
}

impl fmt::Display for ClaimToken {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.0.fmt(formatter)
    }
}

/// Replaces separators that would make an agent id ambiguous in a compound id.
pub fn safe_agent_id(agent_id: &str) -> String {
    agent_id.replace([':', '.'], "_")
}

/// Derives the unbound queue item created with an ingress event.
pub fn pending_queue_item_id(event_id: &str) -> QueueItemId {
    QueueItemId(format!("qi_{event_id}"))
}

/// Derives the binding between an event and one target agent.
///
/// Agent separators are normalized so the compound identity remains
/// unambiguous.
///
/// # Examples
///
/// ```
/// let id = zeta_dispatch::queue_item_id("evt_1", "team.review");
/// assert_eq!(id.as_str(), "qi_evt_1_team_review");
/// ```
pub fn queue_item_id(event_id: &str, agent_id: &str) -> QueueItemId {
    QueueItemId(format!("qi_{event_id}_{}", safe_agent_id(agent_id)))
}

/// Derives the terminal queue item recorded when no route accepts an event.
pub fn unhandled_queue_item_id(event_id: &str) -> QueueItemId {
    QueueItemId(format!("qi_{event_id}_unhandled"))
}

/// Derives one numbered execution try from its queue item.
pub fn attempt_id(queue_item_id: &QueueItemId, attempt_number: u32) -> AttemptId {
    AttemptId(format!("att_{queue_item_id}_{attempt_number}"))
}

/// Derives an invocation id when no caller claimed one in advance.
pub fn derived_run_id(attempt_id: &AttemptId) -> RunId {
    RunId(format!("run_{attempt_id}"))
}

/// Selects a non-empty caller-claimed run id or derives one from the attempt.
pub fn run_id_for_attempt(claimed: Option<&str>, attempt_id: &AttemptId) -> RunId {
    if let Some(claimed) = claimed {
        if !claimed.is_empty() {
            return RunId(claimed.to_owned());
        }
    }
    derived_run_id(attempt_id)
}

/// Derives a retry-stable handle for an ordered publication request.
pub fn publish_event_handle(queue_item_id: &QueueItemId, position: u64) -> PublishHandle {
    let identity = format!("{queue_item_id}:{position}");
    PublishHandle(format!(
        "pub_{}",
        derive(Domain::Chain, identity.as_bytes())
    ))
}

/// Derives a retry-stable handle for an ordered wait request.
pub fn wait_handle(queue_item_id: &QueueItemId, position: u64) -> WaitHandle {
    let identity = format!("{queue_item_id}:{position}");
    WaitHandle(format!(
        "wait_{}",
        derive(Domain::Chain, identity.as_bytes())
    ))
}

/// Derives an attempt-independent identity for one logical external effect.
///
/// Canonical JSON makes parameter-object insertion order irrelevant while the
/// scope keeps distinct queue items or calls separate.
///
/// # Errors
///
/// Returns [`CanonicalJsonError`] when a parameter value cannot be represented
/// by the shared canonical JSON format.
pub fn effect_key(
    scope: &str,
    operation: &str,
    params: &Map<String, Value>,
) -> Result<String, CanonicalJsonError> {
    let mut identity = Map::new();
    identity.insert("scope".to_owned(), Value::String(scope.to_owned()));
    identity.insert("operation".to_owned(), Value::String(operation.to_owned()));
    identity.insert("params".to_owned(), Value::Object(params.clone()));
    let encoded = canonical_json(&Value::Object(identity))?;
    Ok(format!("effect:{}", derive(Domain::Chain, &encoded)))
}

/// Derives the idempotency key for a queue lifecycle fact.
pub fn queue_item_idempotency_key(
    event_id: &str,
    target_agent: &str,
    status: QueueItemStatus,
) -> String {
    format!("queue_item:{event_id}:{target_agent}:{status}")
}

/// Derives the attempt-qualified idempotency key for a queue lifecycle fact.
pub fn queue_item_attempt_idempotency_key(
    event_id: &str,
    target_agent: &str,
    status: QueueItemStatus,
    attempt_number: u32,
) -> String {
    format!(
        "{}:{attempt_number}",
        queue_item_idempotency_key(event_id, target_agent, status)
    )
}

/// Derives the idempotency key for an unhandled ingress event.
pub fn unhandled_queue_item_idempotency_key(event_id: &str) -> String {
    format!("queue_item:{event_id}:unhandled")
}

/// Derives the idempotency key for an attempt lifecycle fact.
pub fn attempt_idempotency_key(
    queue_item_id: &QueueItemId,
    attempt_number: u32,
    status: AttemptStatus,
) -> String {
    format!("attempt:{queue_item_id}:{attempt_number}:{status}")
}

/// Reports an empty stored or externally supplied runtime identity.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RuntimeIdParseError {
    resource: &'static str,
}

impl RuntimeIdParseError {
    pub(crate) fn new(resource: &'static str) -> Self {
        RuntimeIdParseError { resource }
    }

    /// Returns the identity kind that rejected an empty value.
    pub fn resource(&self) -> &'static str {
        self.resource
    }
}

impl fmt::Display for RuntimeIdParseError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{} identity must not be empty", self.resource)
    }
}

impl std::error::Error for RuntimeIdParseError {}

fn parse_runtime_id(resource: &'static str, text: &str) -> Result<String, RuntimeIdParseError> {
    if text.is_empty() {
        return Err(RuntimeIdParseError { resource });
    }
    Ok(text.to_owned())
}
