//! Capability declarations, validated calls, and execution boundaries.

use std::fmt;
use std::future::Future;
use std::pin::Pin;

use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use zeta_journal::DraftEvent;

use crate::error::AgentError;
use crate::model::AbortSignal;

/// Resolves one capability execution at an injected runtime boundary.
pub type CapabilityFuture<'a> =
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

/// Selects one exact model-facing presentation of canonical capabilities.
#[derive(Clone, Copy, Debug, Default, Deserialize, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ToolProfile {
    /// Preserves each canonical declaration's native presentation.
    #[default]
    Native,
    /// Presents built-in tools using the names expected by Codex models.
    Codex,
}

impl ToolProfile {
    /// Resolves canonical declarations using model names derived from their ids.
    ///
    /// # Examples
    ///
    /// ```
    /// let capabilities = zeta_agent::native_capabilities();
    /// let resolved = zeta_agent::ToolProfile::Native.resolve(&capabilities);
    /// assert_eq!(resolved[0].model_name, capabilities[0].id.model_name());
    /// ```
    pub fn resolve(self, capabilities: &[Capability]) -> Vec<ResolvedCapability> {
        let mut resolved = Vec::with_capacity(capabilities.len());
        for capability in capabilities {
            let route = self.resolve_capability(capability, capability.id.model_name());
            resolved.push(route);
        }
        resolved
    }

    /// Resolves one declaration with an explicit authored model-facing name.
    ///
    /// The authored name is retained unless this profile defines a built-in
    /// adapter for the declaration's canonical id.
    ///
    /// # Examples
    ///
    /// ```
    /// let capabilities = zeta_agent::native_capabilities();
    /// let resolved = zeta_agent::ToolProfile::Native.resolve_capability(
    ///     &capabilities[0],
    ///     "search_code",
    /// );
    /// assert_eq!(resolved.model_name, "search_code");
    /// ```
    pub fn resolve_capability(
        self,
        capability: &Capability,
        model_name: &str,
    ) -> ResolvedCapability {
        if self == ToolProfile::Codex && capability.id.as_str() == "zeta.bash" {
            return ResolvedCapability {
                canonical: capability.clone(),
                model_name: "exec_command".to_owned(),
                model_description: "Run a shell command.".to_owned(),
                model_input_schema: object_schema(json!({
                    "type": "object",
                    "additionalProperties": false,
                    "required": ["cmd"],
                    "properties": {"cmd": {"type": "string"}},
                })),
                argument_adapter: ArgumentAdapter::RenameField {
                    from: "cmd".to_owned(),
                    to: "command".to_owned(),
                },
            };
        }
        if self == ToolProfile::Codex && capability.id.as_str() == "zeta.patch" {
            return ResolvedCapability {
                canonical: capability.clone(),
                model_name: "apply_patch".to_owned(),
                model_description: "Apply a patch to files.".to_owned(),
                model_input_schema: object_schema(json!({
                    "type": "object",
                    "additionalProperties": false,
                    "required": ["patch"],
                    "properties": {"patch": {"type": "string", "minLength": 1}},
                })),
                argument_adapter: ArgumentAdapter::Identity,
            };
        }
        ResolvedCapability {
            canonical: capability.clone(),
            model_name: model_name.to_owned(),
            model_description: capability.description.clone(),
            model_input_schema: capability.input_schema.clone(),
            argument_adapter: ArgumentAdapter::Identity,
        }
    }
}

/// Adapts validated model arguments into canonical capability arguments.
#[derive(Clone, Debug, Default, Deserialize, PartialEq, Eq, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ArgumentAdapter {
    /// Preserves the model arguments exactly.
    #[default]
    Identity,
    /// Moves one model-facing field to its canonical name.
    RenameField {
        /// Names the model-facing field.
        from: String,
        /// Names the canonical field.
        to: String,
    },
}

impl ArgumentAdapter {
    /// Returns canonical arguments after applying this explicit adaptation.
    ///
    /// # Errors
    ///
    /// Returns [`AgentError`] when a rename source is absent or its destination
    /// would overwrite another value.
    pub fn adapt(&self, arguments: &Map<String, Value>) -> Result<Map<String, Value>, AgentError> {
        match self {
            ArgumentAdapter::Identity => Ok(arguments.clone()),
            ArgumentAdapter::RenameField { from, to } => {
                let mut adapted = arguments.clone();
                let Some(value) = adapted.remove(from) else {
                    return Err(AgentError::invocation(format!(
                        "argument adapter expected field '{from}'"
                    )));
                };
                if adapted.insert(to.clone(), value).is_some() {
                    return Err(AgentError::invocation(format!(
                        "argument adapter would overwrite field '{to}'"
                    )));
                }
                Ok(adapted)
            }
        }
    }
}

/// Carries a canonical declaration and its explicit model presentation.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct ResolvedCapability {
    /// Preserves the identity, canonical schema, and retry contract.
    pub canonical: Capability,
    /// Names the function exposed to the model.
    pub model_name: String,
    /// Explains the model-facing operation.
    pub model_description: String,
    /// Validates arguments before adaptation.
    pub model_input_schema: Map<String, Value>,
    /// Converts validated model arguments to the canonical schema.
    pub argument_adapter: ArgumentAdapter,
}

/// Resolves canonical declarations using model names derived from their ids.
///
/// # Examples
///
/// ```
/// let capabilities = zeta_agent::native_capabilities();
/// let resolved = zeta_agent::resolve_capabilities(
///     &capabilities,
///     zeta_agent::ToolProfile::Native,
/// );
/// assert_eq!(resolved[0].model_name, capabilities[0].id.model_name());
/// ```
pub fn resolve_capabilities(
    capabilities: &[Capability],
    profile: ToolProfile,
) -> Vec<ResolvedCapability> {
    profile.resolve(capabilities)
}

fn object_schema(value: Value) -> Map<String, Value> {
    value.as_object().cloned().unwrap_or_default()
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

/// Executes validated capabilities without choosing a host or plugin system.
pub trait CapabilityExecutor {
    /// Executes one canonical invocation.
    fn execute<'a>(
        &'a mut self,
        invocation: &'a CapabilityInvocation,
        abort: &'a dyn AbortSignal,
    ) -> CapabilityFuture<'a>;
}

/// Persists complete journal drafts at the moment they become true.
pub trait DraftRecorder {
    /// Records one draft before execution crosses its durability boundary.
    fn record(&mut self, draft: &DraftEvent) -> Result<(), AgentError>;
}

/// Supplies deterministic identities without reading process-global randomness.
pub trait IdSource {
    /// Returns one non-empty event identity.
    fn next_id(&mut self) -> Result<String, AgentError>;
}
