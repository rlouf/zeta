//! Reactively routes durable native runtime ingress and agent work.

use std::collections::BTreeMap;
use std::fmt;
use std::panic::{self, AssertUnwindSafe};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, mpsc};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use chrono::{DateTime, SecondsFormat, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use tokio::sync::{broadcast, watch};
use uuid::Uuid;
use zeta_dispatch::{
    AttemptCompletion, AttemptFailure, AttemptFailureCode, ClaimToken, Dispatch, Effect,
    EffectDeliverySemantics, EffectStatus, EgressDeliveryClaim, QueueClaim, RetryPolicy, Route,
    RuntimeEventIdentity, effect_key, route_event,
};
use zeta_journal::{DraftEvent, Event, EventFilter};
use zeta_manifest::AgentSpec;

use crate::{
    CancellationToken, ProjectRevision, ProjectRevisionStore, PythonModelGateway,
    PythonProviderCatalog, PythonProviderHost, PythonToolExecutor, Scheduler,
    SharedPythonProviderHost, SystemClock, UuidIdSource, attempt_completion, host_model,
};
use zeta_substrate::{canonical_json, hash_bytes};

const DUE_ADVANCE_LIMIT: usize = 128;
const AGENT_CAPACITY: usize = 4;
const AGENT_LEASE_MS: u64 = 60_000;
const AGENT_HEARTBEAT_MS: i64 = 15_000;
const AGENT_WORKER_NAME: &str = "native-agent";
const EGRESS_CAPACITY: usize = 4;
const EGRESS_LEASE_MS: u64 = 60_000;
const EGRESS_HEARTBEAT_MS: i64 = 15_000;
const EGRESS_WORKER_NAME: &str = "native-egress";
const SCHEDULE_TICK_INTERVAL_MS: i64 = 30_000;
const SUBSCRIPTION_TICK_INTERVAL_MS: i64 = 1_000;
const SUBSCRIPTION_CURSOR_EVENT: &str = "runtime.connector.cursor";

/// Reports a native runtime failure.
#[derive(Debug)]
pub struct RuntimeError {
    detail: String,
}

impl RuntimeError {
    fn new(detail: impl Into<String>) -> Self {
        Self {
            detail: detail.into(),
        }
    }
}

impl fmt::Display for RuntimeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.detail)
    }
}

impl std::error::Error for RuntimeError {}

/// Carries one claimed agent invocation outside the Dispatch actor.
#[derive(Clone, Debug)]
struct AgentTask {
    /// Identifies the durable queue item.
    pub queue_item_id: String,
    /// Supplies the project directory from that revision.
    pub project_root: PathBuf,
    /// Identifies the exact provider catalog revision for this task.
    pub project_revision_id: String,
    /// Carries the selected Python provider catalog for this task.
    pub providers: PythonProviderCatalog,
    /// Carries the exact declared agent source.
    pub agent: AgentSpec,
    /// Carries the retained triggering event.
    pub event: Event,
    /// Identifies the resolved durable session.
    pub session_id: String,
    /// Identifies the durable invocation run.
    pub run_id: String,
    /// Selects the retry policy for this exact agent revision.
    pub retry_policy: RetryPolicy,
    /// Sends durable agent steps and live observations to the runtime actor.
    pub event_sink: Option<AgentEventSink>,
}

/// Reports one transient observation from an active agent run.
#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
pub struct AgentProgress {
    /// Identifies the durable queue item that owns this run.
    pub queue_item_id: String,
    /// Identifies the active agent.
    pub agent_slug: String,
    /// Identifies the resolved session.
    pub session_id: String,
    /// Identifies the active run.
    pub run_id: String,
    /// Carries the transient model observation.
    pub observation: zeta_agent::Observation,
}

/// Sends one agent worker's facts and observations to the runtime actor.
#[derive(Clone, Debug)]
struct AgentEventSink {
    sender: mpsc::Sender<RuntimeCommand>,
}

impl AgentEventSink {
    fn record(&self, event_id: &str, draft: &DraftEvent) -> Result<String, zeta_agent::AgentError> {
        let (reply, receiver) = mpsc::channel();
        self.sender
            .send(RuntimeCommand::RecordAgentDraft {
                event_id: event_id.to_owned(),
                draft: draft.clone(),
                reply,
            })
            .map_err(|_error| zeta_agent::AgentError::durability("the runtime actor is not running"))?;
        receiver
            .recv()
            .map_err(|_error| zeta_agent::AgentError::durability("the runtime actor stopped before it stored an agent draft"))?
            .map_err(zeta_agent::AgentError::durability)
    }

    fn observe(&self, progress: AgentProgress) {
        let _sent = self.sender.send(RuntimeCommand::AgentProgress { progress });
    }

    fn record_trace(&self, trace: &zeta_agent::TraceBatch) -> Result<(), zeta_agent::AgentError> {
        let (reply, receiver) = mpsc::channel();
        self.sender
            .send(RuntimeCommand::RecordAgentTrace {
                trace: trace.clone(),
                reply,
            })
            .map_err(|_error| zeta_agent::AgentError::durability("the runtime actor is not running"))?;
        receiver
            .recv()
            .map_err(|_error| zeta_agent::AgentError::durability("the runtime actor stopped before it stored an agent trace"))?
            .map_err(zeta_agent::AgentError::durability)
    }
}

struct RuntimeDraftRecorder {
    sink: Option<AgentEventSink>,
}

impl RuntimeDraftRecorder {
    fn new(sink: Option<AgentEventSink>) -> Self {
        Self { sink }
    }
}

impl zeta_agent::DraftRecorder for RuntimeDraftRecorder {
    fn record(
        &mut self,
        event_id: &str,
        draft: &DraftEvent,
    ) -> Result<String, zeta_agent::AgentError> {
        match &self.sink {
            Some(sink) => sink.record(event_id, draft),
            None => Ok(event_id.to_owned()),
        }
    }

    fn record_trace(&mut self, trace: &zeta_agent::TraceBatch) -> Result<(), zeta_agent::AgentError> {
        match &self.sink {
            Some(sink) => sink.record_trace(trace),
            None => Ok(()),
        }
    }
}

struct RuntimeAgentObserver {
    sink: Option<AgentEventSink>,
    queue_item_id: String,
    agent_slug: String,
    session_id: String,
    run_id: String,
}

impl RuntimeAgentObserver {
    fn new(task: &AgentTask) -> Self {
        Self {
            sink: task.event_sink.clone(),
            queue_item_id: task.queue_item_id.clone(),
            agent_slug: task.agent.slug.clone(),
            session_id: task.session_id.clone(),
            run_id: task.run_id.clone(),
        }
    }
}

impl zeta_agent::AgentObserver for RuntimeAgentObserver {
    fn observe(&mut self, observation: zeta_agent::Observation) {
        let Some(sink) = &self.sink else {
            return;
        };
        sink.observe(AgentProgress {
            queue_item_id: self.queue_item_id.clone(),
            agent_slug: self.agent_slug.clone(),
            session_id: self.session_id.clone(),
            run_id: self.run_id.clone(),
            observation,
        });
    }
}

/// Reports one agent-execution failure before Dispatch selects a retry.
#[derive(Clone, Debug)]
struct AgentExecutionError {
    detail: String,
    code: AttemptFailureCode,
}

impl AgentExecutionError {
    /// Creates a classified execution failure.
    fn new(detail: impl Into<String>, code: AttemptFailureCode) -> Self {
        Self {
            detail: detail.into(),
            code,
        }
    }
}

/// Returns a terminal proposal from one external agent execution.
#[derive(Clone, Debug)]
enum AgentExecution {
    /// Carries a validated successful attempt proposal.
    Completed(AttemptCompletion),
    /// Carries a classified failure proposal.
    Failed(AgentExecutionError),
}

/// Describes one connector operation for the internal egress lane.
#[derive(Clone, Debug, PartialEq, Eq)]
struct EgressTarget {
    connector_id: String,
    operation: String,
    semantics: EffectDeliverySemantics,
}

#[allow(dead_code)]
impl EgressTarget {
    fn new(
        connector_id: impl Into<String>,
        operation: impl Into<String>,
        semantics: EffectDeliverySemantics,
    ) -> Result<Self, RuntimeError> {
        let connector_id = connector_id.into();
        let operation = operation.into();
        if connector_id.is_empty() || operation.is_empty() {
            return Err(RuntimeError::new(
                "connector egress fields must be non-empty",
            ));
        }
        Ok(Self {
            connector_id,
            operation,
            semantics,
        })
    }

    fn connector_id(&self) -> &str {
        &self.connector_id
    }

    fn operation(&self) -> &str {
        &self.operation
    }

    fn semantics(&self) -> EffectDeliverySemantics {
        self.semantics
    }
}

/// Carries one claimed connector delivery outside the Dispatch actor.
#[derive(Clone, Debug)]
#[allow(dead_code)]
struct EgressTask {
    /// Identifies the retry-stable external effect.
    effect_key: String,
    /// Identifies the project revision that selected this connector.
    project_revision_id: String,
    /// Supplies the project directory for the selected revision.
    project_root: PathBuf,
    /// Carries the selected Python provider catalog for this delivery.
    providers: PythonProviderCatalog,
    /// Identifies the connector child.
    connector_id: String,
    /// Names the connector method.
    operation: String,
    /// Carries the published event payload.
    payload: Map<String, Value>,
    /// Carries the authored connector options.
    options: Map<String, Value>,
    /// Carries the stable connector idempotency key.
    idempotency_key: String,
    /// Selects the durable retry contract.
    semantics: EffectDeliverySemantics,
}

/// Describes one connector subscription selected by an agent declaration.
#[derive(Clone, Debug, PartialEq)]
struct SubscriptionTarget {
    /// Identifies one stable connector subscription configuration.
    key: String,
    /// Identifies the connector provider.
    connector_id: String,
    /// Names the event that this subscription may publish.
    event_type: String,
    /// Carries connector-specific subscription selection values.
    filter: Map<String, Value>,
    /// Derives the durable ingress identity from a received event.
    idempotency_key: String,
}

/// Holds connector subscriptions and their durable cursors.
struct SubscriptionServices {
    provider_hosts: ProviderHosts,
    targets: Vec<SubscriptionTarget>,
    cursors: BTreeMap<String, Value>,
}

/// Reports the terminal result of one connector call.
#[derive(Clone, Debug)]
#[allow(dead_code)]
enum EgressExecution {
    /// Carries a connector result object.
    Completed(Map<String, Value>),
    /// Carries a retry-safe connector failure message.
    Failed(String),
}

type AgentRunner = dyn Fn(AgentTask, CancellationToken) -> AgentExecution + Send + Sync + 'static;
type EgressRunner =
    dyn Fn(EgressTask, CancellationToken) -> EgressExecution + Send + Sync + 'static;

/// Runs direct-model agents with no external capability grants.
///
/// This executor supports an agent-level OpenAI-compatible model declaration.
/// It preserves the durable attempt boundary before native tool execution is
/// enabled in a later lane.
#[derive(Clone, Default)]
struct ProviderHosts {
    hosts: Arc<Mutex<BTreeMap<String, SharedPythonProviderHost>>>,
}

impl ProviderHosts {
    fn host_for(&self, task: &AgentTask) -> Result<Option<SharedPythonProviderHost>, String> {
        self.host_for_revision(
            &task.project_root,
            &task.project_revision_id,
            &task.providers,
        )
    }

    fn host_for_revision(
        &self,
        project_root: &Path,
        project_revision_id: &str,
        providers: &PythonProviderCatalog,
    ) -> Result<Option<SharedPythonProviderHost>, String> {
        if providers.models().is_empty()
            && providers.tools().is_empty()
            && providers.connectors().is_empty()
        {
            return Ok(None);
        }
        let mut hosts = self
            .hosts
            .lock()
            .map_err(|_error| "the Python provider host registry is unavailable".to_owned())?;
        if let Some(host) = hosts.get(project_revision_id) {
            return Ok(Some(Arc::clone(host)));
        }
        let host = PythonProviderHost::start(project_root).map_err(|error| error.to_string())?;
        if host.catalog() != providers {
            return Err(format!(
                "Python provider catalog changed after revision {}",
                project_revision_id
            ));
        }
        let host = Arc::new(Mutex::new(host));
        hosts.insert(project_revision_id.to_owned(), Arc::clone(&host));
        Ok(Some(host))
    }
}

/// Runs one agent with Python providers when its selected model is Python-owned.
#[derive(Clone, Default)]
struct AgentExecutor {
    provider_hosts: ProviderHosts,
}

impl AgentExecutor {
    fn execute(&self, task: AgentTask, cancellation: CancellationToken) -> AgentExecution {
        let provider_host = match self.provider_hosts.host_for(&task) {
            Ok(host) => host,
            Err(error) => {
                return AgentExecution::Failed(AgentExecutionError::new(
                    error,
                    AttemptFailureCode::AgentExecutionFailed,
                ));
            }
        };
        let python_model = task.agent.model.as_ref().and_then(|model| {
            task.providers
                .models()
                .get(model.profile())
                .map(|provider| (model.profile().to_owned(), provider.tool_profile.clone()))
        });
        if let (Some(host), Some((model, tool_profile))) = (provider_host.as_ref(), python_model) {
            return self.execute_python(task, cancellation, Arc::clone(host), model, tool_profile);
        }
        self.execute_native(task, cancellation, provider_host)
    }

