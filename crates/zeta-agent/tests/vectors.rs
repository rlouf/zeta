//! Shared Python and Rust agent conformance vectors.

use std::cell::{Cell, RefCell};
use std::collections::{HashMap, VecDeque};
use std::fs;
use std::future::Future;
use std::path::Path;
use std::pin::{pin, Pin};
use std::rc::Rc;
use std::sync::Arc;
use std::task::{Context, Poll, Wake, Waker};

use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use zeta_agent::{
    build_prompt, resolve_capabilities, AbortReason, AbortSignal, AgentErrorKind, AgentInvocation,
    AgentObserver, AgentProposal, AgentRunError, AgentRunResult, AgentRunner, ArgumentAdapter,
    Capability, CapabilityExecutor, CapabilityInvocation, Clock, DeliverySemantics, DraftRecorder,
    IdSource, ModelGateway, ModelInput, ModelOutput, ModelRequest, Observation, PromptEnvironment,
    PromptInput, PromptTransform, RunStopReason, StepName, ToolProfile,
};

#[derive(Deserialize)]
struct PromptVectors {
    version: u64,
    environment: PromptEnvironment,
    cases: Vec<PromptCase>,
}

#[derive(Deserialize)]
struct PromptCase {
    name: String,
    input: PromptInput,
    expected: Value,
}

#[derive(Deserialize)]
struct InvocationVectors {
    version: u64,
    environment: PromptEnvironment,
    cases: Vec<InvocationCase>,
}

#[derive(Deserialize)]
struct InvocationCase {
    name: String,
    invocation: Value,
    capabilities: Vec<Capability>,
    model_script: Vec<ModelScriptTurn>,
    tool_results: HashMap<String, VecDeque<Map<String, Value>>>,
    event_ids: VecDeque<String>,
    cancelled: bool,
    #[serde(default)]
    capture_recorded_events: bool,
    expected: Value,
}

#[derive(Clone, Deserialize)]
struct ModelScriptTurn {
    message: Map<String, Value>,
    stream: Vec<ScriptedObservation>,
    telemetry: Map<String, Value>,
    #[serde(default)]
    cancel_after_return: bool,
}

#[derive(Clone, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
enum ScriptedObservation {
    Content { text: String },
    Reasoning { text: String },
}

#[derive(Default)]
struct ScriptedGateway {
    script: VecDeque<ModelScriptTurn>,
    inputs: Vec<ModelInput>,
    cancellation: Option<Rc<Cell<Option<AbortReason>>>>,
}

impl ModelGateway for ScriptedGateway {
    fn generate<'a>(
        &'a mut self,
        input: &'a ModelInput,
        _request: &'a ModelRequest,
        observer: &'a mut dyn AgentObserver,
        _abort: &'a dyn AbortSignal,
    ) -> Pin<Box<dyn Future<Output = Result<ModelOutput, zeta_agent::AgentError>> + 'a>> {
        Box::pin(async move {
            self.inputs.push(input.clone());
            let Some(turn) = self.script.pop_front() else {
                return Err(zeta_agent::AgentError::model(
                    "scripted model response is missing",
                ));
            };
            let mut streamed_content = false;
            for observation in turn.stream {
                match observation {
                    ScriptedObservation::Content { text } => {
                        streamed_content = true;
                        observer.observe(Observation::TextDelta { text });
                    }
                    ScriptedObservation::Reasoning { text } => {
                        observer.observe(Observation::ReasoningDelta { text });
                    }
                }
            }
            if turn.cancel_after_return {
                if let Some(cancellation) = &self.cancellation {
                    cancellation.set(Some(AbortReason::Cancelled));
                }
            }
            Ok(ModelOutput {
                message: turn.message,
                telemetry: turn.telemetry,
                streamed_content,
            })
        })
    }
}

#[derive(Default)]
struct ScriptedExecutor {
    results: HashMap<String, VecDeque<Map<String, Value>>>,
    calls: Vec<RecordedInvocation>,
    recorded_drafts: Rc<RefCell<Vec<zeta_journal::DraftEvent>>>,
    capture_recorded_events: bool,
}

#[derive(Clone, Serialize)]
struct RecordedInvocation {
    capability_id: zeta_agent::CapabilityId,
    params: Map<String, Value>,
    base_directory: Option<String>,
    effect_key: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    recorded_event_types: Option<Vec<String>>,
}

impl CapabilityExecutor for ScriptedExecutor {
    fn execute<'a>(
        &'a mut self,
        invocation: &'a CapabilityInvocation,
        _abort: &'a dyn AbortSignal,
    ) -> Pin<Box<dyn Future<Output = Result<Map<String, Value>, zeta_agent::AgentError>> + 'a>>
    {
        Box::pin(async move {
            let recorded_event_types = if self.capture_recorded_events {
                let mut event_types = Vec::new();
                for draft in self.recorded_drafts.borrow().iter() {
                    event_types.push(draft.event_type.clone());
                }
                Some(event_types)
            } else {
                None
            };
            self.calls.push(RecordedInvocation {
                capability_id: invocation.capability_id.clone(),
                params: invocation.params.clone(),
                base_directory: invocation.base_directory.clone(),
                effect_key: invocation.effect_key.clone(),
                recorded_event_types,
            });
            let id = invocation.capability_id.as_str();
            let Some(results) = self.results.get_mut(id) else {
                return Err(zeta_agent::AgentError::tool(
                    "scripted capability result is missing",
                ));
            };
            let Some(result) = results.pop_front() else {
                return Err(zeta_agent::AgentError::tool(
                    "scripted capability result is exhausted",
                ));
            };
            Ok(result)
        })
    }
}

struct AbortReturningExecutor {
    trigger: Option<Rc<Cell<Option<AbortReason>>>>,
    observed: Option<AbortReason>,
}

