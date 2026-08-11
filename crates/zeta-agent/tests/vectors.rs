//! Shared Python and Rust agent conformance vectors.

use std::cell::Cell;
use std::collections::{HashMap, VecDeque};
use std::fs;
use std::future::Future;
use std::path::Path;
use std::pin::{pin, Pin};
use std::sync::Arc;
use std::task::{Context, Poll, Wake, Waker};

use serde::Deserialize;
use serde_json::{json, Map, Value};
use zeta_agent::{
    build_prompt, AbortReason, AbortSignal, AgentErrorKind, AgentInvocation, AgentObserver,
    AgentRequest, AgentRunError, AgentRunResult, AgentRunner, Capability, CapabilityInvocation,
    Clock, DeliverySemantics, EffectEvent, EffectRecorder, EffectStatus, IdSource, ModelGateway,
    ModelInput, ModelOutput, ModelRequest, Observation, PromptEnvironment, PromptInput,
    RunStopReason, ToolExecutor,
};

#[derive(Deserialize)]
struct PromptVectors {
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
    expected: Value,
}

#[derive(Clone, Deserialize)]
struct ModelScriptTurn {
    message: Map<String, Value>,
    stream: Vec<ScriptedObservation>,
    telemetry: Map<String, Value>,
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
    calls: Vec<CapabilityInvocation>,
}

impl ToolExecutor for ScriptedExecutor {
    fn execute<'a>(
        &'a mut self,
        invocation: &'a CapabilityInvocation,
    ) -> Pin<Box<dyn Future<Output = Result<Map<String, Value>, zeta_agent::AgentError>> + 'a>>
    {
        Box::pin(async move {
            self.calls.push(invocation.clone());
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

#[derive(Default)]
struct RecordingObserver {
    observations: Vec<Observation>,
}

impl AgentObserver for RecordingObserver {
    fn observe(&mut self, observation: Observation) {
        self.observations.push(observation);
    }
}

#[derive(Default)]
struct RecordingEffects {
    events: Vec<EffectEvent>,
}

impl EffectRecorder for RecordingEffects {
    fn record(&mut self, event: EffectEvent) -> Result<(), zeta_agent::AgentError> {
        self.events.push(event);
        Ok(())
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

struct FixedClock;

impl Clock for FixedClock {
    fn now_millis(&self) -> i64 {
        100_000
    }
}

struct CrossingDeadlineClock {
    calls: Cell<usize>,
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
    effects: RecordingEffects,
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
    };
    let mut executor = ScriptedExecutor {
        results: tool_results,
        calls: Vec::new(),
    };
    let mut observer = RecordingObserver::default();
    let mut effects = RecordingEffects::default();
    let mut values = VecDeque::new();
    for event_id in event_ids {
        values.push_back(event_id.to_owned());
    }
    let mut ids = ScriptedIds { values };
    let abort = FixedAbort { reason: None };
    let clock = FixedClock;
    let runner = AgentRunner::new(
        capabilities,
        &mut gateway,
        &mut executor,
        &mut observer,
        &mut effects,
        &mut ids,
        &abort,
        &clock,
    );
    let result = block_on(runner.run(invocation));
    ScriptedRun {
        result,
        gateway,
        executor,
        effects,
        ids,
    }
}

#[test]
fn shared_prompt_vectors_match_python_ground_truth() {
    let vectors: PromptVectors = serde_json::from_value(vectors("prompts.json")).unwrap();
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
fn shared_invocation_vectors_match_python_ground_truth() {
    let vectors: InvocationVectors = serde_json::from_value(vectors("invocations.json")).unwrap();
    for case in vectors.cases {
        let InvocationCase {
            name,
            mut invocation,
            capabilities,
            model_script,
            tool_results,
            event_ids,
            cancelled,
            expected,
        } = case;
        invocation["environment"] = serde_json::to_value(&vectors.environment).unwrap();
        let invocation: AgentInvocation = serde_json::from_value(invocation).unwrap();
        let mut gateway = ScriptedGateway {
            script: VecDeque::from(model_script),
            inputs: Vec::new(),
        };
        let mut executor = ScriptedExecutor {
            results: tool_results,
            calls: Vec::new(),
        };
        let mut observer = RecordingObserver::default();
        let mut effects = RecordingEffects::default();
        let mut ids = ScriptedIds { values: event_ids };
        let abort = FixedAbort {
            reason: if cancelled {
                Some(AbortReason::Cancelled)
            } else {
                None
            },
        };
        let clock = FixedClock;
        let runner = AgentRunner::new(
            &capabilities,
            &mut gateway,
            &mut executor,
            &mut observer,
            &mut effects,
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
        assert!(
            effects.events.is_empty(),
            "unexpected effects in case {name}"
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
            "requests": result.requests,
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

    assert_eq!(result.stop_reason, Some(RunStopReason::MaxTurns));
    assert_eq!(run.gateway.inputs.len(), 1);
    assert_eq!(run.executor.calls.len(), 2);
    assert_eq!(result.events.len(), 5);
    assert!(run.ids.values.is_empty());
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
    assert_eq!(result.requests.len(), 2);
    match &result.requests[0] {
        AgentRequest::Publish { position, .. } => assert_eq!(*position, 0),
        AgentRequest::Wait { .. }
        | AgentRequest::Cancel { .. }
        | AgentRequest::Return { .. }
        | AgentRequest::ContentPromotion { .. } => panic!("expected publish request"),
    }
    match &result.requests[1] {
        AgentRequest::Wait { position, .. } => assert_eq!(*position, 1),
        AgentRequest::Publish { .. }
        | AgentRequest::Cancel { .. }
        | AgentRequest::Return { .. }
        | AgentRequest::ContentPromotion { .. } => panic!("expected wait request"),
    }
}

#[test]
fn publish_requests_must_match_the_declared_event_schema() {
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
    let result = run.result.unwrap();

    assert!(result.requests.is_empty());
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
    run.result.unwrap();

    let mut statuses = Vec::new();
    for event in &run.effects.events {
        statuses.push(event.status);
    }
    assert_eq!(
        statuses,
        [
            EffectStatus::Planned,
            EffectStatus::Started,
            EffectStatus::Completed,
        ]
    );
    let effect_key = run.executor.calls[0].effect_key.as_deref().unwrap();
    assert!(effect_key.starts_with("effect:b3:"));
    for event in &run.effects.events {
        assert_eq!(event.effect_key, effect_key);
        assert_eq!(event.semantics, DeliverySemantics::IdempotentWithKey);
        assert_eq!(event.scope, "attempt-7");
        assert_eq!(event.caused_by, "call-1");
    }
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
    let mut effects = RecordingEffects::default();
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
        &mut effects,
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