    fn execute_native(
        &self,
        task: AgentTask,
        cancellation: CancellationToken,
        provider_host: Option<SharedPythonProviderHost>,
    ) -> AgentExecution {
        let model = match host_model::resolve(
            task.agent.model.as_ref(),
            &task.project_root,
            &task.session_id,
        ) {
            Ok(model) => model.selection,
            Err(error) => {
                return AgentExecution::Failed(AgentExecutionError::new(
                    format!(
                        "cannot resolve a model for agent {:?}: {error}",
                        task.agent.slug
                    ),
                    AttemptFailureCode::AgentExecutionFailed,
                ));
            }
        };
        let base_directory = agent_base_directory(&task.agent, &task.project_root);
        let Some(base_directory) = base_directory.to_str() else {
            return AgentExecution::Failed(AgentExecutionError::new(
                "the agent base directory is not valid UTF-8",
                AttemptFailureCode::AgentExecutionFailed,
            ));
        };
        let mut endpoint = zeta_agent::ModelHttpEndpoint::new(&model.url);
        for (name, value) in &model.headers {
            endpoint = endpoint.with_header(name, value);
        }
        let gateway_config = match model.api.as_str() {
            "chat-completions" => zeta_agent::HttpModelGatewayConfig::new(Some(endpoint), None),
            "codex-responses" => zeta_agent::HttpModelGatewayConfig::new(None, Some(endpoint)),
            api => {
                return AgentExecution::Failed(AgentExecutionError::new(
                    format!(
                        "agent {:?} has unsupported model API {api:?}",
                        task.agent.slug
                    ),
                    AttemptFailureCode::AgentExecutionFailed,
                ));
            }
        };
        let gateway = zeta_agent::HttpModelGateway::new(gateway_config);
        let Ok(mut gateway) = gateway else {
            return AgentExecution::Failed(AgentExecutionError::new(
                "the native model gateway configuration is invalid",
                AttemptFailureCode::AgentExecutionFailed,
            ));
        };
        let web_search = if model.api == "codex-responses" {
            match zeta_agent::WebSearchConfig::from_responses_endpoint(
                &model.url,
                model.name.clone(),
                task.session_id.clone(),
            ) {
                Ok(config) => Some(config.with_headers(model.headers.clone())),
                Err(error) => {
                    return AgentExecution::Failed(AgentExecutionError::new(
                        error.to_string(),
                        AttemptFailureCode::AgentExecutionFailed,
                    ));
                }
            }
        } else {
            None
        };
        let mut timeline_event = Map::new();
        timeline_event.insert("id".to_owned(), Value::String(task.event.id.clone()));
        timeline_event.insert(
            "type".to_owned(),
            Value::String(task.event.event_type.clone()),
        );
        timeline_event.insert(
            "source".to_owned(),
            Value::String(task.event.source.clone()),
        );
        timeline_event.insert(
            "payload".to_owned(),
            Value::Object(task.event.payload.clone()),
        );
        let context = serde_json::to_string(&Value::Object(timeline_event.clone()))
            .unwrap_or_else(|_error| "{}".to_owned());
        let mut publishable_events = Map::new();
        for event_type in &task.agent.publishes {
            publishable_events.insert(event_type.clone(), Value::Null);
        }
        let capabilities = match provider_host.as_ref() {
            Some(_host) if !task.providers.tools().is_empty() => {
                match selected_native_and_python_capabilities(
                    &task.agent,
                    &task.providers,
                    model.tool_profile,
                ) {
                    Ok(capabilities) => capabilities,
                    Err(error) => {
                        return AgentExecution::Failed(AgentExecutionError::new(
                            error,
                            AttemptFailureCode::AgentExecutionFailed,
                        ));
                    }
                }
            }
            Some(_) | None => selected_native_capabilities(&task.agent, model.tool_profile),
        };
        let allowed_capabilities = capabilities
            .iter()
            .map(|capability| capability.canonical.id.clone())
            .collect();
        let invocation = zeta_agent::AgentInvocation {
            objective: format!("Handle the event {}.", task.event.event_type),
            timeline: vec![timeline_event],
            context,
            system_prompt: Some(task.agent.instructions.clone()),
            allowed_capabilities,
            tool_profile: model.tool_profile,
            max_model_calls: 25,
            model_name: Some(model.name),
            model_url: Some(model.url),
            model_api: Some(model.api),
            thinking: model.thinking,
            model_session_id: Some(task.session_id.clone()),
            max_tokens: 8_192,
            tool_choice: Value::String("auto".to_owned()),
            base_directory: Some(base_directory.to_owned()),
            effect_scope: Some(task.queue_item_id.clone()),
            source_queue_item_id: Some(task.queue_item_id.clone()),
            source_agent_id: Some(task.agent.slug.clone()),
            source_session_id: Some(task.session_id.clone()),
            caused_by: Some(task.event.id.clone()),
            event_source: format!("agent:{}", task.agent.slug),
            session_id: Some(task.session_id.clone()),
            run_id: Some(task.run_id.clone()),
            turn_id: task.event.turn_id.clone(),
            environment: zeta_agent::PromptEnvironment {
                working_directory: base_directory.to_owned(),
                calendar_date: Utc::now().date_naive().to_string(),
            },
            prompt_transform: zeta_agent::PromptTransform::None,
            compaction_threshold_tokens: None,
            deadline_ms: None,
            publishable_events,
        };
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build();
        let Ok(runtime) = runtime else {
            return AgentExecution::Failed(AgentExecutionError::new(
                "the native agent runtime cannot start",
                AttemptFailureCode::AgentExecutionFailed,
            ));
        };
        let executor = zeta_agent::NativeToolExecutor::new(zeta_agent::SystemCommandRunner);
        let mut executor = match web_search {
            Some(config) => match executor.with_web_search(config) {
                Ok(executor) => executor,
                Err(error) => {
                    return AgentExecution::Failed(AgentExecutionError::new(
                        error.to_string(),
                        AttemptFailureCode::AgentExecutionFailed,
                    ));
                }
            },
            None => executor,
        };
        let mut observer = RuntimeAgentObserver::new(&task);
        let mut recorder = RuntimeDraftRecorder::new(task.event_sink.clone());
        let mut ids = UuidIdSource::new("agent");
        let clock = SystemClock;
        let result = match provider_host.filter(|_host| !task.providers.tools().is_empty()) {
            Some(host) => {
                let mut executor = PythonToolExecutor::new(host, executor);
                runtime.block_on(
                    zeta_agent::AgentRunner::new(
                        &capabilities,
                        &mut gateway,
                        &mut executor,
                        &mut observer,
                        &mut recorder,
                        &mut ids,
                        &cancellation,
                        &clock,
                    )
                    .run(&invocation),
                )
            }
            None => runtime.block_on(
                zeta_agent::AgentRunner::new(
                    &capabilities,
                    &mut gateway,
                    &mut executor,
                    &mut observer,
                    &mut recorder,
                    &mut ids,
                    &cancellation,
                    &clock,
                )
                .run(&invocation),
            ),
        };
        let result = match result {
            Ok(result) => result,
            Err(error) => {
                return AgentExecution::Failed(AgentExecutionError::new(
                    error.to_string(),
                    AttemptFailureCode::AgentExecutionFailed,
                ));
            }
        };
        match attempt_completion(format_timestamp_now(), &result) {
            Ok(completion) => AgentExecution::Completed(completion),
            Err(error) => AgentExecution::Failed(AgentExecutionError::new(
                error.to_string(),
                AttemptFailureCode::AgentExecutionFailed,
            )),
        }
    }

    fn execute_python(
        &self,
        task: AgentTask,
        cancellation: CancellationToken,
        host: SharedPythonProviderHost,
        model: String,
        tool_profile: Option<Map<String, Value>>,
    ) -> AgentExecution {
        let base_directory = agent_base_directory(&task.agent, &task.project_root);
        let Some(base_directory) = base_directory.to_str() else {
            return AgentExecution::Failed(AgentExecutionError::new(
                "the agent base directory is not valid UTF-8",
                AttemptFailureCode::AgentExecutionFailed,
            ));
        };
        let mut timeline_event = Map::new();
        timeline_event.insert("id".to_owned(), Value::String(task.event.id.clone()));
        timeline_event.insert(
            "type".to_owned(),
            Value::String(task.event.event_type.clone()),
        );
        timeline_event.insert(
            "source".to_owned(),
            Value::String(task.event.source.clone()),
        );
        timeline_event.insert(
            "payload".to_owned(),
            Value::Object(task.event.payload.clone()),
        );
        let context = serde_json::to_string(&Value::Object(timeline_event.clone()))
            .unwrap_or_else(|_error| "{}".to_owned());
        let mut publishable_events = Map::new();
        for event_type in &task.agent.publishes {
            publishable_events.insert(event_type.clone(), Value::Null);
        }
        let capabilities =
            match selected_python_capabilities(&task.agent, &task.providers, tool_profile.as_ref())
            {
                Ok(capabilities) => capabilities,
                Err(error) => {
                    return AgentExecution::Failed(AgentExecutionError::new(
                        error,
                        AttemptFailureCode::AgentExecutionFailed,
                    ));
                }
            };
        let allowed_capabilities = capabilities
            .iter()
            .map(|capability| capability.canonical.id.clone())
            .collect();
        let invocation = zeta_agent::AgentInvocation {
            objective: format!("Handle the event {}.", task.event.event_type),
            timeline: vec![timeline_event],
            context,
            system_prompt: Some(task.agent.instructions.clone()),
            allowed_capabilities,
            tool_profile: zeta_agent::ToolProfile::Native,
            max_model_calls: 25,
            model_name: Some(model.clone()),
            model_url: None,
            model_api: Some("python".to_owned()),
            thinking: None,
            model_session_id: Some(task.session_id.clone()),
            max_tokens: 8_192,
            tool_choice: Value::String("auto".to_owned()),
            base_directory: Some(base_directory.to_owned()),
            effect_scope: Some(task.queue_item_id.clone()),
            source_queue_item_id: Some(task.queue_item_id.clone()),
            source_agent_id: Some(task.agent.slug.clone()),
            source_session_id: Some(task.session_id.clone()),
            caused_by: Some(task.event.id.clone()),
            event_source: format!("agent:{}", task.agent.slug),
            session_id: Some(task.session_id.clone()),
            run_id: Some(task.run_id.clone()),
            turn_id: task.event.turn_id.clone(),
            environment: zeta_agent::PromptEnvironment {
                working_directory: base_directory.to_owned(),
                calendar_date: Utc::now().date_naive().to_string(),
            },
            prompt_transform: zeta_agent::PromptTransform::None,
            compaction_threshold_tokens: None,
            deadline_ms: None,
            publishable_events,
        };
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build();
        let Ok(runtime) = runtime else {
            return AgentExecution::Failed(AgentExecutionError::new(
                "the native agent runtime cannot start",
                AttemptFailureCode::AgentExecutionFailed,
            ));
        };
        let native = zeta_agent::NativeToolExecutor::new(zeta_agent::SystemCommandRunner);
        let mut gateway = PythonModelGateway::new(Arc::clone(&host), model);
        let mut executor = PythonToolExecutor::new(host, native);
        let mut observer = RuntimeAgentObserver::new(&task);
        let mut recorder = RuntimeDraftRecorder::new(task.event_sink.clone());
        let mut ids = UuidIdSource::new("agent");
        let clock = SystemClock;
        let result = runtime.block_on(
            zeta_agent::AgentRunner::new(
                &capabilities,
                &mut gateway,
                &mut executor,
                &mut observer,
                &mut recorder,
                &mut ids,
                &cancellation,
                &clock,
            )
            .run(&invocation),
        );
        let result = match result {
            Ok(result) => result,
            Err(error) => {
                return AgentExecution::Failed(AgentExecutionError::new(
                    error.to_string(),
                    AttemptFailureCode::AgentExecutionFailed,
                ));
            }
        };
        match attempt_completion(format_timestamp_now(), &result) {
            Ok(completion) => AgentExecution::Completed(completion),
            Err(error) => AgentExecution::Failed(AgentExecutionError::new(
                error.to_string(),
                AttemptFailureCode::AgentExecutionFailed,
            )),
        }
    }
}

fn selected_native_capabilities(
    agent: &AgentSpec,
    profile: zeta_agent::ToolProfile,
) -> Vec<zeta_agent::ResolvedCapability> {
    let available = zeta_agent::native_capabilities();
    let selected = if agent.tools_inherit {
        available
    } else {
        available
            .into_iter()
            .filter(|capability| {
                agent.tools.iter().any(|selected| {
                    selected == capability.id.as_str() || selected == capability.id.model_name()
                })
            })
            .collect()
    };
    zeta_agent::resolve_capabilities(&selected, profile)
}

fn selected_python_capabilities(
    agent: &AgentSpec,
    providers: &PythonProviderCatalog,
    tool_profile: Option<&Map<String, Value>>,
) -> Result<Vec<zeta_agent::ResolvedCapability>, String> {
    let selected = selected_capability_definitions(agent, providers)?;
    let mut resolved = Vec::with_capacity(selected.len());
    for capability in selected {
        let model_name = tool_profile
            .and_then(|profile| profile.get(capability.id.as_str()))
            .and_then(Value::as_str)
            .filter(|name| !name.is_empty())
            .unwrap_or_else(|| capability.id.model_name())
            .to_owned();
        resolved.push(zeta_agent::ResolvedCapability {
            model_name,
            model_description: capability.description.clone(),
            model_input_schema: capability.input_schema.clone(),
            argument_adapter: zeta_agent::ArgumentAdapter::Identity,
            canonical: capability,
        });
    }
    Ok(resolved)
}