impl CapabilityExecutor for AbortReturningExecutor {
    fn execute<'a>(
        &'a mut self,
        _invocation: &'a CapabilityInvocation,
        abort: &'a dyn AbortSignal,
    ) -> Pin<Box<dyn Future<Output = Result<Map<String, Value>, zeta_agent::AgentError>> + 'a>>
    {
        Box::pin(async move {
            if let Some(trigger) = &self.trigger {
                trigger.set(Some(AbortReason::Cancelled));
            }
            self.observed = abort.reason();
            Err(zeta_agent::AgentError::tool("tool execution stopped"))
        })
    }
}

#[derive(Default)]
struct RecordingObserver {
    observations: Vec<Observation>,
}

impl AgentObserver for RecordingObserver {
    fn observe(&mut self, observation: Observation) {
        self.observations.push(observation);
    }
}

struct RecordingDrafts {
    events: Rc<RefCell<Vec<zeta_journal::DraftEvent>>>,
}

struct RejectingDrafts {
    event_type: &'static str,
}

impl DraftRecorder for RejectingDrafts {
    fn record(
        &mut self,
        event_id: &str,
        event: &zeta_journal::DraftEvent,
    ) -> Result<String, zeta_agent::AgentError> {
        if event.event_type == self.event_type {
            return Err(zeta_agent::AgentError::durability(
                "durable event sink rejected the draft",
            ));
        }
        Ok(event_id.to_owned())
    }
}

impl Default for RecordingDrafts {
    fn default() -> Self {
        RecordingDrafts {
            events: Rc::new(RefCell::new(Vec::new())),
        }
    }
}

impl DraftRecorder for RecordingDrafts {
    fn record(
        &mut self,
        event_id: &str,
        event: &zeta_journal::DraftEvent,
    ) -> Result<String, zeta_agent::AgentError> {
        self.events.borrow_mut().push(event.clone());
        Ok(event_id.to_owned())
    }
}

struct ScriptedIds {
    values: VecDeque<String>,
}

impl IdSource for ScriptedIds {
    fn next_id(&mut self) -> Result<String, zeta_agent::AgentError> {
        let Some(id) = self.values.pop_front() else {
            return Err(zeta_agent::AgentError::identity(
                "scripted event id is missing",
            ));
        };
        Ok(id)
    }
}

struct FixedAbort {
    reason: Option<AbortReason>,
}

impl AbortSignal for FixedAbort {
    fn reason(&self) -> Option<AbortReason> {
        self.reason
    }
}

struct CountingAbort {
    calls: Cell<usize>,
    reason: AbortReason,
}

impl AbortSignal for CountingAbort {
    fn reason(&self) -> Option<AbortReason> {
        self.calls.set(self.calls.get() + 1);
        Some(self.reason)
    }
}

struct SharedAbort {
    reason: Rc<Cell<Option<AbortReason>>>,
}

impl AbortSignal for SharedAbort {
    fn reason(&self) -> Option<AbortReason> {
        self.reason.get()
    }
}

struct FixedClock;

impl Clock for FixedClock {
    fn now_millis(&self) -> i64 {
        100_000
    }
}

struct CrossingDeadlineClock {
    calls: Cell<usize>,
}

struct ToolDeadlineClock {
    calls: Cell<usize>,
}

impl Clock for ToolDeadlineClock {
    fn now_millis(&self) -> i64 {
        let calls = self.calls.get();
        self.calls.set(calls + 1);
        if calls < 2 {
            99
        } else {
            101
        }
    }
}

impl Clock for CrossingDeadlineClock {
    fn now_millis(&self) -> i64 {
        let calls = self.calls.get();
        self.calls.set(calls + 1);
        if calls == 0 {
            99
        } else {
            101
        }
    }
}

#[derive(Default)]
struct AbortAwareGateway {
    observed: Option<AbortReason>,
}

#[derive(Default)]
struct SuccessfulDeadlineCrossingGateway {
    observed: Option<AbortReason>,
}

impl ModelGateway for SuccessfulDeadlineCrossingGateway {
    fn generate<'a>(
        &'a mut self,
        _input: &'a ModelInput,
        _request: &'a ModelRequest,
        _observer: &'a mut dyn AgentObserver,
        abort: &'a dyn AbortSignal,
    ) -> Pin<Box<dyn Future<Output = Result<ModelOutput, zeta_agent::AgentError>> + 'a>> {
        Box::pin(async move {
            self.observed = abort.reason();
            Ok(ModelOutput {
                message: json!({"content": "done"}).as_object().unwrap().clone(),
                telemetry: Map::new(),
                streamed_content: false,
            })
        })
    }
}

impl ModelGateway for AbortAwareGateway {
    fn generate<'a>(
        &'a mut self,
        _input: &'a ModelInput,
        _request: &'a ModelRequest,
        _observer: &'a mut dyn AgentObserver,
        abort: &'a dyn AbortSignal,
    ) -> Pin<Box<dyn Future<Output = Result<ModelOutput, zeta_agent::AgentError>> + 'a>> {
        Box::pin(async move {
            self.observed = abort.reason();
            Err(zeta_agent::AgentError::model("model request stopped"))
        })
    }
}

struct ScriptedRun {
    result: Result<AgentRunResult, AgentRunError>,
    gateway: ScriptedGateway,
    executor: ScriptedExecutor,
    drafts: RecordingDrafts,
    ids: ScriptedIds,
}

struct NoopWake;

impl Wake for NoopWake {
    fn wake(self: Arc<Self>) {}
}

fn block_on<F: Future>(future: F) -> F::Output {
    let waker = Waker::from(Arc::new(NoopWake));
    let mut context = Context::from_waker(&waker);
    let mut future = pin!(future);
    loop {
        match future.as_mut().poll(&mut context) {
            Poll::Ready(output) => return output,
            Poll::Pending => std::thread::yield_now(),
        }
    }
}

fn vectors(name: &str) -> Value {
    let workspace = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap();
    let path = workspace.join("spec/vectors/agent").join(name);
    serde_json::from_slice(&fs::read(path).unwrap()).unwrap()
}

