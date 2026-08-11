//! Defines authored agent declaration values.

use std::path::PathBuf;

use serde::Serialize;
use serde_json::{Map, Value};
use zeta::substrate::Hash;

/// Declares one schedule attached to an agent.
///
/// # Examples
///
/// ```
/// let schedule = zeta_authoring::ScheduleEntry {
///     cron: "0 18 * * 0".to_owned(),
///     timezone: Some("Europe/Paris".to_owned()),
///     catchup: Some("latest".to_owned()),
/// };
/// assert_eq!(schedule.timezone.as_deref(), Some("Europe/Paris"));
/// ```
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct ScheduleEntry {
    /// Carries the cron expression.
    pub cron: String,
    /// Names the schedule timezone when one is authored.
    pub timezone: Option<String>,
    /// Names the requested catch-up rule.
    pub catchup: Option<String>,
}

/// Selects one concrete model endpoint.
///
/// # Examples
///
/// ```
/// let model = zeta_authoring::ModelSpec {
///     name: "qwen3.6".to_owned(),
///     url: "http://127.0.0.1:8080/v1/chat/completions".to_owned(),
/// };
/// assert_eq!(model.name, "qwen3.6");
/// ```
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct ModelSpec {
    /// Names the model served by the endpoint.
    pub name: String,
    /// Carries the model endpoint URL.
    pub url: String,
}

/// Overrides the retry policy for one agent.
///
/// # Examples
///
/// ```
/// let retry = zeta_authoring::RetrySpec {
///     max_attempts: Some(3),
///     backoff_seconds: Some(1.5),
/// };
/// assert_eq!(retry.max_attempts, Some(3));
/// ```
#[derive(Clone, Debug, PartialEq, Serialize)]
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
/// let executor = zeta_authoring::ExecutorSpec::default();
/// assert_eq!(executor.provider, "local");
/// assert!(executor.config.is_empty());
/// ```
#[derive(Clone, Debug, PartialEq, Serialize)]
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
/// let binding = zeta_authoring::IngressBinding {
///     event: "message.received".to_owned(),
///     filter: serde_json::Map::new(),
///     idempotency_key: Some("message:{id}".to_owned()),
/// };
/// assert_eq!(binding.event, "message.received");
/// ```
#[derive(Clone, Debug, PartialEq, Serialize)]
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
/// let binding = zeta_authoring::EgressBinding {
///     event: "message.send".to_owned(),
///     options: serde_json::Map::new(),
///     idempotency_key: None,
/// };
/// assert_eq!(binding.event, "message.send");
/// ```
#[derive(Clone, Debug, PartialEq, Serialize)]
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
/// The content address identifies the exact source bytes. The path is a
/// caller-supplied logical label and is not opened by this crate.
///
/// # Examples
///
/// ```
/// use std::path::Path;
///
/// let spec = zeta_authoring::parse_agent(
///     Path::new("worker.md"),
///     b"---\nname: Worker\ndescription: Does work.\n---\nWork.\n",
/// )?;
/// assert_eq!(spec.slug, "worker");
/// # Ok::<(), zeta_authoring::SpecError>(())
/// ```
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct AgentSpec {
    /// Carries the lowercase identifier derived from the logical filename.
    pub slug: String,
    /// Carries the authored display name.
    pub name: String,
    /// Carries the authored description.
    pub description: String,
    /// Preserves the Markdown body after frontmatter.
    pub instructions: String,
    /// Preserves the caller-supplied logical source path.
    pub path: PathBuf,
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
    /// Preserves non-core frontmatter for later validation.
    pub extensions: Map<String, Value>,
}

/// Returns whether an enabled agent accepts an exact event type.
///
/// # Examples
///
/// ```
/// use std::path::Path;
///
/// let spec = zeta_authoring::parse_agent(
///     Path::new("worker.md"),
///     b"---\nname: Worker\ndescription: Does work.\naccepts: [work.requested]\n---\n",
/// )?;
/// assert!(zeta_authoring::matches(&spec, "work.requested"));
/// # Ok::<(), zeta_authoring::SpecError>(())
/// ```
pub fn matches(spec: &AgentSpec, event_type: &str) -> bool {
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

/// Returns the synthetic event type used by an agent schedule.
///
/// # Examples
///
/// ```
/// assert_eq!(
///     zeta_authoring::scheduled_event_type("digest"),
///     "agent.digest.scheduled"
/// );
/// ```
pub fn scheduled_event_type(agent_slug: &str) -> String {
    format!("agent.{agent_slug}.scheduled")
}
