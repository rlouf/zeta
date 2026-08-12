//! Concrete runtime services for native invocation composition.

use std::collections::BTreeSet;
use std::fmt;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{Map, Value};
use uuid::Uuid;
use zeta_agent::{
    AbortReason, AbortSignal, AgentError, AgentInvocation, AgentObserver, Capability, Clock,
    DeliverySemantics as AgentDeliverySemantics, DraftRecorder, IdSource, Observation,
    PromptEnvironment, PromptTransform, ResolvedCapability, ToolProfile,
};
use zeta_authoring::{
    verify_execution_manifest, DeliverySemantics as AuthoredDeliverySemantics, ExecutionManifest,
    ExecutionManifestId, ImplementationFingerprint, ProjectGenerationId, ProjectManifest,
};
use zeta_journal::DraftEvent;

/// Classifies a failure while projecting verified authoring data for execution.
///
/// # Examples
///
/// ```
/// let kind = zeta::PrepareAgentErrorKind::DuplicateToolName;
/// assert_eq!(kind.reason(), "duplicate_tool_name");
/// ```
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PrepareAgentErrorKind {
    /// A project or execution manifest fails verification or projection.
    InvalidManifest,
    /// An authored capability cannot become a native capability declaration.
    InvalidCapability,
    /// Two capabilities resolve to the same model-facing name.
    DuplicateToolName,
    /// A home-relative authored directory has no explicit home directory.
    MissingHomeDirectory,
    /// A resolved execution directory cannot be represented as UTF-8.
    NonUtf8Directory,
}

impl PrepareAgentErrorKind {
    /// Returns a stable machine-readable reason.
    ///
    /// # Examples
    ///
    /// ```
    /// assert_eq!(
    ///     zeta::PrepareAgentErrorKind::MissingHomeDirectory.reason(),
    ///     "missing_home_directory",
    /// );
    /// ```
    pub fn reason(self) -> &'static str {
        match self {
            PrepareAgentErrorKind::InvalidManifest => "invalid_manifest",
            PrepareAgentErrorKind::InvalidCapability => "invalid_capability",
            PrepareAgentErrorKind::DuplicateToolName => "duplicate_tool_name",
            PrepareAgentErrorKind::MissingHomeDirectory => "missing_home_directory",
            PrepareAgentErrorKind::NonUtf8Directory => "non_utf8_directory",
        }
    }
}

/// Reports a pure authored-agent preparation failure.
///
/// # Examples
///
/// ```
/// # fn inspect(error: &zeta::PrepareAgentError) {
/// assert!(!error.detail().is_empty());
/// let _kind = error.kind();
/// # }
/// ```
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PrepareAgentError {
    kind: PrepareAgentErrorKind,
    detail: String,
}

impl PrepareAgentError {
    fn new(kind: PrepareAgentErrorKind, detail: impl Into<String>) -> Self {
        PrepareAgentError {
            kind,
            detail: detail.into(),
        }
    }

    /// Returns the stable failure class.
    ///
    /// # Examples
    ///
    /// ```
    /// # fn inspect(error: &zeta::PrepareAgentError) {
    /// let _kind: zeta::PrepareAgentErrorKind = error.kind();
    /// # }
    /// ```
    pub fn kind(&self) -> PrepareAgentErrorKind {
        self.kind
    }

    /// Returns the human-readable failure detail.
    ///
    /// # Examples
    ///
    /// ```
    /// # fn inspect(error: &zeta::PrepareAgentError) {
    /// let _detail: &str = error.detail();
    /// # }
    /// ```
    pub fn detail(&self) -> &str {
        &self.detail
    }
}

impl fmt::Display for PrepareAgentError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}: {}", self.kind.reason(), self.detail)
    }
}

impl std::error::Error for PrepareAgentError {}