fn invocation() -> AgentInvocation {
    AgentInvocation {
        objective: "Exercise the agent loop.".to_owned(),
        environment: PromptEnvironment {
            working_directory: "/workspace/zeta".to_owned(),
            calendar_date: "2026-08-12".to_owned(),
        },
        ..AgentInvocation::default()
    }
}

fn capability(schema: Value, delivery_semantics: Option<DeliverySemantics>) -> Capability {
    Capability {
        id: "test.lookup".parse().unwrap(),
        description: "Look up a value.".to_owned(),
        input_schema: schema.as_object().unwrap().clone(),
        delivery_semantics,
    }
}

fn model_turn(message: Value) -> ModelScriptTurn {
    ModelScriptTurn {
        message: message.as_object().unwrap().clone(),
        stream: Vec::new(),
        telemetry: Map::new(),
        cancel_after_return: false,
    }
}

fn tool_call(id: &str, name: &str, arguments: &str) -> Value {
    json!({
        "id": id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    })
}

fn scripted_run(
    invocation: &AgentInvocation,
    capabilities: &[Capability],
    model_script: Vec<ModelScriptTurn>,
    tool_results: HashMap<String, VecDeque<Map<String, Value>>>,
    event_ids: Vec<&str>,
) -> ScriptedRun {
    let mut gateway = ScriptedGateway {
        script: VecDeque::from(model_script),
        inputs: Vec::new(),
        cancellation: None,
    };
    let mut executor = ScriptedExecutor {
        results: tool_results,
        calls: Vec::new(),
        recorded_drafts: Rc::new(RefCell::new(Vec::new())),
        capture_recorded_events: false,
    };
    let mut observer = RecordingObserver::default();
    let mut drafts = RecordingDrafts::default();
    executor.recorded_drafts = Rc::clone(&drafts.events);
    let mut values = VecDeque::new();
    for event_id in event_ids {
        values.push_back(event_id.to_owned());
    }
    let mut ids = ScriptedIds { values };
    let abort = FixedAbort { reason: None };
    let clock = FixedClock;
    let capabilities = resolve_capabilities(capabilities, invocation.tool_profile);
    let runner = AgentRunner::new(
        &capabilities,
        &mut gateway,
        &mut executor,
        &mut observer,
        &mut drafts,
        &mut ids,
        &abort,
        &clock,
    );
    let result = block_on(runner.run(invocation));
    ScriptedRun {
        result,
        gateway,
        executor,
        drafts,
        ids,
    }
}

#[test]
fn shared_prompt_vectors_match_python_ground_truth() {
    let vectors: PromptVectors = serde_json::from_value(vectors("prompts.json")).unwrap();
    assert_eq!(vectors.version, 2);
    for case in vectors.cases {
        let actual = build_prompt(&case.input, &vectors.environment).unwrap();
        assert_eq!(
            serde_json::to_value(actual).unwrap(),
            case.expected,
            "prompt case {}",
            case.name
        );
    }
}

#[test]
fn prompt_input_defaults_match_the_model_request_defaults() {
    let input = PromptInput::default();

    assert_eq!(input.tool_choice, json!("auto"));
    assert_eq!(input.max_tokens, 8_192);
}

#[test]
fn native_profile_preserves_an_explicit_model_name() {
    let capability = capability(json!({"type": "object"}), None);

    let resolved = ToolProfile::Native.resolve_capability(&capability, "find_customer");

    assert_eq!(resolved.canonical, capability);
    assert_eq!(resolved.model_name, "find_customer");
    assert_eq!(resolved.model_description, "Look up a value.");
    assert_eq!(
        resolved.model_input_schema,
        json!({"type": "object"}).as_object().unwrap().clone()
    );
    assert_eq!(resolved.argument_adapter, ArgumentAdapter::Identity);
}

#[test]
fn codex_profile_preserves_an_explicit_model_name_for_ordinary_capabilities() {
    let capability = capability(json!({"type": "object"}), None);

    let resolved = ToolProfile::Codex.resolve_capability(&capability, "find_customer");

    assert_eq!(resolved.canonical, capability);
    assert_eq!(resolved.model_name, "find_customer");
    assert_eq!(resolved.model_description, "Look up a value.");
    assert_eq!(
        resolved.model_input_schema,
        json!({"type": "object"}).as_object().unwrap().clone()
    );
    assert_eq!(resolved.argument_adapter, ArgumentAdapter::Identity);
}

#[test]
fn codex_profile_preserves_builtin_adapter_contracts() {
    let mut bash = capability(
        json!({
            "type": "object",
            "required": ["command"],
            "properties": {"command": {"type": "string"}},
            "additionalProperties": false,
        }),
        Some(DeliverySemantics::UnsafeToRetry),
    );
    bash.id = "zeta.bash".parse().unwrap();
    bash.description = "Execute a shell command.".to_owned();
    let mut patch = capability(
        json!({
            "type": "object",
            "required": ["patch"],
            "properties": {"patch": {"type": "string"}},
            "additionalProperties": false,
        }),
        Some(DeliverySemantics::IdempotentWithKey),
    );
    patch.id = "zeta.patch".parse().unwrap();
    patch.description = "Patch files.".to_owned();

    let bash = ToolProfile::Codex.resolve_capability(&bash, "run_shell");
    let patch = ToolProfile::Codex.resolve_capability(&patch, "modify_files");

    assert_eq!(bash.canonical.id.as_str(), "zeta.bash");
    assert_eq!(bash.model_name, "exec_command");
    assert_eq!(bash.model_description, "Run a shell command.");
    assert_eq!(bash.model_input_schema["required"], json!(["cmd"]));
    assert_eq!(
        bash.argument_adapter,
        ArgumentAdapter::RenameField {
            from: "cmd".to_owned(),
            to: "command".to_owned(),
        }
    );
    assert_eq!(
        Value::Object(
            bash.argument_adapter
                .adapt(json!({"cmd": "pwd"}).as_object().unwrap())
                .unwrap()
        ),
        json!({"command": "pwd"})
    );
    assert_eq!(patch.canonical.id.as_str(), "zeta.patch");
    assert_eq!(patch.model_name, "apply_patch");
    assert_eq!(patch.model_description, "Apply a patch to files.");
    assert_eq!(patch.model_input_schema["required"], json!(["patch"]));
    assert_eq!(patch.argument_adapter, ArgumentAdapter::Identity);
    assert_eq!(
        Value::Object(
            patch
                .argument_adapter
                .adapt(json!({"patch": "*** Begin Patch"}).as_object().unwrap())
                .unwrap()
        ),
        json!({"patch": "*** Begin Patch"})
    );
}

