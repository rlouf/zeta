//! Defines authored agent declaration values.

use std::fmt;
use std::path::PathBuf;
use std::str::FromStr;

use serde::de::Error as _;
use serde::{Deserialize, Deserializer, Serialize};
use serde_json::{Map, Value};
use zeta_substrate::Hash;

pub use crate::connector::{parse_connector, ConnectorOperation, ConnectorSpec};
use crate::error::{ManifestError, ManifestErrorKind};
pub use crate::event::{derive_returns_schema, scheduled_event_type, EventRegistry};
pub use crate::manifest::{
    execution_manifest, project_manifest, restore_execution_manifest, restore_project_manifest,
    verify_execution_manifest, verify_project_manifest, ExecutionManifest, ExecutionManifestId,
    ProjectManifest, ProjectRevisionId, EXECUTION_MANIFEST_SCHEMA, EXECUTION_MANIFEST_VERSION,
    PROJECT_MANIFEST_SCHEMA, PROJECT_MANIFEST_VERSION,
};
pub use crate::project::{
    compile_project, validate_agent, AgentProject, AgentProjectInput, AgentValidationContext,
};

/// Identifies the exact implementation behind one host-supplied declaration.
///
/// The host decides which bytes define an implementation and supplies their
/// plain content address. Authoring records the value without inspecting a
/// process, package, or filesystem path.
///
/// # Examples
///
/// ```
/// let fingerprint = zeta_manifest::ImplementationFingerprint::new(
///     zeta_substrate::hash_bytes(b"implementation"),
/// );
/// assert_eq!(fingerprint.as_hash(), zeta_substrate::hash_bytes(b"implementation"));
/// ```
#[derive(Clone, Debug, Deserialize, PartialEq, Eq, PartialOrd, Ord, Serialize)]
#[serde(transparent)]
pub struct ImplementationFingerprint(Hash);

impl ImplementationFingerprint {
    /// Creates a fingerprint from a plain content address.
    ///
    /// # Examples
    ///
    /// ```
    /// let hash = zeta_substrate::hash_bytes(b"implementation");
    /// let fingerprint = zeta_manifest::ImplementationFingerprint::new(hash);
    /// assert_eq!(fingerprint.as_hash(), hash);
    /// ```
    pub fn new(hash: Hash) -> Self {
        ImplementationFingerprint(hash)
    }

    /// Returns the plain implementation content address.
    ///
    /// # Examples
    ///
    /// ```
    /// let hash = zeta_substrate::hash_bytes(b"implementation");
    /// let fingerprint = zeta_manifest::ImplementationFingerprint::new(hash);
    /// assert_eq!(fingerprint.as_hash(), hash);
    /// ```
    pub fn as_hash(&self) -> Hash {
        self.0
    }
}

/// Names how an effect may be retried after an interrupted call.
///
/// # Examples
///
/// ```
/// let semantics = zeta_manifest::DeliverySemantics::IdempotentWithKey;
/// assert_eq!(
///     serde_json::to_value(semantics).unwrap(),
///     serde_json::json!("idempotent_with_key")
/// );
/// ```
#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum DeliverySemantics {
    /// Requires a retry-stable key at the capability boundary.
    IdempotentWithKey,
    /// Delegates deduplication to the external connector.
    ConnectorDeduplicated,
    /// Allows another attempt with the same stable effect identity.
    AtLeastOnce,
    /// Treats an interrupted operation as ambiguous.
    UnsafeToRetry,
}

/// Declares one schedule attached to an agent.
///
/// # Examples
///
/// ```
/// let schedule = zeta_manifest::ScheduleEntry {
///     cron: "0 18 * * 0".to_owned(),
///     timezone: Some("Europe/Paris".to_owned()),
///     catchup: Some("latest".to_owned()),
/// };
/// assert_eq!(schedule.timezone.as_deref(), Some("Europe/Paris"));
/// ```
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ScheduleEntry {
    /// Carries the cron expression.
    pub cron: String,
    /// Names the schedule timezone when one is authored.
    pub timezone: Option<String>,
    /// Names the requested catch-up rule.
    pub catchup: Option<String>,
}

/// Selects a named model profile for an authored agent.
///
/// # Examples
///
/// ```
/// let model = zeta_manifest::ModelSpec::Profile("fast-local".to_owned());
/// assert_eq!(model.profile(), Some("fast-local"));
/// ```
#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(untagged)]
pub enum ModelSpec {
    /// Names the profile that the host resolves before execution.
    Profile(String),
    /// Selects a concrete endpoint from the legacy frontmatter shape.
    Endpoint {
        /// Names the model served by the endpoint.
        name: String,
        /// Carries the model endpoint URL.
        url: String,
    },
}