fn selected_native_and_python_capabilities(
    agent: &AgentSpec,
    providers: &PythonProviderCatalog,
    profile: zeta_agent::ToolProfile,
) -> Result<Vec<zeta_agent::ResolvedCapability>, String> {
    let selected = selected_capability_definitions(agent, providers)?;
    Ok(profile.resolve(&selected))
}

fn selected_capability_definitions(
    agent: &AgentSpec,
    providers: &PythonProviderCatalog,
) -> Result<Vec<zeta_agent::Capability>, String> {
    let native = zeta_agent::native_capabilities();
    let mut selected = Vec::new();
    if agent.tools_inherit {
        selected.extend(native);
        for provider in providers.tools().values() {
            selected.push(python_capability(provider)?);
        }
        return Ok(selected);
    }
    for identifier in &agent.tools {
        if let Some(provider) = providers.tools().get(identifier) {
            selected.push(python_capability(provider)?);
            continue;
        }
        let Some(capability) = native.iter().find(|capability| {
            capability.id.as_str() == identifier || capability.id.model_name() == identifier
        }) else {
            return Err(format!(
                "agent {:?} has unavailable tool {identifier:?}",
                agent.slug
            ));
        };
        selected.push(capability.clone());
    }
    Ok(selected)
}

fn python_capability(provider: &crate::PythonProvider) -> Result<zeta_agent::Capability, String> {
    let id = provider
        .id
        .parse()
        .map_err(|error: zeta_agent::AgentError| {
            format!(
                "Python tool {:?} has an invalid identifier: {error}",
                provider.id
            )
        })?;
    let input_schema = provider.input_schema.clone().unwrap_or_else(|| {
        serde_json::json!({"type": "object", "additionalProperties": true})
            .as_object()
            .cloned()
            .expect("the default Python tool schema is an object")
    });
    Ok(zeta_agent::Capability {
        id,
        description: format!("Run the {} tool.", provider.id),
        input_schema,
        delivery_semantics: None,
    })
}

fn agent_base_directory(agent: &AgentSpec, project_root: &Path) -> PathBuf {
    let Some(base_directory) = agent.base_dir.as_deref() else {
        return project_root.to_path_buf();
    };
    let base_directory = base_directory.to_string_lossy();
    if base_directory == "~" {
        return std::env::var_os("HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| project_root.to_path_buf());
    }
    if let Some(relative) = base_directory.strip_prefix("~/") {
        return std::env::var_os("HOME")
            .map(PathBuf::from)
            .map(|home| home.join(relative))
            .unwrap_or_else(|| project_root.join(relative));
    }
    let path = PathBuf::from(base_directory.as_ref());
    if path.is_absolute() {
        path
    } else {
        project_root.join(path)
    }
}

fn built_in_agent_runner(provider_hosts: ProviderHosts) -> Arc<AgentRunner> {
    let executor = AgentExecutor { provider_hosts };
    Arc::new(move |task, cancellation| executor.execute(task, cancellation))
}

/// Reports the result of one durably accepted ingress event.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct IngressResult {
    /// Contains the retained event identity.
    pub event_id: String,
    /// Reports whether this call appended a new durable event.
    pub inserted: bool,
    /// Counts the durable agent routes retained for the event.
    pub route_count: usize,
}

/// Reports durable work that the runtime can observe.
#[derive(Clone, Debug, Default, Deserialize, PartialEq, Serialize)]
pub struct RuntimeStatus {
    /// Identifies the latest local wake epoch.
    pub wake_epoch: u64,
    /// Counts queue items by durable lifecycle state.
    pub queue: BTreeMap<String, usize>,
    /// Counts active agent executions.
    pub active_agents: usize,
    /// States the configured agent execution capacity.
    pub agent_capacity: usize,
    /// Counts active connector deliveries.
    pub active_egress: usize,
    /// States the configured connector delivery capacity.
    pub egress_capacity: usize,
    /// Counts planned or retry-ready connector deliveries.
    pub pending_egress: usize,
    /// Counts active waits.
    pub active_waits: usize,
    /// Counts pending deferred publications.
    pub pending_publications: usize,
    /// Contains the next durable maintenance deadline when one exists.
    pub next_deadline_ms: Option<i64>,
}

/// Carries a coalescing local notification after durable work commits.
#[derive(Clone)]
pub struct RuntimeWake {
    sender: watch::Sender<u64>,
}

impl RuntimeWake {
    fn new() -> (Self, watch::Receiver<u64>) {
        let (sender, receiver) = watch::channel(0_u64);
        (Self { sender }, receiver)
    }

    /// Returns a receiver for future wake epochs.
    pub fn subscribe(&self) -> watch::Receiver<u64> {
        self.sender.subscribe()
    }

    fn signal(&self) -> u64 {
        let next = self.sender.borrow().saturating_add(1);
        self.sender.send_replace(next);
        next
    }

    fn epoch(&self) -> u64 {
        *self.sender.borrow()
    }
}

/// Owns Dispatch, agent lanes, and connector delivery lanes.
pub struct Runtime {
    sender: mpsc::Sender<RuntimeCommand>,
    wake: RuntimeWake,
    progress: broadcast::Sender<AgentProgress>,
    thread: Mutex<Option<thread::JoinHandle<()>>>,
}

impl Runtime {
    /// Opens a durable Dispatch actor with the built-in Rust agent executor.
    pub fn start(
        database_path: impl AsRef<Path>,
        revision: ProjectRevision,
    ) -> Result<Self, RuntimeError> {
        let provider_hosts = ProviderHosts::default();
        let agent_runner = built_in_agent_runner(provider_hosts.clone());
        let egress = built_in_egress_services(&revision, provider_hosts.clone())?;
        let subscriptions = built_in_subscription_services(&revision, provider_hosts)?;
        Self::start_inner(
            database_path.as_ref(),
            revision,
            Some(agent_runner),
            Some(egress),
            Some(subscriptions),
        )
    }

    fn start_inner(
        database_path: &Path,
        revision: ProjectRevision,
        agent_runner: Option<Arc<AgentRunner>>,
        egress: Option<EgressServices>,
        mut subscriptions: Option<SubscriptionServices>,
    ) -> Result<Self, RuntimeError> {
        let routes = revision
            .routes()
            .map_err(|error| RuntimeError::new(error.to_string()))?;
        let scheduler = Scheduler::from_agents(revision.agents())
            .map_err(|error| RuntimeError::new(error.to_string()))?;
        let archive_parent = database_path.parent().ok_or_else(|| {
            RuntimeError::new("the native dispatch database has no parent directory")
        })?;
        let archive = ProjectRevisionStore::new(archive_parent.join("revisions"));
        archive
            .record(&revision)
            .map_err(|error| RuntimeError::new(error.to_string()))?;
        let dispatch = Dispatch::open(database_path)
            .map_err(|error| RuntimeError::new(format!("cannot open native dispatch: {error}")))?;
        if let Some(subscriptions) = subscriptions.as_mut() {
            subscriptions.cursors = load_subscription_cursors(&dispatch)?;
        }
        let (sender, receiver) = mpsc::channel();
        let (wake, _initial_receiver) = RuntimeWake::new();
        let (progress, _initial_progress_receiver) = broadcast::channel(256);
        let actor_wake = wake.clone();
        let actor_sender = sender.clone();
        let actor_progress = progress.clone();
        let thread = thread::Builder::new()
            .name("zeta-dispatch".to_owned())
            .spawn(move || {
                run_actor(
                    dispatch,
                    revision,
                    routes,
                    scheduler,
                    archive,
                    receiver,
                    actor_sender,
                    actor_wake,
                    actor_progress,
                    agent_runner,
                    egress,
                    subscriptions,
                )
            })
            .map_err(|error| RuntimeError::new(format!("cannot start dispatch actor: {error}")))?;
        Ok(Self {
            sender,
            wake,
            progress,
            thread: Mutex::new(Some(thread)),
        })
    }

    #[cfg(test)]
    fn start_without_agents_for_test(
        database_path: impl AsRef<Path>,
        revision: ProjectRevision,
    ) -> Result<Self, RuntimeError> {
        Self::start_inner(database_path.as_ref(), revision, None, None, None)
    }

    #[cfg(test)]
    fn start_with_test_agent_runner(
        database_path: impl AsRef<Path>,
        revision: ProjectRevision,
        agent_runner: Arc<AgentRunner>,
    ) -> Result<Self, RuntimeError> {
        Self::start_inner(
            database_path.as_ref(),
            revision,
            Some(agent_runner),
            None,
            None,
        )
    }

    #[cfg(test)]
    fn start_with_test_agent_and_egress_runners(
        database_path: impl AsRef<Path>,
        revision: ProjectRevision,
        agent_runner: Arc<AgentRunner>,
        egress_runner: Arc<EgressRunner>,
        targets: BTreeMap<String, EgressTarget>,
    ) -> Result<Self, RuntimeError> {
        Self::start_inner(
            database_path.as_ref(),
            revision,
            Some(agent_runner),
            Some(EgressServices {
                runner: egress_runner,
                targets,
            }),
            None,
        )
    }

    /// Returns a receiver for every post-commit work notification.
    pub fn subscribe(&self) -> watch::Receiver<u64> {
        self.wake.subscribe()
    }

    /// Returns a receiver for transient agent progress updates.
    pub fn subscribe_progress(&self) -> broadcast::Receiver<AgentProgress> {
        self.progress.subscribe()
    }

    /// Durably stores and immediately routes one external event.
    pub fn ingest(&self, draft: DraftEvent) -> Result<IngressResult, RuntimeError> {
        self.request(|reply| RuntimeCommand::Ingest { draft, reply })
    }

    /// Replaces routes for future ingress without changing existing work.
    pub fn reload(&self, revision: ProjectRevision) -> Result<(), RuntimeError> {
        self.request(|reply| RuntimeCommand::Reload {
            revision: revision,
            reply,
        })
    }

    /// Returns durable queue and timer state.
    pub fn status(&self) -> Result<RuntimeStatus, RuntimeError> {
        self.request(|reply| RuntimeCommand::Status { reply })
    }

    /// Publishes due schedules and starts their routed agent work.
    pub fn tick_schedules(&self, now_ms: i64) -> Result<usize, RuntimeError> {
        self.request(|reply| RuntimeCommand::TickSchedules { now_ms, reply })
    }

    /// Stops the actor and asks active agents to cancel.
    pub fn shutdown(&self) -> Result<(), RuntimeError> {
        let result = self.request(|reply| RuntimeCommand::Shutdown { reply });
        let thread = self
            .thread
            .lock()
            .map_err(|_error| RuntimeError::new("the dispatch actor state is unavailable"))?
            .take();
        if let Some(thread) = thread {
            thread
                .join()
                .map_err(|_error| RuntimeError::new("the dispatch actor panicked"))?;
        }
        result
    }

    fn request<T>(
        &self,
        build: impl FnOnce(mpsc::Sender<Result<T, RuntimeError>>) -> RuntimeCommand,
    ) -> Result<T, RuntimeError> {
        let (sender, receiver) = mpsc::channel();
        self.sender
            .send(build(sender))
            .map_err(|_error| RuntimeError::new("the dispatch actor is not running"))?;
        receiver
            .recv()
            .map_err(|_error| RuntimeError::new("the dispatch actor stopped before replying"))?
    }
}

impl Drop for Runtime {
    fn drop(&mut self) {
        let _result = self.shutdown();
    }
}

enum RuntimeCommand {
    Ingest {
        draft: DraftEvent,
        reply: mpsc::Sender<Result<IngressResult, RuntimeError>>,
    },
    Reload {
        revision: ProjectRevision,
        reply: mpsc::Sender<Result<(), RuntimeError>>,
    },
    Status {
        reply: mpsc::Sender<Result<RuntimeStatus, RuntimeError>>,
    },
    TickSchedules {
        now_ms: i64,
        reply: mpsc::Sender<Result<usize, RuntimeError>>,
    },
    Shutdown {
        reply: mpsc::Sender<Result<(), RuntimeError>>,
    },
    RecordAgentDraft {
        event_id: String,
        draft: DraftEvent,
        reply: mpsc::Sender<Result<String, String>>,
    },
    AgentProgress {
        progress: AgentProgress,
    },
    RecordAgentTrace {
        trace: zeta_agent::TraceBatch,
        reply: mpsc::Sender<Result<(), String>>,
    },
    AgentFinished {
        claim: QueueClaim,
        execution: AgentExecution,
        retry_policy: RetryPolicy,
    },
    EgressFinished {
        claim: EgressDeliveryClaim,
        effect: Effect,
        execution: EgressExecution,
        retry_policy: RetryPolicy,
    },
}

struct ActorState {
    revision: ProjectRevision,
    routes: Vec<Route>,
    scheduler: Scheduler,
    next_schedule_tick_ms: i64,
    next_subscription_tick_ms: i64,
    archive: ProjectRevisionStore,
    active_agents: BTreeMap<String, ActiveAgent>,
    active_egress: BTreeMap<String, ActiveEgress>,
    egress: Option<EgressServices>,
    subscriptions: Option<SubscriptionServices>,
}

struct ActiveAgent {
    claim: QueueClaim,
    cancellation: CancellationToken,
    heartbeat_at_ms: i64,
    thread: thread::JoinHandle<()>,
}

struct EgressServices {
    runner: Arc<EgressRunner>,
    targets: BTreeMap<String, EgressTarget>,
}