#[test]
fn context_budget_uses_latest_provider_telemetry() {
    let mut invocation = invocation();
    invocation.max_model_calls = 2;
    invocation.prompt_transform = PromptTransform::StructuralTrim {
        max_content_chars: 120_000,
    };
    invocation.compaction_threshold_tokens = Some(15_000);
    invocation
        .allowed_capabilities
        .push("zeta.query_context_budget".parse().unwrap());
    let mut query_budget = capability(
        json!({
            "type": "object",
            "properties": {},
            "additionalProperties": false,
        }),
        None,
    );
    query_budget.id = "zeta.query_context_budget".parse().unwrap();
    let mut first = model_turn(json!({
        "tool_calls": [tool_call(
            "call-context-budget",
            "query_context_budget",
            "{}",
        )],
    }));
    first.telemetry = json!({
        "usage": {"prompt_tokens": 10_000},
        "model_context_tokens": 32_768,
    })
    .as_object()
    .unwrap()
    .clone();

    let run = scripted_run(
        &invocation,
        &[query_budget],
        vec![first, model_turn(json!({"content": "budget checked"}))],
        HashMap::new(),
        vec!["model-1", "result-1", "model-2"],
    );
    let result = run.result.unwrap();
    let tool_result = result.events[2].payload["result"].clone();

    assert_eq!(tool_result["context_window_tokens"], json!(32_768));
    assert_eq!(tool_result["prompt_tokens"], json!(10_000));
    assert_eq!(tool_result["prompt_tokens_source"], json!("provider"));
    assert_eq!(tool_result["reserved_output_tokens"], json!(8_192));
    assert_eq!(tool_result["remaining_tokens"], json!(14_576));
    assert_eq!(tool_result["compaction_strategy"], json!("structural_trim"));
    assert_eq!(tool_result["compaction_threshold_tokens"], json!(15_000));
    assert!(run.executor.calls.is_empty());
}

#[test]
fn shared_invocation_vectors_match_python_ground_truth() {
    let vectors: InvocationVectors = serde_json::from_value(vectors("invocations.json")).unwrap();
    assert_eq!(vectors.version, 2);
    for case in vectors.cases.into_iter().filter(|case| {
        !case.capabilities.iter().any(|capability| {
            matches!(
                capability.id.as_str(),
                "zeta.query_content" | "zeta.transform_content" | "zeta.finish" | "zeta.query_log"
            )
        })
    }) {
        let InvocationCase {
            name,
            mut invocation,
            capabilities,
            model_script,
            tool_results,
            event_ids,
            cancelled,
            capture_recorded_events,
            expected,
        } = case;
        invocation["environment"] = serde_json::to_value(&vectors.environment).unwrap();
        let invocation: AgentInvocation = serde_json::from_value(invocation).unwrap();
        let mut gateway = ScriptedGateway {
            script: VecDeque::from(model_script),
            inputs: Vec::new(),
            cancellation: None,
        };
        let mut executor = ScriptedExecutor {
            results: tool_results,
            calls: Vec::new(),
            recorded_drafts: Rc::new(RefCell::new(Vec::new())),
            capture_recorded_events,
        };
        let mut observer = RecordingObserver::default();
        let mut drafts = RecordingDrafts::default();
        executor.recorded_drafts = Rc::clone(&drafts.events);
        let mut ids = ScriptedIds { values: event_ids };
        let reason = Rc::new(Cell::new(if cancelled {
            Some(AbortReason::Cancelled)
        } else {
            None
        }));
        gateway.cancellation = Some(Rc::clone(&reason));
        let abort = SharedAbort {
            reason: Rc::clone(&reason),
        };
        let clock = FixedClock;
        let capabilities = resolve_capabilities(&capabilities, invocation.tool_profile);
        let runner = AgentRunner::new(
            &capabilities,
            &mut gateway,
            &mut executor,
            &mut observer,
            &mut drafts,
            &mut ids,
            &abort,
            &clock,
        );
        let (aborted, abort_reason, result) = match block_on(runner.run(&invocation)) {
            Ok(result) => (false, Value::Null, result),
            Err(AgentRunError::Aborted(aborted)) => (
                true,
                serde_json::to_value(aborted.reason).unwrap(),
                aborted.result,
            ),
            Err(AgentRunError::Failed(error)) => {
                panic!("invocation case {name} failed: {error}")
            }
        };
        assert!(
            ids.values.is_empty(),
            "unused ids in invocation case {name}"
        );
        assert_eq!(
            *drafts.events.borrow(),
            result.events,
            "recorded drafts in case {name}"
        );
        let actual = json!({
            "aborted": aborted,
            "abort_reason": abort_reason,
            "final_answer": result.final_answer,
            "final_object_id": result.final_object_id,
            "stop_reason": result.stop_reason,
            "answer_streamed": result.answer_streamed,
            "telemetry": result.telemetry,
            "model_telemetry_calls": result.model_telemetry_calls,
            "events": result.events,
            "observations": observer.observations,
            "requests": result.proposals,
            "prompt_traces": result.prompt_traces,
            "steps": result.steps,
            "model_call_count": gateway.inputs.len(),
            "executor_calls": executor.calls,
            "trace": result.trace,
        });
        assert_eq!(actual, expected, "invocation case {name}");
    }
}