/// Selects the executor provider and immutable authored configuration.
///
/// # Examples
///
/// ```
/// # fn inspect(selection: &zeta::ExecutorSelection) {
/// assert!(!selection.provider_id.is_empty());
/// let _config: &serde_json::Map<String, serde_json::Value> = &selection.config;
/// # }
/// ```
#[derive(Clone, Debug, PartialEq)]
pub struct ExecutorSelection {
    /// Names the selected executor provider.
    pub provider_id: String,
    /// Identifies the selected provider implementation.
    pub implementation: ImplementationFingerprint,
    /// Carries the agent's provider-specific configuration.
    pub config: Map<String, Value>,
}

/// Supplies values that vary for each invocation of one prepared agent.
///
/// # Examples
///
/// ```
/// # fn set_objective(mut inputs: zeta::InvocationInputs) {
/// inputs.objective = "Handle this event.".to_owned();
/// # }
/// ```
#[derive(Clone, Debug, PartialEq)]
pub struct InvocationInputs {
    /// States the objective already rendered for this triggering event.
    pub objective: String,
    /// Carries the prior normalized event timeline.
    pub timeline: Vec<Map<String, Value>>,
    /// Adds caller-owned project context.
    pub context: String,
    /// Supplies the directory containing the authored project.
    pub project_directory: PathBuf,
    /// Supplies the home directory used for exact `~` and `~/` expansion.
    pub home_directory: Option<PathBuf>,
    /// Overrides the authored execution directory for this invocation.
    pub base_directory_override: Option<PathBuf>,
    /// Supplies the ISO calendar date shown to the model.
    pub calendar_date: String,
    /// Associates provider transport state with a model session.
    pub model_session_id: Option<String>,
    /// Bounds model calls while allowing a final pending tool batch.
    pub max_model_calls: usize,
    /// Supplies the model-facing output-token budget.
    pub max_tokens: u64,
    /// Selects the model's tool behavior.
    pub tool_choice: Value,
    /// Stabilizes side-effect identities across retries.
    pub effect_scope: Option<String>,
    /// Enables retry-stable control handles for a queue item.
    pub source_queue_item_id: Option<String>,
    /// Associates cancellation requests with the triggering session.
    pub source_session_id: Option<String>,
    /// Names the causal parent of the first model proposal.
    pub caused_by: Option<String>,
    /// Names the producer on model, tool, and turn drafts.
    pub event_source: String,
    /// Associates every emitted draft with one session.
    pub session_id: Option<String>,
    /// Associates every emitted draft with one run.
    pub run_id: Option<String>,
    /// Associates every emitted draft with one turn.
    pub turn_id: Option<String>,
    /// Selects deterministic prompt compaction behavior.
    pub prompt_transform: PromptTransform,
    /// Reports the caller's context threshold to budget queries.
    pub compaction_threshold_tokens: Option<usize>,
    /// Stops the run at this caller-defined clock value.
    pub deadline_ms: Option<i64>,
}

/// Holds one verified, immutable authored projection for repeated invocations.
///
/// # Examples
///
/// ```
/// # fn inspect(agent: &zeta::PreparedAgent) {
/// assert!(!agent.agent_slug().is_empty());
/// let _capabilities: &[zeta_agent::ResolvedCapability] = agent.capabilities();
/// # }
/// ```
#[derive(Clone, Debug, PartialEq)]
pub struct PreparedAgent {
    execution_manifest_id: ExecutionManifestId,
    project_generation_id: ProjectGenerationId,
    agent_slug: String,
    agent_description: String,
    allowed_capabilities: Vec<zeta_agent::CapabilityId>,
    capabilities: Vec<ResolvedCapability>,
    model_name: Option<String>,
    model_url: Option<String>,
    model_api: String,
    thinking: Option<String>,
    tool_profile: ToolProfile,
    publishable_events: Map<String, Value>,
    executor_selection: ExecutorSelection,
    authored_base_directory: Option<PathBuf>,
}

impl PreparedAgent {
    /// Returns the verified execution-manifest identity.
    ///
    /// # Examples
    ///
    /// ```
    /// # fn inspect(agent: &zeta::PreparedAgent) {
    /// let _id: zeta_authoring::ExecutionManifestId = agent.execution_manifest_id();
    /// # }
    /// ```
    pub fn execution_manifest_id(&self) -> ExecutionManifestId {
        self.execution_manifest_id
    }