fn built_in_egress_services(
    revision: &ProjectRevision,
    provider_hosts: ProviderHosts,
) -> Result<EgressServices, RuntimeError> {
    let runner = Arc::new(move |task: EgressTask, cancellation: CancellationToken| {
        let host = match provider_hosts.host_for_revision(
            &task.project_root,
            &task.project_revision_id,
            &task.providers,
        ) {
            Ok(Some(host)) => host,
            Ok(None) => {
                return EgressExecution::Failed(
                    "the connector provider host is unavailable".to_owned(),
                );
            }
            Err(error) => return EgressExecution::Failed(error),
        };
        let mut request = Map::new();
        request.insert("operation".to_owned(), Value::String(task.operation));
        request.insert("payload".to_owned(), Value::Object(task.payload));
        request.insert("options".to_owned(), Value::Object(task.options));
        request.insert(
            "idempotency_key".to_owned(),
            Value::String(task.idempotency_key),
        );
        let mut host = match host.lock() {
            Ok(host) => host,
            Err(_error) => {
                return EgressExecution::Failed(
                    "the Python connector host is unavailable".to_owned(),
                );
            }
        };
        match host.deliver(
            &task.connector_id,
            request,
            Some(task.effect_key),
            &cancellation,
        ) {
            Ok(result) => EgressExecution::Completed(result),
            Err(error) => EgressExecution::Failed(error.to_string()),
        }
    });
    Ok(EgressServices {
        runner,
        targets: egress_targets(revision)?,
    })
}

fn egress_targets(
    revision: &ProjectRevision,
) -> Result<BTreeMap<String, EgressTarget>, RuntimeError> {
    let mut targets = BTreeMap::new();
    for agent in revision.active_agents() {
        let Some(spec) = revision.agent(&agent.slug) else {
            continue;
        };
        for binding in &spec.egress {
            let Some(connector_id) = &binding.connector else {
                continue;
            };
            if !revision.providers().connectors().contains_key(connector_id) {
                return Err(RuntimeError::new(format!(
                    "agent {:?} selects unavailable connector {connector_id:?}",
                    spec.slug
                )));
            }
            let target = EgressTarget::new(
                connector_id.clone(),
                binding.event.clone(),
                EffectDeliverySemantics::IdempotentWithKey,
            )?;
            if let Some(previous) = targets.insert(binding.event.clone(), target.clone()) {
                if previous != target {
                    return Err(RuntimeError::new(format!(
                        "published event {:?} selects multiple connectors",
                        binding.event
                    )));
                }
            }
        }
    }
    Ok(targets)
}

fn built_in_subscription_services(
    revision: &ProjectRevision,
    provider_hosts: ProviderHosts,
) -> Result<SubscriptionServices, RuntimeError> {
    Ok(SubscriptionServices {
        provider_hosts,
        targets: subscription_targets(revision)?,
        cursors: BTreeMap::new(),
    })
}

fn subscription_targets(
    revision: &ProjectRevision,
) -> Result<Vec<SubscriptionTarget>, RuntimeError> {
    let mut targets = BTreeMap::new();
    for agent in revision.active_agents() {
        let Some(spec) = revision.agent(&agent.slug) else {
            continue;
        };
        for binding in &spec.ingress {
            let Some(connector_id) = &binding.connector else {
                continue;
            };
            let Some(idempotency_key) = &binding.idempotency_key else {
                return Err(RuntimeError::new(format!(
                    "agent {:?} has connector ingress without an idempotency key",
                    spec.slug
                )));
            };
            if !revision.providers().connectors().contains_key(connector_id) {
                return Err(RuntimeError::new(format!(
                    "agent {:?} selects unavailable connector {connector_id:?}",
                    spec.slug
                )));
            }
            let key = subscription_target_key(
                connector_id,
                &binding.event,
                &binding.filter,
                idempotency_key,
            )?;
            targets
                .entry(key.clone())
                .or_insert_with(|| SubscriptionTarget {
                    key,
                    connector_id: connector_id.clone(),
                    event_type: binding.event.clone(),
                    filter: binding.filter.clone(),
                    idempotency_key: idempotency_key.clone(),
                });
        }
    }
    Ok(targets.into_values().collect())
}

fn subscription_target_key(
    connector_id: &str,
    event_type: &str,
    filter: &Map<String, Value>,
    idempotency_key: &str,
) -> Result<String, RuntimeError> {
    let value = serde_json::json!({
        "connector": connector_id,
        "event": event_type,
        "filter": filter,
        "idempotency_key": idempotency_key,
    });
    let bytes = canonical_json(&value).map_err(|error| {
        RuntimeError::new(format!("cannot identify connector subscription: {error}"))
    })?;
    Ok(format!("subscription:{}", hash_bytes(&bytes)))
}

struct ActiveEgress {
    claim: EgressDeliveryClaim,
    cancellation: CancellationToken,
    heartbeat_at_ms: i64,
    thread: thread::JoinHandle<()>,
}

fn run_actor(
    mut dispatch: Dispatch,
    revision: ProjectRevision,
    routes: Vec<Route>,
    scheduler: Scheduler,
    archive: ProjectRevisionStore,
    receiver: mpsc::Receiver<RuntimeCommand>,
    sender: mpsc::Sender<RuntimeCommand>,
    wake: RuntimeWake,
    progress: broadcast::Sender<AgentProgress>,
    agent_runner: Option<Arc<AgentRunner>>,
    egress: Option<EgressServices>,
    subscriptions: Option<SubscriptionServices>,
) {
    let now_ms = current_time_ms().unwrap_or(0);
    let next_subscription_tick_ms = subscriptions
        .as_ref()
        .filter(|services| !services.targets.is_empty())
        .map(|_services| now_ms)
        .unwrap_or(i64::MAX);
    let mut state = ActorState {
        revision,
        routes,
        scheduler,
        next_schedule_tick_ms: now_ms,
        next_subscription_tick_ms,
        archive,
        active_agents: BTreeMap::new(),
        active_egress: BTreeMap::new(),
        egress,
        subscriptions,
    };
    if refresh_actor_state(&mut dispatch, &mut state, &sender, agent_runner.as_ref())
        .unwrap_or(false)
    {
        wake.signal();
    }
    loop {
        let deadline = next_actor_deadline(&dispatch, &state).ok().flatten();
        let received = match deadline {
            Some(deadline) => receive_until(&receiver, deadline),
            None => receiver
                .recv()
                .map(Received::Command)
                .unwrap_or(Received::Closed),
        };
        match received {
            Received::Command(RuntimeCommand::Ingest { draft, reply }) => {
                let result = ingest_and_route(&mut dispatch, &state.routes, draft);
                let changed = result.is_ok()
                    && refresh_actor_state(
                        &mut dispatch,
                        &mut state,
                        &sender,
                        agent_runner.as_ref(),
                    )
                    .unwrap_or(false);
                if result.is_ok() || changed {
                    wake.signal();
                }
                let _sent = reply.send(result);
            }
            Received::Command(RuntimeCommand::Reload { revision, reply }) => {
                let result = reload_actor(&mut state, revision).and_then(|()| {
                    refresh_actor_state(&mut dispatch, &mut state, &sender, agent_runner.as_ref())
                        .map(|_changed| ())
                });
                if result.is_ok() {
                    wake.signal();
                }
                let _sent = reply.send(result);
            }
            Received::Command(RuntimeCommand::Status { reply }) => {
                let _sent = reply.send(status(&dispatch, &wake, &state));
            }
            Received::Command(RuntimeCommand::TickSchedules { now_ms, reply }) => {
                let result = tick_schedules(&mut dispatch, &mut state, now_ms).and_then(|count| {
                    refresh_actor_state(&mut dispatch, &mut state, &sender, agent_runner.as_ref())
                        .map(|_changed| count)
                });
                if result.is_ok() {
                    wake.signal();
                }
                let _sent = reply.send(result);
            }
            Received::Command(RuntimeCommand::Shutdown { reply }) => {
                for active in state.active_agents.values() {
                    let _cancelled = active
                        .cancellation
                        .cancel(zeta_agent::AbortReason::Cancelled);
                }
                for active in state.active_egress.values() {
                    let _cancelled = active
                        .cancellation
                        .cancel(zeta_agent::AbortReason::Cancelled);
                }
                let _sent = reply.send(Ok(()));
                return;
            }
            Received::Command(RuntimeCommand::RecordAgentDraft {
                event_id,
                draft,
                reply,
            }) => {
                let result = record_agent_draft(&mut dispatch, &event_id, draft);
                if result.is_ok() {
                    wake.signal();
                }
                let _sent = reply.send(result);
            }
            Received::Command(RuntimeCommand::AgentProgress { progress: update }) => {
                let _sent = progress.send(update);
            }
            Received::Command(RuntimeCommand::RecordAgentTrace { trace, reply }) => {
                let result = record_agent_trace(&mut dispatch, &trace);
                let _sent = reply.send(result);
            }
            Received::Command(RuntimeCommand::AgentFinished {
                claim,
                execution,
                retry_policy,
            }) => {
                let key = claim.token().as_str().to_owned();
                let Some(active) = state.active_agents.remove(&key) else {
                    continue;
                };
                let _joined = active.thread.join();
                let changed = commit_agent_execution(
                    &mut dispatch,
                    &state.routes,
                    &claim,
                    execution,
                    retry_policy,
                )
                .or_else(|error| {
                    fail_claimed_agent(&mut dispatch, &claim, retry_policy, error.to_string())
                })
                .and_then(|changed| {
                    let refreshed = refresh_actor_state(
                        &mut dispatch,
                        &mut state,
                        &sender,
                        agent_runner.as_ref(),
                    )?;
                    Ok(changed || refreshed)
                })
                .unwrap_or(false);
                if changed {
                    wake.signal();
                }
            }
            Received::Command(RuntimeCommand::EgressFinished {
                claim,
                effect,
                execution,
                retry_policy,
            }) => {
                let key = claim.token().as_str().to_owned();
                let Some(active) = state.active_egress.remove(&key) else {
                    continue;
                };
                let _joined = active.thread.join();
                let changed = commit_egress_execution(
                    &mut dispatch,
                    &claim,
                    &effect,
                    execution,
                    retry_policy,
                )
                .and_then(|changed| {
                    let refreshed = refresh_actor_state(
                        &mut dispatch,
                        &mut state,
                        &sender,
                        agent_runner.as_ref(),
                    )?;
                    Ok(changed || refreshed)
                })
                .unwrap_or(false);
                if changed {
                    wake.signal();
                }
            }
            Received::Deadline => {
                let subscribed = current_time_ms()
                    .ok()
                    .filter(|now_ms| *now_ms >= state.next_subscription_tick_ms)
                    .map(|now_ms| poll_subscriptions(&mut dispatch, &mut state, now_ms))
                    .transpose()
                    .unwrap_or(Some(false))
                    .unwrap_or(false);
                let scheduled = current_time_ms()
                    .ok()
                    .filter(|now_ms| *now_ms >= state.next_schedule_tick_ms)
                    .map(|now_ms| tick_schedules(&mut dispatch, &mut state, now_ms))
                    .transpose()
                    .map(|count| count.unwrap_or(0) > 0)
                    .unwrap_or(false);
                let changed = renew_due_agents(&mut dispatch, &mut state)
                    .and_then(|renewed_agents| {
                        let renewed_egress = renew_due_egress(&mut dispatch, &mut state)?;
                        Ok(renewed_agents || renewed_egress)
                    })
                    .and_then(|renewed| {
                        let refreshed = refresh_actor_state(
                            &mut dispatch,
                            &mut state,
                            &sender,
                            agent_runner.as_ref(),
                        )?;
                        Ok(subscribed || scheduled || renewed || refreshed)
                    })
                    .unwrap_or(false);
                if changed {
                    wake.signal();
                }
            }
            Received::Closed => return,
        }
    }
}

enum Received {
    Command(RuntimeCommand),
    Deadline,
    Closed,
}

fn receive_until(receiver: &mpsc::Receiver<RuntimeCommand>, deadline_ms: i64) -> Received {
    let Ok(now_ms) = current_time_ms() else {
        return Received::Deadline;
    };
    let remaining_ms = deadline_ms.saturating_sub(now_ms);
    if remaining_ms == 0 {
        return Received::Deadline;
    }
    match receiver.recv_timeout(Duration::from_millis(remaining_ms as u64)) {
        Ok(command) => Received::Command(command),
        Err(mpsc::RecvTimeoutError::Timeout) => Received::Deadline,
        Err(mpsc::RecvTimeoutError::Disconnected) => Received::Closed,
    }
}

fn next_actor_deadline(
    dispatch: &Dispatch,
    state: &ActorState,
) -> Result<Option<i64>, RuntimeError> {
    let now_ms = current_time_ms()?;
    if dispatch
        .has_due_maintenance(now_ms)
        .map_err(|error| RuntimeError::new(error.to_string()))?
    {
        return Ok(Some(now_ms));
    }
    let durable = dispatch
        .next_deadline_ms(now_ms)
        .map_err(|error| RuntimeError::new(error.to_string()))?;
    let egress = dispatch
        .next_egress_deadline_ms(now_ms)
        .map_err(|error| RuntimeError::new(error.to_string()))?;
    let agent_heartbeat = state
        .active_agents
        .values()
        .map(|active| active.heartbeat_at_ms)
        .min();
    let egress_heartbeat = state
        .active_egress
        .values()
        .map(|active| active.heartbeat_at_ms)
        .min();
    Ok([
        durable,
        egress,
        agent_heartbeat,
        egress_heartbeat,
        Some(state.next_schedule_tick_ms),
        Some(state.next_subscription_tick_ms),
    ]
    .into_iter()
    .flatten()
    .min())
}

