//! Capability declarations, validated calls, and execution boundaries.

use std::fmt;
use std::future::Future;
use std::pin::Pin;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::error::AgentError;

/// Resolves one capability execution at an injected runtime boundary.
pub type ToolFuture<'a> =
    Pin<Box<dyn Future<Output = Result<Map<String, Value>, AgentError>> + 'a>>;

/// Identifies one canonical capability independently of its model-facing name.
///
/// # Examples
///
/// ```
/// let id: zeta_agent::CapabilityId = "test.lookup".parse().unwrap();
/// assert_eq!(id.as_str(), "test.lookup");
/// assert_eq!(id.model_name(), "lookup");
/// ```
#[derive(Clone, Debug, Deserialize, Hash, PartialEq, Eq, Serialize)]
#[serde(transparent)]
pub struct CapabilityId(String);

impl CapabilityId {
    /// Returns the complete canonical capability id.
    pub fn as_str(&self) -> &str {
        &self.0
    }

    /// Returns the model-facing name after the provider prefix.
    pub fn model_name(&self) -> &str {
        match self.0.split_once('.') {
            Some((_provider, name)) => name,
            None => self.0.as_str(),
        }
    }
}

impl fmt::Display for CapabilityId {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.0.fmt(formatter)
    }
}

impl std::str::FromStr for CapabilityId {
    type Err = AgentError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        let Some((provider, name)) = value.split_once('.') else {
            return Err(AgentError::invocation(
                "capability id must contain a provider and name",
            ));
        };
        if provider.is_empty() || name.is_empty() {
            return Err(AgentError::invocation(
                "capability id must contain a provider and name",
            ));
        }
        Ok(CapabilityId(value.to_owned()))
    }
}

/// Names how a side effect may be retried after an interrupted call.
#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum DeliverySemantics {
    /// Requires a retry-stable key at the capability boundary.
    IdempotentWithKey,
    /// Delegates deduplication to the external connector.
    ConnectorDeduplicated,
    /// Allows the same stable effect identity to be executed again.
    AtLeastOnce,
    /// Treats an interrupted effect as ambiguous instead of retrying it.
    UnsafeToRetry,
}

/// Declares one capability available to an invocation.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct Capability {
    /// Carries the canonical provider-qualified identity.
    pub id: CapabilityId,
    /// Explains the operation to a model.
    pub description: String,
    /// Validates canonical JSON arguments.
    pub input_schema: Map<String, Value>,
    /// States the retry contract for a side effect.
    #[serde(default)]
    pub delivery_semantics: Option<DeliverySemantics>,
}

/// Carries one validated call to an injected capability executor.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct CapabilityInvocation {
    /// Identifies the canonical capability implementation.
    pub capability_id: CapabilityId,
    /// Carries validated canonical JSON arguments.
    pub params: Map<String, Value>,
    /// Scopes local execution when the invocation supplied a directory.
    pub base_directory: Option<String>,
    /// Carries a retry-stable side-effect identity when needed.
    pub effect_key: Option<String>,
}

/// Names one durable stage in a side effect's execution.
#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum EffectStatus {
    /// Records intent before execution begins.
    Planned,
    /// Records that execution has begun.
    Started,
    /// Records a successful result.
    Completed,
    /// Records a retry-safe failure.
    Failed,
    /// Records an outcome that cannot safely be retried.
    Ambiguous,
}

/// Carries one durable side-effect lifecycle observation.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct EffectEvent {
    /// Names the lifecycle stage.
    pub status: EffectStatus,
    /// Identifies the stable logical effect.
    pub effect_key: String,
    /// Identifies the canonical capability.
    pub capability_id: CapabilityId,
    /// States the declared retry contract.
    pub semantics: DeliverySemantics,
    /// Identifies the caller-defined logical attempt.
    pub scope: String,
    /// Identifies the model tool call that requested the effect.
    pub caused_by: String,
    /// Carries canonical arguments.
    pub params: Map<String, Value>,
    /// Carries a terminal result when one exists.
    pub result: Option<Map<String, Value>>,
}

/// Executes validated capabilities without choosing a host or plugin system.
pub trait ToolExecutor {
    /// Executes one canonical invocation.
    fn execute<'a>(&'a mut self, invocation: &'a CapabilityInvocation) -> ToolFuture<'a>;
}

/// Persists side-effect lifecycle facts at the moment they become true.
pub trait EffectRecorder {
    /// Records one lifecycle fact before execution continues.
    fn record(&mut self, event: EffectEvent) -> Result<(), AgentError>;
}

/// Supplies deterministic identities without reading process-global randomness.
pub trait IdSource {
    /// Returns one non-empty event identity.
    fn next_id(&mut self) -> Result<String, AgentError>;
}
