//! Reactively routes durable native runtime ingress and agent work.

use std::collections::BTreeMap;
use std::fmt;
use std::panic::{self, AssertUnwindSafe};
use std::path::{Path, PathBuf};
use std::sync::{mpsc, Arc, Mutex};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use chrono::{DateTime, SecondsFormat, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use tokio::sync::watch;
use uuid::Uuid;
use zeta_dispatch::{
    effect_key, route_event, AttemptCompletion, AttemptFailure, AttemptFailureCode, ClaimToken,
    Dispatch, Effect, EffectDeliverySemantics, EffectStatus, EgressDeliveryClaim, QueueClaim,
    RetryPolicy, Route, RuntimeEventIdentity,
};
use zeta_journal::{DraftEvent, Event, EventFilter};
use zeta_manifest::AgentSpec;

use crate::{
    attempt_completion, CallbackDraftRecorder, CallbackObserver, CancellationToken,
    ProjectRevision, ProjectRevisionStore, SystemClock, UuidIdSource,
};

const DUE_ADVANCE_LIMIT: usize = 128;
const AGENT_CAPACITY: usize = 4;
const AGENT_LEASE_MS: u64 = 60_000;
const AGENT_HEARTBEAT_MS: i64 = 15_000;
const AGENT_WORKER_NAME: &str = "native-agent";
const EGRESS_CAPACITY: usize = 4;
const EGRESS_LEASE_MS: u64 = 60_000;
const EGRESS_HEARTBEAT_MS: i64 = 15_000;
const EGRESS_WORKER_NAME: &str = "native-egress";

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
pub struct AgentTask {
    /// Identifies the durable queue item.
    pub queue_item_id: String,
    /// Identifies the fenced durable attempt.
    pub attempt_id: String,
    /// Identifies the selected immutable project revision.
    pub project_revision: String,
    /// Supplies the project directory from that revision.
    pub project_root: PathBuf,
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
}

/// Reports one agent-execution failure before Dispatch selects a retry.
#[derive(Clone, Debug)]
pub struct AgentExecutionError {
    detail: String,
    code: AttemptFailureCode,
}

impl AgentExecutionError {
    /// Creates a classified execution failure.
    pub fn new(detail: impl Into<String>, code: AttemptFailureCode) -> Self {
        Self {
            detail: detail.into(),
            code,
        }
    }

    /// Returns the stable failure text.
    pub fn detail(&self) -> &str {
        &self.detail
    }

    /// Returns the durable failure class.
    pub fn code(&self) -> AttemptFailureCode {
        self.code
    }
}

/// Returns a terminal proposal from one external agent execution.
#[derive(Clone, Debug)]
pub enum AgentExecution {
    /// Carries a validated successful attempt proposal.
    Completed(AttemptCompletion),
    /// Carries a classified failure proposal.
    Failed(AgentExecutionError),
}

/// Runs one claimed agent outside the Dispatch actor.
pub trait AgentExecutor: Send + Sync + 'static {
    /// Executes one task and must observe the supplied cancellation token.
    fn execute(&self, task: AgentTask, cancellation: CancellationToken) -> AgentExecution;
}

/// Describes one connector operation that the native host can deliver.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConnectorEgress {
    connector_id: String,
    operation: String,
    semantics: EffectDeliverySemantics,
}