fn reload_actor(state: &mut ActorState, revision: ProjectRevision) -> Result<(), RuntimeError> {
    let routes = revision
        .routes()
        .map_err(|error| RuntimeError::new(error.to_string()))?;
    let scheduler = Scheduler::from_agents(revision.agents())
        .map_err(|error| RuntimeError::new(error.to_string()))?;
    state
        .archive
        .record(&revision)
        .map_err(|error| RuntimeError::new(error.to_string()))?;
    if let Some(egress) = state.egress.as_mut() {
        egress.targets = egress_targets(&revision)?;
    }
    if let Some(subscriptions) = state.subscriptions.as_mut() {
        subscriptions.targets = subscription_targets(&revision)?;
        state.next_subscription_tick_ms = if subscriptions.targets.is_empty() {
            i64::MAX
        } else {
            current_time_ms()?
        };
    }
    state.revision = revision;
    state.routes = routes;
    state.scheduler = scheduler;
    state.next_schedule_tick_ms = current_time_ms()?;
    Ok(())
}

fn tick_schedules(
    dispatch: &mut Dispatch,
    state: &mut ActorState,
    now_ms: i64,
) -> Result<usize, RuntimeError> {
    state.next_schedule_tick_ms = now_ms.saturating_add(SCHEDULE_TICK_INTERVAL_MS);
    let mut ids = UuidIdSource::new("scheduler");
    let requested = state
        .scheduler
        .tick(dispatch, now_ms, &mut ids)
        .map_err(|error| RuntimeError::new(error.to_string()))?;
    route_unrouted(dispatch, &state.routes)?;
    Ok(requested.len())
}

struct SubscriptionBatch {
    events: Vec<Map<String, Value>>,
    cursor: Option<Value>,
}

fn poll_subscriptions(
    dispatch: &mut Dispatch,
    state: &mut ActorState,
    now_ms: i64,
) -> Result<bool, RuntimeError> {
    state.next_subscription_tick_ms = now_ms.saturating_add(SUBSCRIPTION_TICK_INTERVAL_MS);
    let Some(services) = state.subscriptions.as_mut() else {
        return Ok(false);
    };
    let targets = services.targets.clone();
    let cursors = services.cursors.clone();
    let provider_hosts = services.provider_hosts.clone();
    let revision = state.revision.clone();
    let routes = state.routes.clone();
    let mut changed = false;
    let mut advanced_cursors = Vec::new();
    for target in targets {
        let host = provider_hosts
            .host_for_revision(
                revision.project_root(),
                revision.revision_id(),
                revision.providers(),
            )
            .map_err(RuntimeError::new)?;
        let Some(host) = host else {
            return Err(RuntimeError::new(
                "the connector provider host is unavailable",
            ));
        };
        let mut request = Map::new();
        request.insert(
            "event_type".to_owned(),
            Value::String(target.event_type.clone()),
        );
        request.insert("filter".to_owned(), Value::Object(target.filter.clone()));
        if let Some(cursor) = cursors.get(&target.key) {
            request.insert("cursor".to_owned(), cursor.clone());
        }
        let cancellation = CancellationToken::new();
        let result = host
            .lock()
            .map_err(|_error| RuntimeError::new("the Python connector host is unavailable"))?
            .subscribe(&target.connector_id, request, &cancellation)
            .map_err(|error| RuntimeError::new(error.to_string()))?;
        let batch = parse_subscription_batch(result)?;
        for payload in batch.events {
            let provisional = Event::from_draft(
                &next_event_id("connector"),
                now_ms,
                DraftEvent {
                    event_type: target.event_type.clone(),
                    source: format!("connector:{}", target.connector_id),
                    payload: payload.clone(),
                    idempotency_key: None,
                    caused_by: None,
                    session_id: None,
                    run_id: None,
                    turn_id: None,
                },
            );
            let idempotency_key =
                zeta_dispatch::render_event_template(&target.idempotency_key, &provisional)
                    .map_err(|error| RuntimeError::new(error.to_string()))?;
            let outcome = ingest_and_route(
                dispatch,
                &routes,
                DraftEvent {
                    event_type: target.event_type.clone(),
                    source: format!("connector:{}", target.connector_id),
                    payload,
                    idempotency_key: Some(idempotency_key),
                    caused_by: None,
                    session_id: None,
                    run_id: None,
                    turn_id: None,
                },
            )?;
            changed |= outcome.inserted;
        }
        if let Some(cursor) = batch.cursor {
            advanced_cursors.push((target.key, cursor));
        }
    }
    let Some(services) = state.subscriptions.as_mut() else {
        return Ok(changed);
    };
    for (key, cursor) in advanced_cursors {
        record_subscription_cursor(dispatch, &key, &cursor, now_ms)?;
        services.cursors.insert(key, cursor);
        changed = true;
    }
    Ok(changed)
}

fn parse_subscription_batch(
    mut value: Map<String, Value>,
) -> Result<SubscriptionBatch, RuntimeError> {
    let events = match value.remove("events") {
        Some(Value::Array(events)) => events,
        Some(_) | None => {
            return Err(RuntimeError::new(
                "Python connector subscription result has no events array",
            ));
        }
    };
    let cursor = value.remove("cursor");
    if !value.is_empty() {
        return Err(RuntimeError::new(
            "Python connector subscription result has unknown fields",
        ));
    }
    let mut payloads = Vec::with_capacity(events.len());
    for event in events {
        let Value::Object(payload) = event else {
            return Err(RuntimeError::new(
                "Python connector subscription events must be objects",
            ));
        };
        payloads.push(payload);
    }
    Ok(SubscriptionBatch {
        events: payloads,
        cursor,
    })
}

fn load_subscription_cursors(dispatch: &Dispatch) -> Result<BTreeMap<String, Value>, RuntimeError> {
    let events = dispatch
        .list_events(&EventFilter {
            event_type: Some(SUBSCRIPTION_CURSOR_EVENT.to_owned()),
            ..EventFilter::default()
        })
        .map_err(|error| RuntimeError::new(error.to_string()))?;
    let mut cursors = BTreeMap::new();
    for event in events {
        if event.source != "runtime:connector" {
            continue;
        }
        let Some(key) = event.payload.get("subscription").and_then(Value::as_str) else {
            return Err(RuntimeError::new(
                "a connector subscription cursor lacks its subscription identity",
            ));
        };
        let Some(cursor) = event.payload.get("cursor") else {
            return Err(RuntimeError::new(
                "a connector subscription cursor lacks its cursor value",
            ));
        };
        cursors.insert(key.to_owned(), cursor.clone());
    }
    Ok(cursors)
}

fn record_subscription_cursor(
    dispatch: &mut Dispatch,
    key: &str,
    cursor: &Value,
    now_ms: i64,
) -> Result<(), RuntimeError> {
    let mut payload = Map::new();
    payload.insert("subscription".to_owned(), Value::String(key.to_owned()));
    payload.insert("cursor".to_owned(), cursor.clone());
    let event = Event::from_draft(
        &next_event_id("connector_cursor"),
        now_ms,
        DraftEvent {
            event_type: SUBSCRIPTION_CURSOR_EVENT.to_owned(),
            source: "runtime:connector".to_owned(),
            payload,
            idempotency_key: None,
            caused_by: None,
            session_id: None,
            run_id: None,
            turn_id: None,
        },
    );
    dispatch
        .append_trusted_event(event)
        .map_err(|error| RuntimeError::new(error.to_string()))?;
    Ok(())
}

fn ingest_and_route(
    dispatch: &mut Dispatch,
    routes: &[Route],
    draft: DraftEvent,
) -> Result<IngressResult, RuntimeError> {
    let event = Event::from_draft(&next_event_id("evt"), current_time_ms()?, draft);
    let outcome = dispatch
        .ingest_event(event)
        .map_err(|error| RuntimeError::new(error.to_string()))?;
    let _routed = route_unrouted(dispatch, routes)?;
    let route_count = dispatch
        .list_queue_items()
        .map_err(|error| RuntimeError::new(error.to_string()))?
        .into_iter()
        .filter(|item| item.event_id() == outcome.event.id && !item.target_agent().is_empty())
        .count();
    Ok(IngressResult {
        event_id: outcome.event.id,
        inserted: outcome.inserted,
        route_count,
    })
}

fn record_agent_draft(
    dispatch: &mut Dispatch,
    event_id: &str,
    draft: DraftEvent,
) -> Result<String, String> {
    let timestamp_ms = current_time_ms().map_err(|error| error.to_string())?;
    let event = Event::from_draft(event_id, timestamp_ms, draft);
    let outcome = dispatch
        .append_trusted_event(event)
        .map_err(|error| error.to_string())?;
    Ok(outcome.event.id)
}

fn record_agent_trace(
    dispatch: &mut Dispatch,
    trace: &zeta_agent::TraceBatch,
) -> Result<(), String> {
    let objects = trace
        .objects
        .iter()
        .map(|value| (value.id.as_str(), &value.object))
        .collect::<Vec<_>>();
    let derivations = trace
        .derivations
        .iter()
        .map(|value| (value.id.as_str(), &value.derivation))
        .collect::<Vec<_>>();
    dispatch
        .persist_trace(&objects, &derivations)
        .map_err(|error| error.to_string())
}

fn refresh_actor_state(
    dispatch: &mut Dispatch,
    state: &mut ActorState,
    sender: &mpsc::Sender<RuntimeCommand>,
    agent_runner: Option<&Arc<AgentRunner>>,
) -> Result<bool, RuntimeError> {
    let advanced = advance_actor_state(dispatch, &state.routes)?;
    let planned = plan_pending_egress(dispatch, state)?;
    let claimed_agents = fill_agent_lane(dispatch, state, sender, agent_runner)?;
    let claimed_egress = fill_egress_lane(dispatch, state, sender)?;
    Ok(advanced || planned || claimed_agents || claimed_egress)
}

fn advance_actor_state(dispatch: &mut Dispatch, routes: &[Route]) -> Result<bool, RuntimeError> {
    let now_ms = current_time_ms()?;
    let reconciled = dispatch
        .reconcile_expired_claims(now_ms)
        .map_err(|error| RuntimeError::new(error.to_string()))?;
    let reconciled_egress = dispatch
        .reconcile_expired_egress_delivery_claims(now_ms)
        .map_err(|error| RuntimeError::new(error.to_string()))?;
    let due = dispatch
        .advance_due(now_ms, DUE_ADVANCE_LIMIT, || Ok(runtime_identity(now_ms)))
        .map_err(|error| RuntimeError::new(error.to_string()))?;
    let routed = route_unrouted(dispatch, routes)?;
    Ok(reconciled > 0 || reconciled_egress > 0 || !due.is_empty() || routed > 0)
}

fn fill_agent_lane(
    dispatch: &mut Dispatch,
    state: &mut ActorState,
    sender: &mpsc::Sender<RuntimeCommand>,
    agent_runner: Option<&Arc<AgentRunner>>,
) -> Result<bool, RuntimeError> {
    let Some(agent_runner) = agent_runner else {
        return Ok(false);
    };
    let mut claimed = false;
    while state.active_agents.len() < AGENT_CAPACITY {
        let now_ms = current_time_ms()?;
        let token = ClaimToken::new(next_event_id("claim")).map_err(|error| {
            RuntimeError::new(format!("cannot create native agent claim: {error}"))
        })?;
        let Some(claim) = dispatch
            .claim_next_queue_item(AGENT_WORKER_NAME, token, AGENT_LEASE_MS, now_ms)
            .map_err(|error| RuntimeError::new(error.to_string()))?
        else {
            break;
        };
        claimed = true;
        let started = match dispatch.start_claimed_attempt(
            &claim,
            now_ms,
            runtime_identity(now_ms),
            runtime_identity(now_ms),
            &format_timestamp(now_ms)?,
            None,
        ) {
            Ok(started) => started,
            Err(error) => {
                let _released = dispatch
                    .release_claim(&claim, now_ms)
                    .map_err(|release| RuntimeError::new(release.to_string()))?;
                return Err(RuntimeError::new(error.to_string()));
            }
        };
        let mut task = match build_agent_task(dispatch, state, &claim, started.attempt()) {
            Ok(task) => task,
            Err(error) => {
                let _failed = fail_claimed_agent(
                    dispatch,
                    &claim,
                    RetryPolicy::default(),
                    error.to_string(),
                )?;
                continue;
            }
        };
        task.event_sink = Some(AgentEventSink {
            sender: sender.clone(),
        });
        let cancellation = CancellationToken::new();
        let worker_cancellation = cancellation.clone();
        let worker_runner = Arc::clone(agent_runner);
        let worker_sender = sender.clone();
        let worker_claim = claim.clone();
        let retry_policy = task.retry_policy;
        let thread = match thread::Builder::new()
            .name(format!("zeta-agent-{}", task.agent.slug))
            .spawn(move || {
                let execution = match panic::catch_unwind(AssertUnwindSafe(|| {
                    worker_runner(task, worker_cancellation)
                })) {
                    Ok(execution) => execution,
                    Err(_panic) => AgentExecution::Failed(AgentExecutionError::new(
                        "the native agent executor panicked",
                        AttemptFailureCode::AgentExecutionFailed,
                    )),
                };
                let _sent = worker_sender.send(RuntimeCommand::AgentFinished {
                    claim: worker_claim,
                    execution,
                    retry_policy,
                });
            }) {
            Ok(thread) => thread,
            Err(error) => {
                let _failed = fail_claimed_agent(
                    dispatch,
                    &claim,
                    retry_policy,
                    format!("cannot start native agent worker: {error}"),
                )?;
                continue;
            }
        };
        state.active_agents.insert(
            claim.token().as_str().to_owned(),
            ActiveAgent {
                claim,
                cancellation,
                heartbeat_at_ms: now_ms.saturating_add(AGENT_HEARTBEAT_MS),
                thread,
            },
        );
    }
    Ok(claimed)
}