    /// Returns the verified project-generation identity.
    ///
    /// # Examples
    ///
    /// ```
    /// # fn inspect(agent: &zeta::PreparedAgent) {
    /// let _id: zeta_authoring::ProjectGenerationId = agent.project_generation_id();
    /// # }
    /// ```
    pub fn project_generation_id(&self) -> ProjectGenerationId {
        self.project_generation_id
    }

    /// Returns the authored agent slug.
    ///
    /// # Examples
    ///
    /// ```
    /// # fn inspect(agent: &zeta::PreparedAgent) {
    /// let _slug: &str = agent.agent_slug();
    /// # }
    /// ```
    pub fn agent_slug(&self) -> &str {
        &self.agent_slug
    }

    /// Returns the authored agent description used as the base system prompt.
    ///
    /// # Examples
    ///
    /// ```
    /// # fn inspect(agent: &zeta::PreparedAgent) {
    /// let _description: &str = agent.agent_description();
    /// # }
    /// ```
    pub fn agent_description(&self) -> &str {
        &self.agent_description
    }

    /// Returns resolved capabilities in authored grant order.
    ///
    /// # Examples
    ///
    /// ```
    /// # fn inspect(agent: &zeta::PreparedAgent) {
    /// let _capabilities: &[zeta_agent::ResolvedCapability] = agent.capabilities();
    /// # }
    /// ```
    pub fn capabilities(&self) -> &[ResolvedCapability] {
        &self.capabilities
    }

    /// Returns the selected executor provider and authored configuration.
    ///
    /// # Examples
    ///
    /// ```
    /// # fn inspect(agent: &zeta::PreparedAgent) {
    /// let _selection: &zeta::ExecutorSelection = agent.executor_selection();
    /// # }
    /// ```
    pub fn executor_selection(&self) -> &ExecutorSelection {
        &self.executor_selection
    }

    /// Constructs one portable agent invocation from explicit per-run inputs.
    ///
    /// # Errors
    ///
    /// Returns [`PrepareAgentError`] when a home-relative directory has no
    /// supplied home or the resolved directory is not valid UTF-8.
    ///
    /// # Examples
    ///
    /// ```
    /// # fn invoke(
    /// #     agent: &zeta::PreparedAgent,
    /// #     inputs: zeta::InvocationInputs,
    /// # ) -> Result<(), zeta::PrepareAgentError> {
    /// let invocation = agent.invocation(inputs)?;
    /// assert_eq!(invocation.source_agent_id.as_deref(), Some(agent.agent_slug()));
    /// # Ok(())
    /// # }
    /// ```
    pub fn invocation(
        &self,
        inputs: InvocationInputs,
    ) -> Result<AgentInvocation, PrepareAgentError> {
        let InvocationInputs {
            objective,
            timeline,
            context,
            project_directory,
            home_directory,
            base_directory_override,
            calendar_date,
            model_session_id,
            max_model_calls,
            max_tokens,
            tool_choice,
            effect_scope,
            source_queue_item_id,
            source_session_id,
            caused_by,
            event_source,
            session_id,
            run_id,
            turn_id,
            prompt_transform,
            compaction_threshold_tokens,
            deadline_ms,
        } = inputs;
        let base_directory = resolve_base_directory(
            &project_directory,
            home_directory.as_deref(),
            base_directory_override,
            self.authored_base_directory.as_deref(),
        )?;
        Ok(AgentInvocation {
            objective,
            timeline,
            context,
            system_prompt: Some(self.agent_description.clone()),
            allowed_capabilities: self.allowed_capabilities.clone(),
            tool_profile: self.tool_profile,
            max_model_calls,
            model_name: self.model_name.clone(),
            model_url: self.model_url.clone(),
            model_api: Some(self.model_api.clone()),
            thinking: self.thinking.clone(),
            model_session_id,
            max_tokens,
            tool_choice,
            base_directory: Some(base_directory.clone()),
            effect_scope,
            source_queue_item_id,
            source_agent_id: Some(self.agent_slug.clone()),
            source_session_id,
            caused_by,
            event_source,
            session_id,
            run_id,
            turn_id,
            environment: PromptEnvironment {
                working_directory: base_directory,
                calendar_date,
            },
            prompt_transform,
            compaction_threshold_tokens,
            deadline_ms,
            publishable_events: self.publishable_events.clone(),
        })
    }
}