#[test]
fn unknown_capability_grants_are_rejected_before_model_work() {
    let mut invocation = invocation();
    invocation
        .allowed_capabilities
        .push("missing.lookup".parse().unwrap());
    let run = scripted_run(&invocation, &[], Vec::new(), HashMap::new(), Vec::new());

    let error = match run.result {
        Err(AgentRunError::Failed(error)) => error,
        Err(AgentRunError::Aborted(aborted)) => {
            panic!("unexpected abort: {}", aborted.reason)
        }
        Ok(result) => panic!("unexpected successful result: {result:?}"),
    };
    assert_eq!(error.kind, AgentErrorKind::Invocation);
    assert_eq!(error.message, "unknown capability grant: missing.lookup");
    assert!(run.gateway.inputs.is_empty());
}

#[test]
fn model_call_limit_finishes_before_polling_abort() {
    let mut invocation = invocation();
    invocation.max_model_calls = 0;
    let mut gateway = ScriptedGateway::default();
    let mut executor = ScriptedExecutor::default();
    let mut observer = RecordingObserver::default();
    let mut drafts = RecordingDrafts::default();
    let mut ids = ScriptedIds {
        values: VecDeque::new(),
    };
    let abort = CountingAbort {
        calls: Cell::new(0),
        reason: AbortReason::Cancelled,
    };
    let clock = FixedClock;
    let runner = AgentRunner::new(
        &[],
        &mut gateway,
        &mut executor,
        &mut observer,
        &mut drafts,
        &mut ids,
        &abort,
        &clock,
    );

    let result = block_on(runner.run(&invocation)).unwrap();

    assert_eq!(result.stop_reason, Some(RunStopReason::MaxModelCalls));
    assert_eq!(result.steps, [StepName::FinishRun]);
    assert_eq!(abort.calls.get(), 0);
    assert!(gateway.inputs.is_empty());
}

#[test]
fn an_explicit_grant_is_not_blocked_by_an_ungranted_name_collision() {
    let mut invocation = invocation();
    invocation
        .allowed_capabilities
        .push("one.lookup".parse().unwrap());
    let mut first = capability(json!({"type": "object"}), None);
    first.id = "one.lookup".parse().unwrap();
    let mut second = first.clone();
    second.id = "two.lookup".parse().unwrap();
    let script = vec![model_turn(json!({"content": "done"}))];

    let run = scripted_run(
        &invocation,
        &[first, second],
        script,
        HashMap::new(),
        vec!["model-1"],
    );

    assert_eq!(
        run.result.unwrap().stop_reason,
        Some(RunStopReason::Finished)
    );
    assert_eq!(run.gateway.inputs.len(), 1);
}

#[test]
fn pending_tool_batch_finishes_after_the_last_model_call() {
    let mut invocation = invocation();
    invocation.max_model_calls = 1;
    invocation
        .allowed_capabilities
        .push("test.lookup".parse().unwrap());
    let capability = capability(json!({"type": "object"}), None);
    let script = vec![model_turn(json!({
        "tool_calls": [
            tool_call("call-1", "lookup", "{}"),
            tool_call("call-2", "lookup", "{}"),
        ],
    }))];
    let mut tool_results = HashMap::new();
    tool_results.insert(
        "test.lookup".to_owned(),
        VecDeque::from([
            json!({"ok": true}).as_object().unwrap().clone(),
            json!({"ok": true}).as_object().unwrap().clone(),
        ]),
    );

    let run = scripted_run(
        &invocation,
        &[capability],
        script,
        tool_results,
        vec!["model-1", "result-1", "result-2"],
    );
    let result = run.result.unwrap();

    assert_eq!(result.stop_reason, Some(RunStopReason::MaxModelCalls));
    assert_eq!(run.gateway.inputs.len(), 1);
    assert_eq!(run.executor.calls.len(), 2);
    assert_eq!(result.events.len(), 5);
    assert!(run.ids.values.is_empty());
}

#[test]
fn stopping_tool_suppresses_later_calls_in_the_same_batch() {
    let mut invocation = invocation();
    invocation.max_model_calls = 1;
    invocation.source_queue_item_id = Some("qi-stop".to_owned());
    invocation
        .allowed_capabilities
        .push("test.lookup".parse().unwrap());
    let capability = capability(json!({"type": "object"}), None);
    let script = vec![model_turn(json!({
        "tool_calls": [
            tool_call("call-wait", "wait_for", "{\"event_type\":\"review.completed\"}"),
            tool_call("call-lookup", "lookup", "{}"),
        ],
    }))];

    let run = scripted_run(
        &invocation,
        &[capability],
        script,
        HashMap::new(),
        vec!["model-1", "result-1"],
    );
    let result = run.result.unwrap();

    assert_eq!(result.stop_reason, Some(RunStopReason::ToolStop));
    assert_eq!(result.proposals.len(), 1);
    assert!(run.executor.calls.is_empty());
    assert_eq!(result.events.len(), 3);
    assert!(run.ids.values.is_empty());
}