fn plan_pending_egress(dispatch: &mut Dispatch, state: &ActorState) -> Result<bool, RuntimeError> {
    let Some(services) = &state.egress else {
        return Ok(false);
    };
    let events = dispatch
        .list_events(&EventFilter::default())
        .map_err(|error| RuntimeError::new(error.to_string()))?;
    let mut changed = false;
    for event in events {
        let Some(agent_slug) = event.source.strip_prefix("agent:") else {
            continue;
        };
        let Some((revision_id, queue_item_id)) = published_event_origin(dispatch, &event)? else {
            continue;
        };
        let revision = if revision_id == state.revision.revision_id() {
            state.revision.clone()
        } else {
            state
                .archive
                .load(&revision_id)
                .map_err(|error| RuntimeError::new(error.to_string()))?
                .ok_or_else(|| {
                    RuntimeError::new(format!(
                        "the egress project revision is unavailable: {revision_id}"
                    ))
                })?
        };
        let Some(agent) = revision.agent(agent_slug) else {
            return Err(RuntimeError::new(format!(
                "the egress agent is absent from revision {revision_id}: {agent_slug}"
            )));
        };
        let Some(binding) = agent
            .egress
            .iter()
            .find(|binding| binding.event == event.event_type)
        else {
            continue;
        };
        let Some(connector) = services.targets.get(&binding.event) else {
            continue;
        };
        let idempotency_key = match &binding.idempotency_key {
            Some(template) => zeta_dispatch::render_event_template(template, &event)
                .map_err(|error| RuntimeError::new(error.to_string()))?,
            None => format!("{}:{}", connector.connector_id(), event.id),
        };
        let mut params = Map::new();
        params.insert(
            "connector_id".to_owned(),
            Value::String(connector.connector_id().to_owned()),
        );
        params.insert(
            "connector_operation".to_owned(),
            Value::String(connector.operation().to_owned()),
        );
        params.insert(
            "event_type".to_owned(),
            Value::String(event.event_type.clone()),
        );
        params.insert("payload".to_owned(), Value::Object(event.payload.clone()));
        params.insert("options".to_owned(), Value::Object(binding.options.clone()));
        params.insert("idempotency_key".to_owned(), Value::String(idempotency_key));
        params.insert(
            "project_revision".to_owned(),
            Value::String(revision_id.clone()),
        );
        let operation = format!(
            "connector:{}:{}",
            connector.connector_id(),
            connector.operation()
        );
        let key = effect_key(&event.id, &operation, &params)
            .map_err(|error| RuntimeError::new(error.to_string()))?;
        let planned = planned_egress_effect(
            runtime_identity(current_time_ms()?),
            &event,
            queue_item_id.as_deref(),
            &key,
            &operation,
            connector.semantics(),
            params,
        );
        let outcome = dispatch
            .append_trusted_event(planned)
            .map_err(|error| RuntimeError::new(error.to_string()))?;
        changed |= outcome.inserted;
    }
    Ok(changed)
}

fn published_event_origin(
    dispatch: &Dispatch,
    event: &Event,
) -> Result<Option<(String, Option<String>)>, RuntimeError> {
    let chain = dispatch
        .causal_chain(&event.id)
        .map_err(|error| RuntimeError::new(error.to_string()))?;
    for ancestor in chain.into_iter().rev() {
        if ancestor.event_type != "runtime.attempt.completed" {
            continue;
        }
        let Some(revision_id) = ancestor
            .payload
            .get("project_revision")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
        else {
            return Ok(None);
        };
        let queue_item_id = ancestor
            .payload
            .get("queue_item_id")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .map(str::to_owned);
        return Ok(Some((revision_id.to_owned(), queue_item_id)));
    }
    Ok(None)
}

fn planned_egress_effect(
    identity: RuntimeEventIdentity,
    source: &Event,
    queue_item_id: Option<&str>,
    effect_key: &str,
    operation: &str,
    semantics: EffectDeliverySemantics,
    params: Map<String, Value>,
) -> Event {
    let mut payload = Map::new();
    payload.insert(
        "effect_key".to_owned(),
        Value::String(effect_key.to_owned()),
    );
    payload.insert("operation".to_owned(), Value::String(operation.to_owned()));
    payload.insert(
        "semantics".to_owned(),
        Value::String(effect_semantics_name(semantics).to_owned()),
    );
    payload.insert("scope".to_owned(), Value::String(source.id.clone()));
    payload.insert(
        "queue_item_id".to_owned(),
        queue_item_id
            .map(|value| Value::String(value.to_owned()))
            .unwrap_or(Value::Null),
    );
    payload.insert("params".to_owned(), Value::Object(params));
    payload.insert("status".to_owned(), Value::String("planned".to_owned()));
    Event {
        id: identity.id().to_owned(),
        event_type: "runtime.effect.planned".to_owned(),
        source: "zeta".to_owned(),
        payload,
        idempotency_key: Some(format!("runtime.effect.planned:{effect_key}")),
        caused_by: Some(source.id.clone()),
        session_id: source.session_id.clone(),
        run_id: source.run_id.clone(),
        turn_id: source.turn_id.clone(),
        timestamp_ms: identity.timestamp_ms(),
        cursor: None,
    }
}

fn effect_semantics_name(semantics: EffectDeliverySemantics) -> &'static str {
    match semantics {
        EffectDeliverySemantics::IdempotentWithKey => "idempotent_with_key",
        EffectDeliverySemantics::ConnectorDeduplicated => "connector_deduplicated",
        EffectDeliverySemantics::AtLeastOnce => "at_least_once",
        EffectDeliverySemantics::UnsafeToRetry => "unsafe_to_retry",
    }
}

fn fill_egress_lane(
    dispatch: &mut Dispatch,
    state: &mut ActorState,
    sender: &mpsc::Sender<RuntimeCommand>,
) -> Result<bool, RuntimeError> {
    let Some(services) = &state.egress else {
        return Ok(false);
    };
    let mut claimed = false;
    while state.active_egress.len() < EGRESS_CAPACITY {
        let now_ms = current_time_ms()?;
        let token = ClaimToken::new(next_event_id("egress_claim")).map_err(|error| {
            RuntimeError::new(format!("cannot create native egress claim: {error}"))
        })?;
        let Some((claim, _effect)) = dispatch
            .claim_next_egress_delivery(EGRESS_WORKER_NAME, token, EGRESS_LEASE_MS, now_ms)
            .map_err(|error| RuntimeError::new(error.to_string()))?
        else {
            break;
        };
        claimed = true;
        let effect = match dispatch.start_claimed_egress_delivery(
            &claim,
            now_ms,
            runtime_identity(now_ms),
        ) {
            Ok(effect) => effect,
            Err(error) => return Err(RuntimeError::new(error.to_string())),
        };
        let task = match build_egress_task(&effect, &state.revision, &state.archive) {
            Ok(task) => task,
            Err(error) => {
                commit_egress_execution(
                    dispatch,
                    &claim,
                    &effect,
                    EgressExecution::Failed(error.to_string()),
                    RetryPolicy::default(),
                )?;
                continue;
            }
        };
        let cancellation = CancellationToken::new();
        let worker_cancellation = cancellation.clone();
        let worker_runner = Arc::clone(&services.runner);
        let worker_sender = sender.clone();
        let worker_claim = claim.clone();
        let worker_effect = effect.clone();
        let retry_policy = RetryPolicy::default();
        let thread = match thread::Builder::new()
            .name(format!("zeta-egress-{}", task.connector_id))
            .spawn(move || {
                let execution = match panic::catch_unwind(AssertUnwindSafe(|| {
                    worker_runner(task, worker_cancellation)
                })) {
                    Ok(execution) => execution,
                    Err(_panic) => EgressExecution::Failed(
                        "the native connector egress worker panicked".to_owned(),
                    ),
                };
                let _sent = worker_sender.send(RuntimeCommand::EgressFinished {
                    claim: worker_claim,
                    effect: worker_effect,
                    execution,
                    retry_policy,
                });
            }) {
            Ok(thread) => thread,
            Err(error) => {
                commit_egress_execution(
                    dispatch,
                    &claim,
                    &effect,
                    EgressExecution::Failed(format!(
                        "cannot start native connector worker: {error}"
                    )),
                    retry_policy,
                )?;
                continue;
            }
        };
        state.active_egress.insert(
            claim.token().as_str().to_owned(),
            ActiveEgress {
                claim,
                cancellation,
                heartbeat_at_ms: now_ms.saturating_add(EGRESS_HEARTBEAT_MS),
                thread,
            },
        );
    }
    Ok(claimed)
}

fn build_egress_task(
    effect: &Effect,
    active_revision: &ProjectRevision,
    archive: &ProjectRevisionStore,
) -> Result<EgressTask, RuntimeError> {
    let value = |field: &'static str| {
        effect
            .params()
            .get(field)
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .map(str::to_owned)
            .ok_or_else(|| RuntimeError::new(format!("egress effect lacks {field}")))
    };
    let object = |field: &'static str| {
        effect
            .params()
            .get(field)
            .and_then(Value::as_object)
            .cloned()
            .ok_or_else(|| RuntimeError::new(format!("egress effect lacks {field}")))
    };
    let project_revision_id = value("project_revision")?;
    let revision = if project_revision_id == active_revision.revision_id() {
        active_revision.clone()
    } else {
        archive
            .load(&project_revision_id)
            .map_err(|error| RuntimeError::new(error.to_string()))?
            .ok_or_else(|| {
                RuntimeError::new(format!(
                    "the egress project revision is unavailable: {project_revision_id}"
                ))
            })?
    };
    Ok(EgressTask {
        effect_key: effect.key().to_owned(),
        project_revision_id,
        project_root: revision.project_root().to_path_buf(),
        providers: revision.providers().clone(),
        connector_id: value("connector_id")?,
        operation: value("connector_operation")?,
        payload: object("payload")?,
        options: object("options")?,
        idempotency_key: value("idempotency_key")?,
        semantics: effect.semantics(),
    })
}

fn commit_egress_execution(
    dispatch: &mut Dispatch,
    claim: &EgressDeliveryClaim,
    effect: &Effect,
    execution: EgressExecution,
    retry_policy: RetryPolicy,
) -> Result<bool, RuntimeError> {
    let now_ms = current_time_ms()?;
    match execution {
        EgressExecution::Completed(result) => dispatch
            .complete_claimed_egress_delivery(claim, now_ms, runtime_identity(now_ms), result)
            .map_err(|error| RuntimeError::new(error.to_string()))?,
        EgressExecution::Failed(detail) => {
            let mut result = Map::new();
            result.insert("error".to_owned(), Value::String(detail));
            if effect.semantics() == EffectDeliverySemantics::UnsafeToRetry {
                dispatch
                    .mark_claimed_egress_delivery_ambiguous(
                        claim,
                        now_ms,
                        runtime_identity(now_ms),
                        result,
                    )
                    .map_err(|error| RuntimeError::new(error.to_string()))?;
            } else {
                let retry_at = if retry_policy.permits_retry_after(effect.delivery_attempts()) {
                    let delay = retry_policy
                        .delay_ms(effect.delivery_attempts())
                        .map_err(|error| RuntimeError::new(error.to_string()))?;
                    let delay = i64::try_from(delay)
                        .map_err(|_error| RuntimeError::new("egress retry delay is too large"))?;
                    Some(now_ms.checked_add(delay).ok_or_else(|| {
                        RuntimeError::new("egress retry time is outside the Unix range")
                    })?)
                } else {
                    None
                };
                dispatch
                    .fail_claimed_egress_delivery(
                        claim,
                        now_ms,
                        runtime_identity(now_ms),
                        result,
                        retry_at,
                    )
                    .map_err(|error| RuntimeError::new(error.to_string()))?;
            }
        }
    }
    Ok(true)
}