/// Verifies and projects one authored execution manifest for repeated runs.
///
/// # Errors
///
/// Returns [`PrepareAgentError`] when the manifests fail verification, a
/// capability cannot be projected, or model-facing tool names collide.
///
/// # Examples
///
/// ```
/// # fn prepare(
/// #     project: &zeta_authoring::ProjectManifest,
/// #     execution: &zeta_authoring::ExecutionManifest,
/// # ) -> Result<(), zeta::PrepareAgentError> {
/// let agent = zeta::prepare_agent(project, execution)?;
/// assert_eq!(agent.execution_manifest_id(), execution.id);
/// # Ok(())
/// # }
/// ```
pub fn prepare_agent(
    project: &ProjectManifest,
    execution: &ExecutionManifest,
) -> Result<PreparedAgent, PrepareAgentError> {
    verify_execution_manifest(execution, project).map_err(|error| {
        PrepareAgentError::new(PrepareAgentErrorKind::InvalidManifest, error.to_string())
    })?;
    let (model_name, model_url) = resolved_model_endpoint(execution);
    let (model_api, thinking, tool_profile) = resolved_model_contract(execution)?;
    let (allowed_capabilities, capabilities) = resolved_capabilities(execution, tool_profile)?;
    let publishable_events = publishable_events(execution)?;
    Ok(PreparedAgent {
        execution_manifest_id: execution.id,
        project_generation_id: execution.project_generation,
        agent_slug: execution.agent.slug.clone(),
        agent_description: execution.agent.description.clone(),
        allowed_capabilities,
        capabilities,
        model_name,
        model_url,
        model_api,
        thinking,
        tool_profile,
        publishable_events,
        executor_selection: ExecutorSelection {
            provider_id: execution.executor_provider.id.clone(),
            implementation: execution.executor_provider.implementation.clone(),
            config: execution.agent.executor.config.clone(),
        },
        authored_base_directory: execution.agent.base_dir.clone(),
    })
}

fn resolved_model_endpoint(execution: &ExecutionManifest) -> (Option<String>, Option<String>) {
    let agent = &execution.agent.model;
    let project = &execution.model;
    let model_name = match agent {
        Some(agent) => Some(agent.name.clone()),
        None => project.as_ref().map(|project| project.model.clone()),
    };
    let model_url = match agent {
        Some(agent) => Some(agent.url.clone()),
        None => project.as_ref().map(|project| project.url.clone()),
    };
    (model_name, model_url)
}

fn resolved_model_contract(
    execution: &ExecutionManifest,
) -> Result<(String, Option<String>, ToolProfile), PrepareAgentError> {
    let Some(model) = &execution.model else {
        return Ok(("chat-completions".to_owned(), None, ToolProfile::Native));
    };
    let profile = match model.tool_profile.as_str() {
        "native" => ToolProfile::Native,
        "codex" => ToolProfile::Codex,
        value => {
            return Err(PrepareAgentError::new(
                PrepareAgentErrorKind::InvalidManifest,
                format!("unsupported verified tool profile {value:?}"),
            ))
        }
    };
    Ok((model.api.clone(), model.thinking.clone(), profile))
}