#[test]
fn model_telemetry_is_attached_only_to_the_first_result_in_a_batch() {
    let mut invocation = invocation();
    invocation.max_model_calls = 1;
    invocation
        .allowed_capabilities
        .push("test.lookup".parse().unwrap());
    let capability = capability(json!({"type": "object"}), None);
    let mut turn = model_turn(json!({
        "tool_calls": [
            tool_call("call-1", "lookup", "{}"),
            tool_call("call-2", "lookup", "{}"),
        ],
    }));
    turn.telemetry = json!({"request_id": "request-1"})
        .as_object()
        .unwrap()
        .clone();
    let mut tool_results = HashMap::new();
    tool_results.insert(
        "test.lookup".to_owned(),
        VecDeque::from([
            json!({"ok": true}).as_object().unwrap().clone(),
            json!({"ok": true}).as_object().unwrap().clone(),
        ]),
    );

    let run = scripted_run(
        &invocation,
        &[capability],
        vec![turn],
        tool_results,
        vec!["model-1", "result-1", "result-2"],
    );
    let result = run.result.unwrap();
    let mut tool_results = Vec::new();
    for event in &result.events {
        if event.payload.get("_timeline_type") == Some(&json!("tool_result")) {
            tool_results.push(event);
        }
    }

    assert_eq!(tool_results.len(), 2);
    assert_eq!(
        tool_results[0].payload.get("model_telemetry"),
        Some(&json!({"request_id": "request-1"}))
    );
    assert_eq!(tool_results[1].payload.get("model_telemetry"), None);
}

#[test]
fn control_positions_increase_across_model_turns() {
    let mut invocation = invocation();
    invocation.max_model_calls = 2;
    invocation.source_queue_item_id = Some("qi-control".to_owned());
    invocation
        .publishable_events
        .insert("progress.updated".to_owned(), json!({"type": "object"}));
    let script = vec![
        model_turn(json!({
            "tool_calls": [tool_call(
                "call-publish",
                "publish_event",
                "{\"event_type\":\"progress.updated\",\"payload\":{}}",
            )],
        })),
        model_turn(json!({
            "tool_calls": [tool_call(
                "call-wait",
                "wait_for",
                "{\"event_type\":\"review.completed\"}",
            )],
        })),
    ];

    let run = scripted_run(
        &invocation,
        &[],
        script,
        HashMap::new(),
        vec!["model-1", "result-1", "model-2", "result-2"],
    );
    let result = run.result.unwrap();

    assert_eq!(result.stop_reason, Some(RunStopReason::ToolStop));
    assert_eq!(result.proposals.len(), 2);
    match &result.proposals[0] {
        AgentProposal::Publish { position, .. } => assert_eq!(*position, 0),
        AgentProposal::Wait { .. } | AgentProposal::Cancel { .. } => {
            panic!("expected publish proposal")
        }
    }
    match &result.proposals[1] {
        AgentProposal::Wait { position, .. } => assert_eq!(*position, 1),
        AgentProposal::Publish { .. } | AgentProposal::Cancel { .. } => {
            panic!("expected wait proposal")
        }
    }
}

#[test]
fn publish_proposals_must_match_the_declared_event_schema() {
    let mut invocation = invocation();
    invocation.max_model_calls = 1;
    invocation.source_queue_item_id = Some("qi-publish".to_owned());
    invocation.publishable_events.insert(
        "review.completed".to_owned(),
        json!({
            "type": "object",
            "required": ["approved"],
            "properties": {"approved": {"type": "boolean"}},
            "additionalProperties": false,
        }),
    );
    let script = vec![model_turn(json!({
        "tool_calls": [tool_call(
            "call-publish",
            "publish_event",
            "{\"event_type\":\"review.completed\",\"payload\":{\"approved\":\"yes\"}}",
        )],
    }))];

    let run = scripted_run(
        &invocation,
        &[],
        script,
        HashMap::new(),
        vec!["model-1", "result-1"],
    );
    assert_eq!(
        *run.drafts.events.borrow(),
        run.result.as_ref().unwrap().events
    );
    let result = run.result.unwrap();

    assert!(result.proposals.is_empty());
    let tool_result = result.events[2]
        .payload
        .get("result")
        .and_then(Value::as_object)
        .unwrap();
    let error = tool_result.get("error").and_then(Value::as_object).unwrap();
    assert_eq!(
        error.get("code").and_then(Value::as_str),
        Some("invalid-event-payload")
    );
}

#[test]
fn invalid_tool_calls_become_stable_results_without_execution() {
    let mut invocation = invocation();
    invocation.max_model_calls = 1;
    invocation
        .allowed_capabilities
        .push("test.lookup".parse().unwrap());
    let capability = capability(
        json!({
            "type": "object",
            "required": ["limit"],
            "properties": {"limit": {"type": "integer", "minimum": 1}},
            "additionalProperties": false,
        }),
        None,
    );
    let script = vec![model_turn(json!({
        "tool_calls": [
            tool_call("call-json", "lookup", "{"),
            tool_call("call-schema", "lookup", "{\"limit\":0}"),
            tool_call("call-unknown", "missing", "{}"),
            {"id": "call-shape", "type": "function"},
        ],
    }))];

    let run = scripted_run(
        &invocation,
        &[capability],
        script,
        HashMap::new(),
        vec![
            "model-1",
            "result-json",
            "result-schema",
            "result-unknown",
            "result-shape",
        ],
    );
    let result = run.result.unwrap();
    let mut error_codes = Vec::new();
    let mut statuses = Vec::new();
    for event in &result.events {
        if event.event_type != "zeta.tool_call.failed" {
            continue;
        }
        let code = event
            .payload
            .get("result")
            .and_then(Value::as_object)
            .and_then(|result| result.get("error"))
            .and_then(Value::as_object)
            .and_then(|error| error.get("code"))
            .and_then(Value::as_str)
            .unwrap();
        error_codes.push(code.to_owned());
        statuses.push(
            event
                .payload
                .get("status")
                .and_then(Value::as_str)
                .unwrap()
                .to_owned(),
        );
    }

    assert_eq!(
        error_codes,
        [
            "invalid-json-args",
            "invalid-tool-args",
            "unknown-tool",
            "invalid-tool-call",
        ]
    );
    assert_eq!(statuses, ["refused", "failed", "refused", "refused"]);
    assert!(run.executor.calls.is_empty());
    let shape_call = &result.events[result.events.len() - 2].payload;
    assert!(!shape_call.contains_key("status"));
    assert!(!shape_call.contains_key("arguments"));
}