fn build_agent_task(
    dispatch: &Dispatch,
    state: &ActorState,
    claim: &QueueClaim,
    attempt: &zeta_dispatch::Attempt,
) -> Result<AgentTask, RuntimeError> {
    let queue_item = dispatch
        .queue_item(claim.queue_item_id())
        .map_err(|error| RuntimeError::new(error.to_string()))?
        .ok_or_else(|| RuntimeError::new("a claimed queue item is absent"))?;
    let revision_id = queue_item
        .project_revision()
        .ok_or_else(|| RuntimeError::new("a claimed queue item has no project revision"))?;
    let revision = if revision_id == state.revision.revision_id() {
        state.revision.clone()
    } else {
        state
            .archive
            .load(revision_id)
            .map_err(|error| RuntimeError::new(error.to_string()))?
            .ok_or_else(|| {
                RuntimeError::new(format!(
                    "the queued project revision is unavailable: {revision_id}"
                ))
            })?
    };
    let agent = revision
        .agent(queue_item.target_agent())
        .cloned()
        .ok_or_else(|| {
            RuntimeError::new(format!(
                "the queued agent is absent from revision {revision_id}: {}",
                queue_item.target_agent()
            ))
        })?;
    let event = dispatch
        .get_event(queue_item.event_id())
        .map_err(|error| RuntimeError::new(error.to_string()))?
        .ok_or_else(|| RuntimeError::new("a claimed queue event is absent"))?;
    let session_id = attempt
        .session_id()
        .ok_or_else(|| RuntimeError::new("a claimed attempt has no session"))?
        .as_str()
        .to_owned();
    let run_id = attempt
        .run_id()
        .ok_or_else(|| RuntimeError::new("a claimed attempt has no run"))?
        .as_str()
        .to_owned();
    let retry_policy = retry_policy_for_agent(&agent.slug, &revision);
    Ok(AgentTask {
        queue_item_id: claim.queue_item_id().as_str().to_owned(),
        project_root: revision.project_root().to_path_buf(),
        project_revision_id: revision.revision_id().to_owned(),
        providers: revision.providers().clone(),
        agent,
        event,
        session_id,
        run_id,
        retry_policy,
        event_sink: None,
    })
}

fn commit_agent_execution(
    dispatch: &mut Dispatch,
    routes: &[Route],
    claim: &QueueClaim,
    execution: AgentExecution,
    retry_policy: RetryPolicy,
) -> Result<bool, RuntimeError> {
    let now_ms = current_time_ms()?;
    match execution {
        AgentExecution::Completed(completion) => {
            let mut identities = Vec::with_capacity(completion.controls().len() + 2);
            for _ in 0..completion.controls().len() + 2 {
                identities.push(runtime_identity(now_ms));
            }
            let events = dispatch
                .complete_claimed_attempt(claim, now_ms, &identities, &completion)
                .map_err(|error| RuntimeError::new(error.to_string()))?;
            let routed = route_unrouted(dispatch, routes)?;
            Ok(!events.is_empty() || routed > 0)
        }
        AgentExecution::Failed(error) => {
            fail_claimed_agent_with_code(dispatch, claim, retry_policy, error.detail, error.code)
        }
    }
}

fn fail_claimed_agent(
    dispatch: &mut Dispatch,
    claim: &QueueClaim,
    retry_policy: RetryPolicy,
    detail: String,
) -> Result<bool, RuntimeError> {
    fail_claimed_agent_with_code(
        dispatch,
        claim,
        retry_policy,
        detail,
        AttemptFailureCode::AgentExecutionFailed,
    )
}

fn fail_claimed_agent_with_code(
    dispatch: &mut Dispatch,
    claim: &QueueClaim,
    retry_policy: RetryPolicy,
    detail: String,
    code: AttemptFailureCode,
) -> Result<bool, RuntimeError> {
    let now_ms = current_time_ms()?;
    let failure = AttemptFailure::new(format_timestamp(now_ms)?, detail, code, retry_policy);
    let events = dispatch
        .fail_claimed_attempt(
            claim,
            now_ms,
            [runtime_identity(now_ms), runtime_identity(now_ms)],
            &failure,
        )
        .map_err(|error| RuntimeError::new(error.to_string()))?;
    Ok(!events.is_empty())
}

fn retry_policy_for_agent(agent_id: &str, revision: &ProjectRevision) -> RetryPolicy {
    let Some(retry) = revision
        .agent(agent_id)
        .and_then(|agent| agent.retry.as_ref())
    else {
        return RetryPolicy::default();
    };
    let defaults = RetryPolicy::default();
    let max_attempts = retry
        .max_attempts
        .and_then(|value| u32::try_from(value).ok())
        .unwrap_or_else(|| defaults.max_attempts());
    let backoff_seconds = retry.backoff_seconds.unwrap_or(5.0);
    RetryPolicy::new(max_attempts, backoff_seconds, 2.0, 300.0).unwrap_or(defaults)
}

fn renew_due_agents(dispatch: &mut Dispatch, state: &mut ActorState) -> Result<bool, RuntimeError> {
    let now_ms = current_time_ms()?;
    let mut changed = false;
    for active in state.active_agents.values_mut() {
        if active.heartbeat_at_ms > now_ms {
            continue;
        }
        let renewed = dispatch
            .renew_claim(&active.claim, AGENT_LEASE_MS, now_ms)
            .map_err(|error| RuntimeError::new(error.to_string()))?;
        if renewed {
            active.heartbeat_at_ms = now_ms.saturating_add(AGENT_HEARTBEAT_MS);
            changed = true;
        } else {
            let _cancelled = active
                .cancellation
                .cancel(zeta_agent::AbortReason::Cancelled);
        }
    }
    Ok(changed)
}

fn renew_due_egress(dispatch: &mut Dispatch, state: &mut ActorState) -> Result<bool, RuntimeError> {
    let now_ms = current_time_ms()?;
    let mut changed = false;
    for active in state.active_egress.values_mut() {
        if active.heartbeat_at_ms > now_ms {
            continue;
        }
        let renewed = dispatch
            .renew_egress_delivery_claim(&active.claim, EGRESS_LEASE_MS, now_ms)
            .map_err(|error| RuntimeError::new(error.to_string()))?;
        if renewed {
            active.heartbeat_at_ms = now_ms.saturating_add(EGRESS_HEARTBEAT_MS);
            changed = true;
        } else {
            let _cancelled = active
                .cancellation
                .cancel(zeta_agent::AbortReason::Cancelled);
        }
    }
    Ok(changed)
}

fn route_unrouted(dispatch: &mut Dispatch, routes: &[Route]) -> Result<usize, RuntimeError> {
    let event_ids = dispatch
        .unrouted_ingress_events()
        .map_err(|error| RuntimeError::new(error.to_string()))?;
    let mut route_count: usize = 0;
    for event_id in event_ids {
        let event = dispatch
            .get_event(&event_id)
            .map_err(|error| RuntimeError::new(error.to_string()))?
            .ok_or_else(|| RuntimeError::new("an unrouted event is absent from the journal"))?;
        let decisions =
            route_event(&event, routes).map_err(|error| RuntimeError::new(error.to_string()))?;
        let identity_count = if decisions.len() > 1 {
            decisions.len() + 1
        } else {
            1
        };
        let mut identities = Vec::with_capacity(identity_count);
        for _ in 0..identity_count {
            identities.push(runtime_identity(current_time_ms()?));
        }
        let outcome = dispatch
            .route_ingress_event(&event_id, routes, &identities)
            .map_err(|error| RuntimeError::new(error.to_string()))?;
        route_count = route_count.saturating_add(outcome.decisions().len());
    }
    Ok(route_count)
}

fn status(
    dispatch: &Dispatch,
    wake: &RuntimeWake,
    state: &ActorState,
) -> Result<RuntimeStatus, RuntimeError> {
    let now_ms = current_time_ms()?;
    let mut queue = BTreeMap::new();
    for item in dispatch
        .list_queue_items()
        .map_err(|error| RuntimeError::new(error.to_string()))?
    {
        *queue.entry(item.status().to_string()).or_insert(0) += 1;
    }
    let active_waits = dispatch
        .list_waits()
        .map_err(|error| RuntimeError::new(error.to_string()))?
        .into_iter()
        .filter(|wait| wait.status() == zeta_dispatch::WaitStatus::Active)
        .count();
    let pending_publications = dispatch
        .list_deferred_publications()
        .map_err(|error| RuntimeError::new(error.to_string()))?
        .into_iter()
        .filter(|publication| {
            publication.status() == zeta_dispatch::DeferredPublicationStatus::Pending
        })
        .count();
    let pending_egress = dispatch
        .list_effects()
        .map_err(|error| RuntimeError::new(error.to_string()))?
        .into_iter()
        .filter(|effect| {
            effect.operation().starts_with("connector:")
                && matches!(
                    effect.status(),
                    EffectStatus::Planned | EffectStatus::Failed
                )
                && effect
                    .available_at()
                    .is_some_and(|available_at| available_at <= now_ms)
        })
        .count();
    Ok(RuntimeStatus {
        wake_epoch: wake.epoch(),
        queue,
        active_agents: state.active_agents.len(),
        agent_capacity: AGENT_CAPACITY,
        active_egress: state.active_egress.len(),
        egress_capacity: EGRESS_CAPACITY,
        pending_egress,
        active_waits,
        pending_publications,
        next_deadline_ms: dispatch
            .next_deadline_ms(now_ms)
            .map_err(|error| RuntimeError::new(error.to_string()))?,
    })
}

fn current_time_ms() -> Result<i64, RuntimeError> {
    let duration = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| RuntimeError::new(error.to_string()))?;
    duration
        .as_millis()
        .try_into()
        .map_err(|_error| RuntimeError::new("the current time does not fit Unix milliseconds"))
}

fn format_timestamp(timestamp_ms: i64) -> Result<String, RuntimeError> {
    let timestamp = DateTime::<Utc>::from_timestamp_millis(timestamp_ms)
        .ok_or_else(|| RuntimeError::new("the current time is outside the RFC 3339 range"))?;
    Ok(timestamp.to_rfc3339_opts(SecondsFormat::Millis, true))
}

fn format_timestamp_now() -> String {
    Utc::now().to_rfc3339_opts(SecondsFormat::Millis, true)
}

fn next_event_id(prefix: &str) -> String {
    format!("{prefix}_{}", Uuid::new_v4())
}