impl ConnectorEgress {
    /// Creates one connector operation for a published event type.
    pub fn new(
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

    /// Returns the connector identity.
    pub fn connector_id(&self) -> &str {
        &self.connector_id
    }

    /// Returns the connector method.
    pub fn operation(&self) -> &str {
        &self.operation
    }

    /// Returns the declared retry contract.
    pub fn semantics(&self) -> EffectDeliverySemantics {
        self.semantics
    }
}

/// Carries one claimed connector delivery outside the Dispatch actor.
#[derive(Clone, Debug)]
pub struct EgressTask {
    /// Identifies the retry-stable external effect.
    pub effect_key: String,
    /// Identifies the connector child.
    pub connector_id: String,
    /// Names the connector method.
    pub operation: String,
    /// Carries the published event payload.
    pub payload: Map<String, Value>,
    /// Carries the authored connector options.
    pub options: Map<String, Value>,
    /// Carries the stable connector idempotency key.
    pub idempotency_key: String,
    /// Selects the durable retry contract.
    pub semantics: EffectDeliverySemantics,
}

/// Reports the terminal result of one connector call.
#[derive(Clone, Debug)]
pub enum EgressExecution {
    /// Carries a connector result object.
    Completed(Map<String, Value>),
    /// Carries a retry-safe connector failure message.
    Failed(String),
}

/// Delivers one claimed connector operation outside the Dispatch actor.
pub trait EgressExecutor: Send + Sync + 'static {
    /// Calls a connector and must observe the supplied cancellation token.
    fn execute(&self, task: EgressTask, cancellation: CancellationToken) -> EgressExecution;
}

/// Runs direct-model agents with no external capability grants.
///
/// This executor supports an agent-level OpenAI-compatible model declaration.
/// It preserves the durable attempt boundary before native tool execution is
/// enabled in a later lane.
#[derive(Clone, Copy, Debug, Default)]
pub struct NativeAgentExecutor;