impl ModelSpec {
    /// Returns the authored profile name when this declaration uses one.
    pub fn profile(&self) -> Option<&str> {
        match self {
            Self::Profile(profile) => Some(profile),
            Self::Endpoint { .. } => None,
        }
    }
}

/// Overrides the retry policy for one agent.
///
/// # Examples
///
/// ```
/// let retry = zeta_manifest::RetrySpec {
///     max_attempts: Some(3),
///     backoff_seconds: Some(1.5),
/// };
/// assert_eq!(retry.max_attempts, Some(3));
/// ```
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RetrySpec {
    /// Bounds the total number of attempts when present.
    pub max_attempts: Option<u64>,
    /// Carries the delay before another attempt when present.
    pub backoff_seconds: Option<f64>,
}

/// Selects a tool executor and its JSON configuration.
///
/// # Examples
///
/// ```
/// let executor = zeta_manifest::ExecutorSpec::default();
/// assert_eq!(executor.provider, "local");
/// assert!(executor.config.is_empty());
/// ```
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ExecutorSpec {
    /// Names the executor provider.
    pub provider: String,
    /// Carries provider-specific JSON configuration.
    pub config: Map<String, Value>,
}

impl Default for ExecutorSpec {
    fn default() -> Self {
        ExecutorSpec {
            provider: "local".to_owned(),
            config: Map::new(),
        }
    }
}

/// Declares how an external event enters an agent.
///
/// # Examples
///
/// ```
/// let binding = zeta_manifest::IngressBinding {
///     event: "message.received".to_owned(),
///     filter: serde_json::Map::new(),
///     idempotency_key: Some("message:{id}".to_owned()),
/// };
/// assert_eq!(binding.event, "message.received");
/// ```
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct IngressBinding {
    /// Names the accepted event.
    pub event: String,
    /// Carries connector-specific filter values.
    pub filter: Map<String, Value>,
    /// Defines the stable external idempotency identity.
    pub idempotency_key: Option<String>,
}

/// Declares how an agent publishes an external event.
///
/// # Examples
///
/// ```
/// let binding = zeta_manifest::EgressBinding {
///     event: "message.send".to_owned(),
///     options: serde_json::Map::new(),
///     idempotency_key: None,
/// };
/// assert_eq!(binding.event, "message.send");
/// ```
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct EgressBinding {
    /// Names the published event.
    pub event: String,
    /// Carries connector-specific delivery options.
    pub options: Map<String, Value>,
    /// Defines a caller-authored idempotency identity when present.
    pub idempotency_key: Option<String>,
}

/// Holds one validated authored agent declaration.
///
/// The content address identifies the exact source bytes independently of
/// where they were loaded.
///
/// # Examples
///
/// ```
/// let spec = zeta_manifest::parse_agent(
///     "worker",
///     b"---\nname: Worker\ndescription: Does work.\n---\nWork.\n",
/// )?;
/// assert_eq!(spec.slug, "worker");
/// # Ok::<(), zeta_manifest::AgentSpecError>(())
/// ```
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct AgentSpec {
    /// Carries the validated lowercase agent identifier.
    pub slug: String,
    /// Carries the authored display name.
    pub name: String,
    /// Carries the authored description.
    pub description: String,
    /// Preserves the Markdown body after frontmatter.
    pub instructions: String,
    /// Preserves the exact validated UTF-8 source for identity verification.
    pub source: String,
    /// Identifies the exact source bytes with plain BLAKE3.
    pub content_address: Hash,
    /// Controls whether the agent may receive events.
    pub enabled: bool,
    /// Carries `shared`, `per-event`, or an authored session template.
    pub session: String,
    /// Selects a concrete model endpoint when authored.
    pub model: Option<ModelSpec>,
    /// Selects the tool executor.
    pub executor: ExecutorSpec,
    /// Lists event types the agent accepts.
    pub accepts: Vec<String>,
    /// Lists event types the agent may publish.
    pub publishes: Vec<String>,
    /// Lists event types the agent may return.
    pub returns: Vec<String>,
    /// Lists explicitly selected skills.
    pub skills: Vec<String>,
    /// Records whether skills were omitted and should be inherited.
    pub skills_inherit: bool,
    /// Lists explicitly selected tools.
    pub tools: Vec<String>,
    /// Records whether tools were omitted and should be inherited.
    pub tools_inherit: bool,
    /// Carries structural schedules.
    pub schedules: Vec<ScheduleEntry>,
    /// Overrides retry policy when authored.
    pub retry: Option<RetrySpec>,
    /// Carries the authored base directory.
    pub base_dir: Option<PathBuf>,
    /// Carries typed ingress bindings parsed from `accepts`.
    pub ingress: Vec<IngressBinding>,
    /// Carries typed egress bindings parsed from `publishes`.
    pub egress: Vec<EgressBinding>,
    /// Lists runtime lock identities required by one invocation.
    #[serde(default)]
    pub locks: Vec<String>,
    /// Preserves non-core frontmatter for later validation.
    pub extensions: Map<String, Value>,
}