fn runtime_identity(now_ms: i64) -> RuntimeEventIdentity {
    RuntimeEventIdentity::new(next_event_id("runtime"), now_ms)
        .expect("generated runtime identities are non-empty")
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::sync::{Arc, Barrier};

    use serde_json::Map;
    use tempfile::TempDir;

    use super::*;

    fn project(root: &Path) -> ProjectRevision {
        let agents = root.join("agents");
        fs::create_dir_all(&agents).expect("agents directory");
        fs::write(
            agents.join("worker.md"),
            "---\nname: Worker\ndescription: Routes events.\naccepts: [example.created]\n---\nWork.\n",
        )
        .expect("agent source");
        ProjectRevision::load(root).expect("project revision")
    }

    #[test]
    fn python_model_profile_maps_canonical_tool_names() {
        let agent = zeta_manifest::parse_agent(
            "worker",
            b"---\nname: Worker\ndescription: Uses a Python tool.\ntools: [web_search]\n---\nWork.\n",
        )
        .expect("agent source");
        let providers: PythonProviderCatalog = serde_json::from_value(serde_json::json!({
            "models": {},
            "tools": {"web_search": {
                "id": "web_search",
                "source": {"module": "test", "path": null, "distribution": null},
                "fingerprint": "a".repeat(64),
                "tool_profile": null,
                "input_schema": {"type": "object"},
                "output_schema": null
            }},
            "connectors": {}
        }))
        .expect("provider catalog");
        let profile = serde_json::json!({"web_search": "search"})
            .as_object()
            .expect("profile object")
            .clone();

        let capabilities = selected_python_capabilities(&agent, &providers, Some(&profile))
            .expect("capabilities resolve");

        assert_eq!(capabilities.len(), 1);
        assert_eq!(capabilities[0].canonical.id.as_str(), "web_search");
        assert_eq!(capabilities[0].model_name, "search");
    }

    #[test]
    fn parses_connector_subscription_events_and_cursor() {
        let batch = parse_subscription_batch(
            serde_json::json!({
                "events": [{"message_ts": "1", "text": "hello"}],
                "cursor": "cursor-1"
            })
            .as_object()
            .expect("subscription result object")
            .clone(),
        )
        .expect("subscription batch");

        assert_eq!(batch.events.len(), 1);
        assert_eq!(batch.events[0]["text"], "hello");
        assert_eq!(batch.cursor, Some(Value::String("cursor-1".to_owned())));
    }

    fn draft(id: usize) -> DraftEvent {
        DraftEvent {
            event_type: "example.created".to_owned(),
            source: "test".to_owned(),
            payload: Map::new(),
            idempotency_key: Some(format!("example:{id}")),
            caused_by: None,
            session_id: None,
            run_id: None,
            turn_id: None,
        }
    }

    fn completed_agent_execution() -> AgentExecution {
        AgentExecution::Completed(AttemptCompletion::new(
            "2026-08-13T00:00:00Z",
            zeta_dispatch::AttemptCompletionDisposition::Succeeded,
            Map::new(),
            Vec::new(),
        ))
    }

    fn publishing_agent_runner(
        _task: AgentTask,
        _cancellation: CancellationToken,
    ) -> AgentExecution {
        AgentExecution::Completed(AttemptCompletion::new(
            "2026-08-13T00:00:00Z",
            zeta_dispatch::AttemptCompletionDisposition::Succeeded,
            Map::new(),
            vec![zeta_dispatch::AttemptControl::publish(
                "publish-message",
                "message.send",
                serde_json::json!({"text": "hello"})
                    .as_object()
                    .expect("message payload")
                    .clone(),
                None,
                0,
            )],
        ))
    }

    #[test]
    fn ingress_wakes_and_routes_without_a_poll_interval() {
        let temporary = TempDir::new().expect("temporary directory");
        let runtime = Runtime::start_without_agents_for_test(
            temporary.path().join("zeta.sqlite3"),
            project(temporary.path()),
        )
        .expect("reactive runtime");
        let wake = runtime.subscribe();
        let result = runtime.ingest(draft(1)).expect("ingress");
        assert!(result.inserted);
        assert_eq!(result.route_count, 1);
        assert!(wake.has_changed().expect("ingress wake"));
        let status = runtime.status().expect("runtime status");
        assert_eq!(status.queue.get("available"), Some(&1));
        runtime.shutdown().expect("runtime shutdown");
    }

    #[test]
    fn agent_steps_receive_journal_identity_and_live_progress() {
        let temporary = TempDir::new().expect("temporary directory");
        let database = temporary.path().join("zeta.sqlite3");
        let (recorded_sender, recorded_receiver) = mpsc::channel();
        let trace_object = zeta_substrate::Object {
            kind: "test.agent_step".to_owned(),
            schema: "test.agent_step.v1".to_owned(),
            data: serde_json::from_value(serde_json::json!({"text": "Hello"}))
                .expect("trace data"),
            links: Vec::new(),
        };
        let trace_object_id = trace_object
            .content_address()
            .expect("trace address")
            .to_string();
        let trace = zeta_agent::TraceBatch {
            objects: vec![zeta_agent::AddressedObject {
                id: trace_object_id.clone(),
                object: trace_object,
            }],
            derivations: Vec::new(),
        };
        let runtime = Runtime::start_with_test_agent_runner(
            &database,
            project(temporary.path()),
            Arc::new(move |task, _cancellation| {
                let sink = task.event_sink.expect("the runtime injects an agent event sink");
                sink.record_trace(&trace)
                    .expect("the actor stores the agent trace");
                sink.observe(AgentProgress {
                    queue_item_id: task.queue_item_id.clone(),
                    agent_slug: task.agent.slug.clone(),
                    session_id: task.session_id.clone(),
                    run_id: task.run_id.clone(),
                    observation: zeta_agent::Observation::TextDelta {
                        text: "Hello".to_owned(),
                    },
                });
                let event_id = sink
                    .record(
                        "agent-model-1",
                        &DraftEvent {
                            event_type: "zeta.model_call.completed".to_owned(),
                            source: format!("agent:{}", task.agent.slug),
                            payload: Map::new(),
                            idempotency_key: Some("model:1".to_owned()),
                            caused_by: Some(task.event.id),
                            session_id: Some(task.session_id),
                            run_id: Some(task.run_id),
                            turn_id: None,
                        },
                    )
                    .expect("the actor stores the agent step");
                let _sent = recorded_sender.send(event_id);
                completed_agent_execution()
            }),
        )
        .expect("reactive runtime");
        let mut progress = runtime.subscribe_progress();

        runtime.ingest(draft(1)).expect("ingress");
        assert_eq!(
            recorded_receiver
                .recv_timeout(Duration::from_secs(5))
                .expect("recorded agent step"),
            "agent-model-1"
        );
        let update = tokio::runtime::Builder::new_current_thread()
            .enable_time()
            .build()
            .expect("progress runtime")
            .block_on(async {
                tokio::time::timeout(Duration::from_secs(5), progress.recv())
                    .await
                    .expect("agent progress arrives")
                    .expect("agent progress channel remains open")
            });
        assert_eq!(update.observation, zeta_agent::Observation::TextDelta {
            text: "Hello".to_owned()
        });

        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        while runtime.status().expect("runtime status").active_agents != 0 {
            assert!(std::time::Instant::now() < deadline, "agent completion timed out");
            thread::sleep(Duration::from_millis(10));
        }
        runtime.shutdown().expect("runtime shutdown");

        let dispatch = Dispatch::open(&database).expect("read dispatch journal");
        let events = dispatch
            .list_events(&EventFilter {
                event_type: Some("zeta.model_call.completed".to_owned()),
                ..EventFilter::default()
            })
            .expect("agent step events");
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].id, "agent-model-1");
        assert!(events[0].cursor.is_some());
        assert_eq!(events[0].idempotency_key.as_deref(), Some("model:1"));
        assert!(
            dispatch
                .trace_object(&trace_object_id)
                .expect("agent trace object")
                .is_some()
        );
    }

    #[test]
    fn agent_lane_runs_four_independent_agents_in_parallel() {
        let temporary = TempDir::new().expect("temporary directory");
        let barrier = Arc::new(Barrier::new(5));
        let runner_barrier = Arc::clone(&barrier);
        let runtime = Runtime::start_with_test_agent_runner(
            temporary.path().join("zeta.sqlite3"),
            project(temporary.path()),
            Arc::new(move |_task, _cancellation| {
                runner_barrier.wait();
                completed_agent_execution()
            }),
        )
        .expect("reactive runtime");
        for id in 0..4 {
            runtime.ingest(draft(id)).expect("ingress");
        }
        barrier.wait();
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        loop {
            let status = runtime.status().expect("runtime status");
            if status.queue.get("completed") == Some(&4) {
                break;
            }
            assert!(
                std::time::Instant::now() < deadline,
                "agent work must complete"
            );
            thread::sleep(Duration::from_millis(10));
        }
        runtime.shutdown().expect("runtime shutdown");
    }

    #[test]
    fn queued_work_uses_its_recorded_revision_after_restart() {
        let temporary = TempDir::new().expect("temporary directory");
        let database = temporary.path().join("zeta.sqlite3");
        let initial = project(temporary.path());
        let runtime =
            Runtime::start_without_agents_for_test(&database, initial).expect("reactive runtime");
        runtime.ingest(draft(1)).expect("ingress");
        fs::write(
            temporary.path().join("agents/worker.md"),
            "---\nname: Worker\ndescription: Routes events.\naccepts: [example.created]\n---\nReplacement work.\n",
        )
        .expect("replacement agent source");
        let replacement = ProjectRevision::load(temporary.path()).expect("replacement project");
        runtime.reload(replacement.clone()).expect("reload");
        runtime.shutdown().expect("runtime shutdown");

        let (sender, receiver) = mpsc::channel();
        let runtime = Runtime::start_with_test_agent_runner(
            &database,
            replacement,
            Arc::new(move |task, _cancellation| {
                let _sent = sender.send(task.agent.instructions);
                completed_agent_execution()
            }),
        )
        .expect("reactive runtime");
        let instructions = receiver
            .recv_timeout(Duration::from_secs(5))
            .expect("queued work must execute");
        assert!(instructions.contains("Work."));
        assert!(!instructions.contains("Replacement work."));
        runtime.shutdown().expect("runtime shutdown");
    }

    #[test]
    fn egress_lane_delivers_a_published_event_after_its_effect_starts() {
        let temporary = TempDir::new().expect("temporary directory");
        let agents = temporary.path().join("agents");
        fs::create_dir(&agents).expect("agents directory");
        fs::write(
            agents.join("worker.md"),
            "---\nname: Worker\ndescription: Routes events.\naccepts: [example.created]\npublishes:\n  - event: message.send\n    with: {channel: test}\n    idempotency_key: 'message:{id}'\n---\nWork.\n",
        )
        .expect("agent source");
        let revision = ProjectRevision::load(temporary.path()).expect("project revision");
        let (sender, receiver) = mpsc::channel();
        let connector = EgressTarget::new(
            "messages",
            "message.send",
            EffectDeliverySemantics::IdempotentWithKey,
        )
        .expect("connector egress");
        let runtime = Runtime::start_with_test_agent_and_egress_runners(
            temporary.path().join("zeta.sqlite3"),
            revision,
            Arc::new(publishing_agent_runner),
            Arc::new(move |task, _cancellation| {
                let _sent = sender.send(task);
                EgressExecution::Completed(Map::new())
            }),
            BTreeMap::from([("message.send".to_owned(), connector)]),
        )
        .expect("reactive runtime");
        let _ingress = runtime.ingest(draft(1)).expect("ingress");
        let task = match receiver.recv_timeout(Duration::from_secs(5)) {
            Ok(task) => task,
            Err(error) => panic!(
                "connector delivery: {error}; status: {:?}",
                runtime.status().expect("runtime status")
            ),
        };
        assert_eq!(task.connector_id, "messages");
        assert_eq!(task.operation, "message.send");
        assert_eq!(task.payload["text"], "hello");
        assert_eq!(task.options["channel"], "test");
        assert!(task.idempotency_key.starts_with("message:runtime_"));
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        loop {
            let status = runtime.status().expect("runtime status");
            if status.pending_egress == 0 && status.active_egress == 0 {
                break;
            }
            assert!(
                std::time::Instant::now() < deadline,
                "connector work must complete"
            );
            thread::sleep(Duration::from_millis(10));
        }
        runtime.shutdown().expect("runtime shutdown");
    }

    #[test]
    fn egress_status_reports_active_and_ready_deliveries() {
        let temporary = TempDir::new().expect("temporary directory");
        let agents = temporary.path().join("agents");
        fs::create_dir(&agents).expect("agents directory");
        fs::write(
            agents.join("worker.md"),
            "---\nname: Worker\ndescription: Routes events.\naccepts: [example.created]\npublishes:\n  - event: message.send\n    with: {channel: test}\n---\nWork.\n",
        )
        .expect("agent source");
        let revision = ProjectRevision::load(temporary.path()).expect("project revision");
        let (task_sender, task_receiver) = mpsc::channel();
        let (release_sender, release_receiver) = mpsc::channel();
        let release_receiver = Arc::new(Mutex::new(release_receiver));
        let connector = EgressTarget::new(
            "messages",
            "message.send",
            EffectDeliverySemantics::IdempotentWithKey,
        )
        .expect("connector egress");
        let runtime = Runtime::start_with_test_agent_and_egress_runners(
            temporary.path().join("zeta.sqlite3"),
            revision,
            Arc::new(publishing_agent_runner),
            Arc::new({
                let release_receiver = Arc::clone(&release_receiver);
                move |task, _cancellation| {
                    let _sent = task_sender.send(task);
                    let release = release_receiver
                        .lock()
                        .expect("egress release state")
                        .recv();
                    if release.is_err() {
                        return EgressExecution::Failed("the egress test stopped".to_owned());
                    }
                    EgressExecution::Completed(Map::new())
                }
            }),
            BTreeMap::from([("message.send".to_owned(), connector)]),
        )
        .expect("reactive runtime");
        for id in 0..5 {
            runtime.ingest(draft(id)).expect("ingress");
        }
        for _ in 0..4 {
            task_receiver
                .recv_timeout(Duration::from_secs(5))
                .expect("active connector delivery");
        }
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        loop {
            let status = runtime.status().expect("runtime status");
            if status.active_egress == 4 && status.pending_egress == 1 {
                break;
            }
            assert!(
                std::time::Instant::now() < deadline,
                "egress status must show four active and one ready delivery: {status:?}"
            );
            thread::sleep(Duration::from_millis(10));
        }
        for _ in 0..5 {
            release_sender.send(()).expect("release connector delivery");
        }
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        loop {
            let status = runtime.status().expect("runtime status");
            if status.active_egress == 0 && status.pending_egress == 0 {
                break;
            }
            assert!(
                std::time::Instant::now() < deadline,
                "connector deliveries must complete: {status:?}"
            );
            thread::sleep(Duration::from_millis(10));
        }
        runtime.shutdown().expect("runtime shutdown");
    }

    #[test]
    fn native_executor_completes_a_direct_model_agent() {
        let temporary = TempDir::new().expect("temporary directory");
        let listener = TcpListener::bind("127.0.0.1:0").expect("model listener");
        let address = listener.local_addr().expect("model address");
        let server = thread::spawn(move || {
            let (mut socket, _address) = listener.accept().expect("model connection");
            let mut request = [0_u8; 4096];
            let count = socket.read(&mut request).expect("model request");
            assert!(count > 0, "the model must receive a request");
            let body = format!(
                "data: {}\n\ndata: [DONE]\n\n",
                serde_json::json!({
                    "id": "native-agent-answer",
                    "choices": [{
                        "index": 0,
                        "delta": {"role": "assistant", "content": "Done."},
                        "finish_reason": "stop",
                    }],
                })
            );
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
            socket
                .write_all(response.as_bytes())
                .expect("model response");
        });
        let agents = temporary.path().join("agents");
        fs::create_dir(&agents).expect("agents directory");
        fs::write(
            agents.join("worker.md"),
            "---\nname: Worker\ndescription: Routes events.\naccepts: [example.created]\nmodel: unit-model\n---\nWork.\n",
        )
        .expect("agent source");
        fs::write(
            temporary.path().join("zeta.toml"),
            format!(
                "[[models]]\nname = \"unit-model\"\nmodel = \"unit-model\"\nurl = \"http://{address}/v1/chat/completions\"\n"
            ),
        )
        .expect("project model configuration");
        let revision = ProjectRevision::load(temporary.path()).expect("project revision");
        let task = AgentTask {
            queue_item_id: "queue-1".to_owned(),
            project_root: temporary.path().to_path_buf(),
            project_revision_id: revision.revision_id().to_owned(),
            providers: revision.providers().clone(),
            agent: revision.agent("worker").expect("worker").clone(),
            event: Event {
                id: "event-1".to_owned(),
                event_type: "example.created".to_owned(),
                source: "test".to_owned(),
                payload: Map::new(),
                idempotency_key: None,
                caused_by: None,
                session_id: None,
                run_id: None,
                turn_id: None,
                timestamp_ms: 1,
                cursor: None,
            },
            session_id: "agent/worker/event-1".to_owned(),
            run_id: "run-1".to_owned(),
            retry_policy: RetryPolicy::default(),
            event_sink: None,
        };
        let execution = AgentExecutor::default().execute(task, CancellationToken::new());
        let AgentExecution::Completed(completion) = execution else {
            panic!("the native model agent must complete")
        };
        assert_eq!(completion.metadata()["final_answer"], "Done.");
        server.join().expect("model server");
    }
}