fn resolved_capabilities(
    execution: &ExecutionManifest,
    profile: ToolProfile,
) -> Result<(Vec<zeta_agent::CapabilityId>, Vec<ResolvedCapability>), PrepareAgentError> {
    let mut allowed = Vec::with_capacity(execution.agent.tools.len());
    let mut resolved = Vec::with_capacity(execution.agent.tools.len());
    let mut names = BTreeSet::new();
    for id in &execution.agent.tools {
        let Some(authored) = execution.capabilities.get(id) else {
            return Err(PrepareAgentError::new(
                PrepareAgentErrorKind::InvalidManifest,
                format!("verified execution manifest omits capability {id:?}"),
            ));
        };
        let id = authored.id.as_str().parse().map_err(|error: AgentError| {
            PrepareAgentError::new(PrepareAgentErrorKind::InvalidCapability, error.to_string())
        })?;
        let capability = Capability {
            id,
            description: authored.description.clone(),
            input_schema: authored.input_schema.clone(),
            delivery_semantics: authored.delivery_semantics.map(delivery_semantics),
        };
        let capability = profile.resolve_capability(&capability, &authored.name);
        if !names.insert(capability.model_name.clone()) {
            return Err(PrepareAgentError::new(
                PrepareAgentErrorKind::DuplicateToolName,
                format!(
                    "multiple capabilities resolve to model-facing name {:?}",
                    capability.model_name
                ),
            ));
        }
        allowed.push(capability.canonical.id.clone());
        resolved.push(capability);
    }
    Ok((allowed, resolved))
}

fn delivery_semantics(value: AuthoredDeliverySemantics) -> AgentDeliverySemantics {
    match value {
        AuthoredDeliverySemantics::IdempotentWithKey => AgentDeliverySemantics::IdempotentWithKey,
        AuthoredDeliverySemantics::ConnectorDeduplicated => {
            AgentDeliverySemantics::ConnectorDeduplicated
        }
        AuthoredDeliverySemantics::AtLeastOnce => AgentDeliverySemantics::AtLeastOnce,
        AuthoredDeliverySemantics::UnsafeToRetry => AgentDeliverySemantics::UnsafeToRetry,
    }
}

fn publishable_events(
    execution: &ExecutionManifest,
) -> Result<Map<String, Value>, PrepareAgentError> {
    let mut publishable = Map::new();
    for event_type in &execution.agent.publishes {
        let Some(schema) = execution.events.schema(event_type) else {
            return Err(PrepareAgentError::new(
                PrepareAgentErrorKind::InvalidManifest,
                format!("verified execution manifest omits event {event_type:?}"),
            ));
        };
        let schema = match schema {
            Some(schema) => Value::Object(schema.clone()),
            None => Value::Null,
        };
        publishable.insert(event_type.clone(), schema);
    }
    Ok(publishable)
}

fn resolve_base_directory(
    project_directory: &Path,
    home_directory: Option<&Path>,
    override_directory: Option<PathBuf>,
    authored_directory: Option<&Path>,
) -> Result<String, PrepareAgentError> {
    let directory = if let Some(directory) = override_directory {
        directory
    } else if let Some(directory) = authored_directory {
        resolve_authored_directory(project_directory, home_directory, directory)?
    } else {
        project_directory.to_path_buf()
    };
    let Some(directory) = directory.to_str() else {
        return Err(PrepareAgentError::new(
            PrepareAgentErrorKind::NonUtf8Directory,
            "resolved base directory must be valid UTF-8",
        ));
    };
    Ok(directory.to_owned())
}

fn resolve_authored_directory(
    project_directory: &Path,
    home_directory: Option<&Path>,
    authored_directory: &Path,
) -> Result<PathBuf, PrepareAgentError> {
    if authored_directory.is_absolute() {
        return Ok(authored_directory.to_path_buf());
    }
    let Some(directory) = authored_directory.to_str() else {
        return Err(PrepareAgentError::new(
            PrepareAgentErrorKind::NonUtf8Directory,
            "authored base directory must be valid UTF-8",
        ));
    };
    if directory == "~" || directory.starts_with("~/") {
        let Some(home_directory) = home_directory else {
            return Err(PrepareAgentError::new(
                PrepareAgentErrorKind::MissingHomeDirectory,
                "home-relative base directory requires an explicit home directory",
            ));
        };
        if directory == "~" {
            return Ok(home_directory.to_path_buf());
        }
        return Ok(home_directory.join(&directory[2..]));
    }
    Ok(project_directory.join(authored_directory))
}