impl AgentExecutor for NativeAgentExecutor {
    fn execute(&self, task: AgentTask, cancellation: CancellationToken) -> AgentExecution {
        let Some(model) = &task.agent.model else {
            return AgentExecution::Failed(AgentExecutionError::new(
                format!("agent {:?} has no model declaration", task.agent.slug),
                AttemptFailureCode::AgentExecutionFailed,
            ));
        };
        let Some(project_root) = task.project_root.to_str() else {
            return AgentExecution::Failed(AgentExecutionError::new(
                "the project directory is not valid UTF-8",
                AttemptFailureCode::AgentExecutionFailed,
            ));
        };
        let endpoint = zeta_agent::ModelHttpEndpoint::new(&model.url);
        let gateway = zeta_agent::HttpModelGateway::new(zeta_agent::HttpModelGatewayConfig::new(
            Some(endpoint),
            None,
        ));
        let Ok(mut gateway) = gateway else {
            return AgentExecution::Failed(AgentExecutionError::new(
                "the native model gateway configuration is invalid",
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
        let invocation = zeta_agent::AgentInvocation {
            objective: format!("Handle the event {}.", task.event.event_type),
            timeline: vec![timeline_event],
            context,
            system_prompt: Some(task.agent.instructions.clone()),
            allowed_capabilities: Vec::new(),
            tool_profile: zeta_agent::ToolProfile::Native,
            max_model_calls: 25,
            model_name: Some(model.name.clone()),
            model_url: Some(model.url.clone()),
            model_api: Some("chat-completions".to_owned()),
            thinking: None,
            model_session_id: Some(task.session_id.clone()),
            max_tokens: 8_192,
            tool_choice: Value::String("auto".to_owned()),
            base_directory: Some(project_root.to_owned()),
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
                working_directory: project_root.to_owned(),
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
        let mut executor = zeta_agent::NativeToolExecutor::default();
        let mut observer = CallbackObserver::new(|_observation: zeta_agent::Observation| {});
        let mut recorder = CallbackDraftRecorder::new(|_draft: &DraftEvent| Ok::<(), String>(()));
        let mut ids = UuidIdSource::new("agent");
        let clock = SystemClock;
        let result = runtime.block_on(
            zeta_agent::AgentRunner::new(
                &[],
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
    thread: Mutex<Option<thread::JoinHandle<()>>>,
}

impl Runtime {
    /// Opens a durable Dispatch actor without an attached agent executor.
    ///
    /// Ingress, routing, and recovery remain active. Use
    /// [`Runtime::start_with_agent_executor`] to execute queued work.
    pub fn start(
        database_path: impl AsRef<Path>,
        revision: ProjectRevision,
    ) -> Result<Self, RuntimeError> {
        Self::start_inner(database_path.as_ref(), revision, None, None)
    }

    /// Opens a durable Dispatch actor with four parallel agent slots.
    pub fn start_with_agent_executor(
        database_path: impl AsRef<Path>,
        revision: ProjectRevision,
        executor: Arc<dyn AgentExecutor>,
    ) -> Result<Self, RuntimeError> {
        Self::start_inner(database_path.as_ref(), revision, Some(executor), None)
    }

    /// Opens a durable Dispatch actor with agent and connector worker lanes.
    ///
    /// Each map entry binds one published event type to one connector method.
    /// The project revision still decides which enabled agent may publish it.
    pub fn start_with_executors(
        database_path: impl AsRef<Path>,
        revision: ProjectRevision,
        agent_executor: Arc<dyn AgentExecutor>,
        egress_executor: Arc<dyn EgressExecutor>,
        connector_egress: BTreeMap<String, ConnectorEgress>,
    ) -> Result<Self, RuntimeError> {
        Self::start_inner(
            database_path.as_ref(),
            revision,
            Some(agent_executor),
            Some(EgressServices {
                executor: egress_executor,
                connector_egress,
            }),
        )
    }

    fn start_inner(
        database_path: &Path,
        revision: ProjectRevision,
        executor: Option<Arc<dyn AgentExecutor>>,
        egress: Option<EgressServices>,
    ) -> Result<Self, RuntimeError> {
        let routes = revision
            .routes()
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
        let (sender, receiver) = mpsc::channel();
        let (wake, _initial_receiver) = RuntimeWake::new();
        let actor_wake = wake.clone();
        let actor_sender = sender.clone();
        let thread = thread::Builder::new()
            .name("zeta-dispatch".to_owned())
            .spawn(move || {
                run_actor(
                    dispatch,
                    revision,
                    routes,
                    archive,
                    receiver,
                    actor_sender,
                    actor_wake,
                    executor,
                    egress,
                )
            })
            .map_err(|error| RuntimeError::new(format!("cannot start dispatch actor: {error}")))?;
        Ok(Self {
            sender,
            wake,
            thread: Mutex::new(Some(thread)),
        })
    }

    /// Returns a receiver for every post-commit work notification.
    pub fn subscribe(&self) -> watch::Receiver<u64> {
        self.wake.subscribe()
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
    Shutdown {
        reply: mpsc::Sender<Result<(), RuntimeError>>,
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
    archive: ProjectRevisionStore,
    active_agents: BTreeMap<String, ActiveAgent>,
    active_egress: BTreeMap<String, ActiveEgress>,
    egress: Option<EgressServices>,
}

struct ActiveAgent {
    claim: QueueClaim,
    cancellation: CancellationToken,
    heartbeat_at_ms: i64,
    thread: thread::JoinHandle<()>,
}

struct EgressServices {
    executor: Arc<dyn EgressExecutor>,
    connector_egress: BTreeMap<String, ConnectorEgress>,
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
    archive: ProjectRevisionStore,
    receiver: mpsc::Receiver<RuntimeCommand>,
    sender: mpsc::Sender<RuntimeCommand>,
    wake: RuntimeWake,
    executor: Option<Arc<dyn AgentExecutor>>,
    egress: Option<EgressServices>,
) {
    let mut state = ActorState {
        revision,
        routes,
        archive,
        active_agents: BTreeMap::new(),
        active_egress: BTreeMap::new(),
        egress,
    };
    if refresh_actor_state(&mut dispatch, &mut state, &sender, executor.as_ref()).unwrap_or(false) {
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
                    && refresh_actor_state(&mut dispatch, &mut state, &sender, executor.as_ref())
                        .unwrap_or(false);
                if result.is_ok() || changed {
                    wake.signal();
                }
                let _sent = reply.send(result);
            }
            Received::Command(RuntimeCommand::Reload { revision, reply }) => {
                let result = reload_actor(&mut state, revision).and_then(|()| {
                    refresh_actor_state(&mut dispatch, &mut state, &sender, executor.as_ref())
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
                    let refreshed =
                        refresh_actor_state(&mut dispatch, &mut state, &sender, executor.as_ref())?;
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
                    let refreshed =
                        refresh_actor_state(&mut dispatch, &mut state, &sender, executor.as_ref())?;
                    Ok(changed || refreshed)
                })
                .unwrap_or(false);
                if changed {
                    wake.signal();
                }
            }
            Received::Deadline => {
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
                            executor.as_ref(),
                        )?;
                        Ok(renewed || refreshed)
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
    Ok([durable, egress, agent_heartbeat, egress_heartbeat]
        .into_iter()
        .flatten()
        .min())
}

fn reload_actor(state: &mut ActorState, revision: ProjectRevision) -> Result<(), RuntimeError> {
    let routes = revision
        .routes()
        .map_err(|error| RuntimeError::new(error.to_string()))?;
    state
        .archive
        .record(&revision)
        .map_err(|error| RuntimeError::new(error.to_string()))?;
    state.revision = revision;
    state.routes = routes;
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

fn refresh_actor_state(
    dispatch: &mut Dispatch,
    state: &mut ActorState,
    sender: &mpsc::Sender<RuntimeCommand>,
    executor: Option<&Arc<dyn AgentExecutor>>,
) -> Result<bool, RuntimeError> {
    let advanced = advance_actor_state(dispatch, &state.routes)?;
    let planned = plan_pending_egress(dispatch, state)?;
    let claimed_agents = fill_agent_lane(dispatch, state, sender, executor)?;
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
    executor: Option<&Arc<dyn AgentExecutor>>,
) -> Result<bool, RuntimeError> {
    let Some(executor) = executor else {
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
        let task = match build_agent_task(dispatch, state, &claim, started.attempt()) {
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
        let cancellation = CancellationToken::new();
        let worker_cancellation = cancellation.clone();
        let worker_executor = Arc::clone(executor);
        let worker_sender = sender.clone();
        let worker_claim = claim.clone();
        let retry_policy = task.retry_policy;
        let thread = match thread::Builder::new()
            .name(format!("zeta-agent-{}", task.agent.slug))
            .spawn(move || {
                let execution = match panic::catch_unwind(AssertUnwindSafe(|| {
                    worker_executor.execute(task, worker_cancellation)
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
        let Some(connector) = services.connector_egress.get(&binding.event) else {
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
        let task = match build_egress_task(&effect) {
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
        let worker_executor = Arc::clone(&services.executor);
        let worker_sender = sender.clone();
        let worker_claim = claim.clone();
        let worker_effect = effect.clone();
        let retry_policy = RetryPolicy::default();
        let thread = match thread::Builder::new()
            .name(format!("zeta-egress-{}", task.connector_id))
            .spawn(move || {
                let execution = match panic::catch_unwind(AssertUnwindSafe(|| {
                    worker_executor.execute(task, worker_cancellation)
                })) {
                    Ok(execution) => execution,
                    Err(_panic) => {
                        EgressExecution::Failed("the native connector executor panicked".to_owned())
                    }
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

fn build_egress_task(effect: &Effect) -> Result<EgressTask, RuntimeError> {
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
    Ok(EgressTask {
        effect_key: effect.key().to_owned(),
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
        attempt_id: attempt.id().as_str().to_owned(),
        project_revision: revision_id.to_owned(),
        project_root: revision.project_root().to_path_buf(),
        agent,
        event,
        session_id,
        run_id,
        retry_policy,
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

    struct BarrierExecutor {
        barrier: Arc<Barrier>,
    }

    impl AgentExecutor for BarrierExecutor {
        fn execute(&self, _task: AgentTask, _cancellation: CancellationToken) -> AgentExecution {
            self.barrier.wait();
            AgentExecution::Completed(AttemptCompletion::new(
                "2026-08-13T00:00:00Z",
                zeta_dispatch::AttemptCompletionDisposition::Succeeded,
                Map::new(),
                Vec::new(),
            ))
        }
    }

    struct CaptureExecutor {
        instructions: mpsc::Sender<String>,
    }

    struct PublishingExecutor;

    impl AgentExecutor for PublishingExecutor {
        fn execute(&self, _task: AgentTask, _cancellation: CancellationToken) -> AgentExecution {
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
    }

    struct CaptureEgressExecutor {
        tasks: mpsc::Sender<EgressTask>,
    }

    impl EgressExecutor for CaptureEgressExecutor {
        fn execute(&self, task: EgressTask, _cancellation: CancellationToken) -> EgressExecution {
            let _sent = self.tasks.send(task);
            EgressExecution::Completed(Map::new())
        }
    }

    struct BlockingEgressExecutor {
        tasks: mpsc::Sender<EgressTask>,
        releases: Mutex<mpsc::Receiver<()>>,
    }

    impl EgressExecutor for BlockingEgressExecutor {
        fn execute(&self, task: EgressTask, _cancellation: CancellationToken) -> EgressExecution {
            let _sent = self.tasks.send(task);
            let release = self.releases.lock().expect("egress release state").recv();
            if release.is_err() {
                return EgressExecution::Failed("the egress test stopped".to_owned());
            }
            EgressExecution::Completed(Map::new())
        }
    }

    impl AgentExecutor for CaptureExecutor {
        fn execute(&self, task: AgentTask, _cancellation: CancellationToken) -> AgentExecution {
            let _sent = self.instructions.send(task.agent.instructions);
            AgentExecution::Completed(AttemptCompletion::new(
                "2026-08-13T00:00:00Z",
                zeta_dispatch::AttemptCompletionDisposition::Succeeded,
                Map::new(),
                Vec::new(),
            ))
        }
    }

    #[test]
    fn ingress_wakes_and_routes_without_a_poll_interval() {
        let temporary = TempDir::new().expect("temporary directory");
        let runtime = Runtime::start(
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
    fn agent_lane_runs_four_independent_agents_in_parallel() {
        let temporary = TempDir::new().expect("temporary directory");
        let barrier = Arc::new(Barrier::new(5));
        let runtime = Runtime::start_with_agent_executor(
            temporary.path().join("zeta.sqlite3"),
            project(temporary.path()),
            Arc::new(BarrierExecutor {
                barrier: Arc::clone(&barrier),
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
        let runtime = Runtime::start(&database, initial).expect("reactive runtime");
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
        let runtime = Runtime::start_with_agent_executor(
            &database,
            replacement,
            Arc::new(CaptureExecutor {
                instructions: sender,
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
        let connector = ConnectorEgress::new(
            "messages",
            "message.send",
            EffectDeliverySemantics::IdempotentWithKey,
        )
        .expect("connector egress");
        let runtime = Runtime::start_with_executors(
            temporary.path().join("zeta.sqlite3"),
            revision,
            Arc::new(PublishingExecutor),
            Arc::new(CaptureEgressExecutor { tasks: sender }),
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
        let connector = ConnectorEgress::new(
            "messages",
            "message.send",
            EffectDeliverySemantics::IdempotentWithKey,
        )
        .expect("connector egress");
        let runtime = Runtime::start_with_executors(
            temporary.path().join("zeta.sqlite3"),
            revision,
            Arc::new(PublishingExecutor),
            Arc::new(BlockingEgressExecutor {
                tasks: task_sender,
                releases: Mutex::new(release_receiver),
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
            format!(
                "---\nname: Worker\ndescription: Routes events.\naccepts: [example.created]\nmodel:\n  name: unit-model\n  url: http://{address}/v1/chat/completions\n---\nWork.\n"
            ),
        )
        .expect("agent source");
        let revision = ProjectRevision::load(temporary.path()).expect("project revision");
        let task = AgentTask {
            queue_item_id: "queue-1".to_owned(),
            attempt_id: "attempt-1".to_owned(),
            project_revision: revision.revision_id().to_owned(),
            project_root: temporary.path().to_path_buf(),
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
        };
        let execution = NativeAgentExecutor.execute(task, CancellationToken::new());
        let AgentExecution::Completed(completion) = execution else {
            panic!("the native model agent must complete")
        };
        assert_eq!(completion.metadata()["final_answer"], "Done.");
        server.join().expect("model server");
    }
}