/// Identifies a provider-qualified capability declaration.
///
/// The identity contains at least one provider segment and one operation
/// segment. It carries no executable callback or language-specific import.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize)]
#[serde(transparent)]
pub struct CapabilityId(String);

impl CapabilityId {
    /// Returns the canonical provider-qualified identity.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for CapabilityId {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.0.fmt(formatter)
    }
}

impl FromStr for CapabilityId {
    type Err = ManifestError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        let mut segments = value.split('.');
        let provider = segments.next();
        let operation = segments.next();
        let mut invalid_segment = false;
        for segment in value.split('.') {
            if segment.is_empty() {
                invalid_segment = true;
            }
        }
        let mut contains_whitespace = false;
        for character in value.chars() {
            if character.is_whitespace() {
                contains_whitespace = true;
            }
        }
        if provider.is_none() || operation.is_none() || invalid_segment || contains_whitespace {
            return Err(ManifestError::new(
                ManifestErrorKind::InvalidCapability,
                Some(value),
                Some("id"),
                "capability id must contain non-empty provider and operation segments",
            ));
        }
        Ok(CapabilityId(value.to_owned()))
    }
}

impl<'de> Deserialize<'de> for CapabilityId {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let value = String::deserialize(deserializer)?;
        value
            .parse::<CapabilityId>()
            .map_err(|error| D::Error::custom(error.to_string()))
    }
}

/// Declares one host-supplied capability without an executable callback.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CapabilitySpec {
    /// Carries the canonical provider-qualified identity.
    pub id: CapabilityId,
    /// Carries the model-facing name used by authored tool selections.
    pub name: String,
    /// Explains the operation to a model.
    pub description: String,
    /// Validates canonical JSON arguments.
    pub input_schema: Map<String, Value>,
    /// States the retry contract for an effectful operation.
    pub delivery_semantics: Option<DeliverySemantics>,
    /// Restricts an authored capability to one agent when present.
    pub owner: Option<String>,
    /// Identifies the implementation supplied by the host.
    pub implementation: ImplementationFingerprint,
}

/// Declares one host-supplied executor provider.
#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ExecutorProviderSpec {
    /// Names the provider selected from agent frontmatter.
    pub id: String,
    /// Identifies the provider implementation supplied by the host.
    pub implementation: ImplementationFingerprint,
}

/// Declares one host-resolved model selection.
#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ModelSelectionSpec {
    /// Names the configuration profile.
    pub profile: String,
    /// Names the provider model.
    pub model: String,
    /// Carries the provider endpoint.
    pub url: String,
    /// Carries an optional reasoning selection.
    pub thinking: Option<String>,
    /// Selects the provider protocol.
    pub api: String,
    /// Selects the model-facing tool presentation.
    pub tool_profile: String,
    /// Identifies the model adapter implementation.
    pub implementation: ImplementationFingerprint,
}

/// Returns whether an enabled agent accepts an exact event type.
///
/// # Examples
///
/// ```
/// let spec = zeta_manifest::parse_agent(
///     "worker",
///     b"---\nname: Worker\ndescription: Does work.\naccepts: [work.requested]\n---\n",
/// )?;
/// assert!(zeta_manifest::agent_accepts_event(&spec, "work.requested"));
/// # Ok::<(), zeta_manifest::AgentSpecError>(())
/// ```
pub fn agent_accepts_event(spec: &AgentSpec, event_type: &str) -> bool {
    if !spec.enabled {
        return false;
    }
    for accepted in &spec.accepts {
        if accepted == event_type {
            return true;
        }
    }
    false
}