/// Reads Unix time from the operating-system clock.
#[derive(Clone, Copy, Debug, Default)]
pub struct SystemClock;

impl Clock for SystemClock {
    fn now_millis(&self) -> i64 {
        let elapsed = SystemTime::now().duration_since(UNIX_EPOCH);
        let Ok(elapsed) = elapsed else {
            return 0;
        };
        i64::try_from(elapsed.as_millis()).unwrap_or(i64::MAX)
    }
}

/// Shares the first cooperative abort reason across threads.
#[derive(Clone, Debug, Default)]
pub struct CancellationToken {
    reason: Arc<Mutex<Option<AbortReason>>>,
}

impl CancellationToken {
    /// Creates an active cancellation token.
    ///
    /// # Examples
    ///
    /// ```
    /// use zeta_agent::{AbortReason, AbortSignal};
    ///
    /// let token = zeta::CancellationToken::new();
    /// assert_eq!(token.reason(), None);
    /// assert!(token.cancel(AbortReason::Cancelled));
    /// assert_eq!(token.reason(), Some(AbortReason::Cancelled));
    /// ```
    pub fn new() -> Self {
        Self::default()
    }

    /// Records the first abort reason and reports whether this call won.
    pub fn cancel(&self, reason: AbortReason) -> bool {
        let Ok(mut current) = self.reason.lock() else {
            return false;
        };
        if current.is_some() {
            return false;
        }
        *current = Some(reason);
        true
    }
}

impl AbortSignal for CancellationToken {
    fn reason(&self) -> Option<AbortReason> {
        self.reason.lock().ok().and_then(|reason| *reason)
    }
}

/// Generates opaque UUID version 4 identities with one stable prefix.
#[derive(Clone, Debug)]
pub struct UuidIdSource {
    prefix: String,
}

impl UuidIdSource {
    /// Creates an identity source with a non-empty namespace prefix.
    ///
    /// # Examples
    ///
    /// ```
    /// use zeta_agent::IdSource;
    ///
    /// let mut source = zeta::UuidIdSource::new("event");
    /// assert!(source.next_id().unwrap().starts_with("event_"));
    /// ```
    pub fn new(prefix: impl Into<String>) -> Self {
        Self {
            prefix: prefix.into(),
        }
    }
}

impl IdSource for UuidIdSource {
    fn next_id(&mut self) -> Result<String, AgentError> {
        let prefix = self.prefix.trim();
        if prefix.is_empty() {
            return Err(AgentError::identity(
                "UUID identity prefix must not be empty",
            ));
        }
        Ok(format!("{prefix}_{}", Uuid::new_v4()))
    }
}

/// Forwards transient observations to an application callback.
pub struct CallbackObserver<F> {
    callback: F,
}

impl<F> CallbackObserver<F> {
    /// Creates an observer backed by one callback.
    pub fn new(callback: F) -> Self {
        Self { callback }
    }
}

impl<F> AgentObserver for CallbackObserver<F>
where
    F: FnMut(Observation),
{
    fn observe(&mut self, observation: Observation) {
        (self.callback)(observation);
    }
}

/// Forwards complete durable drafts to an application callback.
pub struct CallbackDraftRecorder<F> {
    callback: F,
}

impl<F> CallbackDraftRecorder<F> {
    /// Creates a draft recorder backed by one callback.
    pub fn new(callback: F) -> Self {
        Self { callback }
    }
}

impl<F> DraftRecorder for CallbackDraftRecorder<F>
where
    F: FnMut(&DraftEvent) -> Result<(), String>,
{
    fn record(&mut self, draft: &DraftEvent) -> Result<(), AgentError> {
        (self.callback)(draft).map_err(|error| AgentError::durability(error.to_string()))
    }
}