#[test]
fn effect_lifecycle_is_recorded_around_execution() {
    let mut invocation = invocation();
    invocation.max_model_calls = 1;
    invocation.effect_scope = Some("attempt-7".to_owned());
    invocation
        .allowed_capabilities
        .push("test.lookup".parse().unwrap());
    let capability = capability(
        json!({"type": "object"}),
        Some(DeliverySemantics::IdempotentWithKey),
    );
    let script = vec![model_turn(json!({
        "tool_calls": [tool_call("call-1", "lookup", "{}")],
    }))];
    let mut tool_results = HashMap::new();
    tool_results.insert(
        "test.lookup".to_owned(),
        VecDeque::from([json!({"ok": true}).as_object().unwrap().clone()]),
    );

    let run = scripted_run(
        &invocation,
        &[capability],
        script,
        tool_results,
        vec!["model-1", "result-1"],
    );
    let result = run.result.unwrap();
    let mut statuses = Vec::new();
    for event in &result.events {
        let Some(status) = event.event_type.strip_prefix("runtime.effect.") else {
            continue;
        };
        statuses.push(status);
    }
    assert_eq!(statuses, ["planned", "started", "completed"]);
    let effect_key = run.executor.calls[0].effect_key.as_deref().unwrap();
    assert!(effect_key.starts_with("effect:b3:"));
    for event in &result.events {
        if !event.event_type.starts_with("runtime.effect.") {
            continue;
        }
        assert_eq!(event.payload["effect_key"], effect_key);
        assert_eq!(event.payload["semantics"], "idempotent_with_key");
        assert_eq!(event.payload["scope"], "attempt-7");
        assert_eq!(event.caused_by.as_deref(), Some("call-1"));
    }
}

#[test]
fn recorder_failure_stops_before_an_effect_crosses_its_barrier() {
    let mut invocation = invocation();
    invocation.max_model_calls = 1;
    invocation.effect_scope = Some("attempt-7".to_owned());
    invocation
        .allowed_capabilities
        .push("test.lookup".parse().unwrap());
    let capability = capability(
        json!({"type": "object"}),
        Some(DeliverySemantics::IdempotentWithKey),
    );
    let capabilities = resolve_capabilities(&[capability], ToolProfile::Native);
    let mut gateway = ScriptedGateway {
        script: VecDeque::from([model_turn(json!({
            "tool_calls": [tool_call("call-1", "lookup", "{}")],
        }))]),
        inputs: Vec::new(),
        cancellation: None,
    };
    let mut executor = ScriptedExecutor::default();
    let mut observer = RecordingObserver::default();
    let mut drafts = RejectingDrafts {
        event_type: "runtime.effect.started",
    };
    let mut ids = ScriptedIds {
        values: VecDeque::from(["model-1".to_owned()]),
    };
    let abort = FixedAbort { reason: None };
    let clock = FixedClock;
    let runner = AgentRunner::new(
        &capabilities,
        &mut gateway,
        &mut executor,
        &mut observer,
        &mut drafts,
        &mut ids,
        &abort,
        &clock,
    );

    let error = block_on(runner.run(&invocation)).unwrap_err();

    let AgentRunError::Failed(error) = error else {
        panic!("draft rejection must fail the run")
    };
    assert_eq!(error.kind, AgentErrorKind::Durability);
    assert!(executor.calls.is_empty());
}

#[test]
fn cancellation_during_an_unsafe_tool_records_ambiguity_before_aborting() {
    let mut invocation = invocation();
    invocation.max_model_calls = 2;
    invocation.effect_scope = Some("attempt-7".to_owned());
    invocation
        .allowed_capabilities
        .push("test.lookup".parse().unwrap());
    let capabilities = resolve_capabilities(
        &[capability(
            json!({"type": "object"}),
            Some(DeliverySemantics::UnsafeToRetry),
        )],
        ToolProfile::Native,
    );
    let reason = Rc::new(Cell::new(None));
    let mut gateway = ScriptedGateway {
        script: VecDeque::from([
            model_turn(json!({
                "tool_calls": [tool_call("call-1", "lookup", "{}")],
            })),
            model_turn(json!({"content": "must not run"})),
        ]),
        inputs: Vec::new(),
        cancellation: None,
    };
    let mut executor = AbortReturningExecutor {
        trigger: Some(Rc::clone(&reason)),
        observed: None,
    };
    let mut observer = RecordingObserver::default();
    let mut drafts = RecordingDrafts::default();
    let mut ids = ScriptedIds {
        values: VecDeque::from(["model-1".to_owned(), "result-1".to_owned()]),
    };
    let abort = SharedAbort { reason };
    let clock = FixedClock;
    let runner = AgentRunner::new(
        &capabilities,
        &mut gateway,
        &mut executor,
        &mut observer,
        &mut drafts,
        &mut ids,
        &abort,
        &clock,
    );

    let aborted = match block_on(runner.run(&invocation)) {
        Err(AgentRunError::Aborted(aborted)) => aborted,
        Err(AgentRunError::Failed(error)) => panic!("unexpected failure: {error}"),
        Ok(result) => panic!("unexpected successful result: {result:?}"),
    };

    assert_eq!(executor.observed, Some(AbortReason::Cancelled));
    assert_eq!(gateway.inputs.len(), 1);
    assert!(ids.values.is_empty());
    assert_eq!(aborted.reason, AbortReason::Cancelled);
    assert_eq!(
        aborted
            .result
            .events
            .iter()
            .map(|event| event.event_type.as_str())
            .collect::<Vec<_>>(),
        [
            "zeta.model_call.completed",
            "zeta.tool_call.started",
            "runtime.effect.planned",
            "runtime.effect.started",
            "zeta.tool_call.failed",
            "runtime.effect.ambiguous",
            "zeta.turn.failed",
        ]
    );
    assert_eq!(
        aborted.result.events[4].payload["result"]["error"]["code"],
        "tool-aborted"
    );
    assert_eq!(
        aborted.result.events.last().unwrap().caused_by.as_deref(),
        Some("result-1")
    );
}

#[test]
fn deadline_during_a_retry_safe_tool_records_failure_before_aborting() {
    let mut invocation = invocation();
    invocation.max_model_calls = 2;
    invocation.deadline_ms = Some(100);
    invocation.effect_scope = Some("attempt-8".to_owned());
    invocation
        .allowed_capabilities
        .push("test.lookup".parse().unwrap());
    let capabilities = resolve_capabilities(
        &[capability(
            json!({"type": "object"}),
            Some(DeliverySemantics::IdempotentWithKey),
        )],
        ToolProfile::Native,
    );
    let mut gateway = ScriptedGateway {
        script: VecDeque::from([
            model_turn(json!({
                "tool_calls": [tool_call("call-1", "lookup", "{}")],
            })),
            model_turn(json!({"content": "must not run"})),
        ]),
        inputs: Vec::new(),
        cancellation: None,
    };
    let mut executor = AbortReturningExecutor {
        trigger: None,
        observed: None,
    };
    let mut observer = RecordingObserver::default();
    let mut drafts = RecordingDrafts::default();
    let mut ids = ScriptedIds {
        values: VecDeque::from(["model-1".to_owned(), "result-1".to_owned()]),
    };
    let abort = FixedAbort { reason: None };
    let clock = ToolDeadlineClock {
        calls: Cell::new(0),
    };
    let runner = AgentRunner::new(
        &capabilities,
        &mut gateway,
        &mut executor,
        &mut observer,
        &mut drafts,
        &mut ids,
        &abort,
        &clock,
    );

    let aborted = match block_on(runner.run(&invocation)) {
        Err(AgentRunError::Aborted(aborted)) => aborted,
        Err(AgentRunError::Failed(error)) => panic!("unexpected failure: {error}"),
        Ok(result) => panic!("unexpected successful result: {result:?}"),
    };

    assert_eq!(executor.observed, Some(AbortReason::DeadlineExceeded));
    assert_eq!(gateway.inputs.len(), 1);
    assert!(ids.values.is_empty());
    assert_eq!(aborted.reason, AbortReason::DeadlineExceeded);
    assert_eq!(
        aborted
            .result
            .events
            .iter()
            .map(|event| event.event_type.as_str())
            .collect::<Vec<_>>(),
        [
            "zeta.model_call.completed",
            "zeta.tool_call.started",
            "runtime.effect.planned",
            "runtime.effect.started",
            "zeta.tool_call.failed",
            "runtime.effect.failed",
            "zeta.turn.failed",
        ]
    );
}

#[test]
fn empty_generated_event_ids_fail_the_invocation() {
    let invocation = invocation();
    let script = vec![model_turn(json!({"content": "done"}))];
    let run = scripted_run(&invocation, &[], script, HashMap::new(), vec![""]);

    let error = match run.result {
        Err(AgentRunError::Failed(error)) => error,
        Err(AgentRunError::Aborted(aborted)) => {
            panic!("unexpected abort: {}", aborted.reason)
        }
        Ok(result) => panic!("unexpected successful result: {result:?}"),
    };
    assert_eq!(error.kind, AgentErrorKind::Identity);
    assert_eq!(error.message, "event id must not be empty");
}

#[test]
fn a_deadline_that_expires_during_model_work_aborts_the_run() {
    let mut invocation = invocation();
    invocation.deadline_ms = Some(100);
    let mut gateway = AbortAwareGateway::default();
    let mut executor = ScriptedExecutor::default();
    let mut observer = RecordingObserver::default();
    let mut drafts = RecordingDrafts::default();
    let mut ids = ScriptedIds {
        values: VecDeque::new(),
    };
    let abort = FixedAbort { reason: None };
    let clock = CrossingDeadlineClock {
        calls: Cell::new(0),
    };
    let runner = AgentRunner::new(
        &[],
        &mut gateway,
        &mut executor,
        &mut observer,
        &mut drafts,
        &mut ids,
        &abort,
        &clock,
    );

    let aborted = match block_on(runner.run(&invocation)) {
        Err(AgentRunError::Aborted(aborted)) => aborted,
        Err(AgentRunError::Failed(error)) => panic!("unexpected failure: {error}"),
        Ok(result) => panic!("unexpected successful result: {result:?}"),
    };
    assert_eq!(gateway.observed, Some(AbortReason::DeadlineExceeded));
    assert_eq!(aborted.reason, AbortReason::DeadlineExceeded);
    assert_eq!(aborted.result.events.len(), 1);
}

#[test]
fn successful_model_response_survives_a_newly_crossed_deadline() {
    let mut invocation = invocation();
    invocation.deadline_ms = Some(100);
    let mut gateway = SuccessfulDeadlineCrossingGateway::default();
    let mut executor = ScriptedExecutor::default();
    let mut observer = RecordingObserver::default();
    let mut drafts = RecordingDrafts::default();
    let mut ids = ScriptedIds {
        values: VecDeque::from(["model-1".to_owned()]),
    };
    let abort = FixedAbort { reason: None };
    let clock = CrossingDeadlineClock {
        calls: Cell::new(0),
    };
    let runner = AgentRunner::new(
        &[],
        &mut gateway,
        &mut executor,
        &mut observer,
        &mut drafts,
        &mut ids,
        &abort,
        &clock,
    );

    let result = block_on(runner.run(&invocation)).unwrap();

    assert_eq!(gateway.observed, Some(AbortReason::DeadlineExceeded));
    assert_eq!(result.stop_reason, Some(RunStopReason::Finished));
    assert_eq!(result.final_answer, "done");
    assert_eq!(
        result
            .events
            .iter()
            .map(|event| event.event_type.as_str())
            .collect::<Vec<_>>(),
        ["zeta.model_call.completed"]
    );
    assert_eq!(clock.calls.get(), 2);
}
