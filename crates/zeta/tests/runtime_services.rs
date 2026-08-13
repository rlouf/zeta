use std::collections::{BTreeMap, VecDeque};
use std::path::PathBuf;
use std::sync::{Arc, Barrier, Mutex};
use std::thread;
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{json, Map, Value};
use tempfile::tempdir;
use zeta::{
    attempt_completion, prepare_agent, CallbackDraftRecorder, CallbackObserver, CancellationToken,
    CompletionHandoffErrorKind, ExecutorSelection, InvocationInputs, PrepareAgentErrorKind,
    ScheduleStatus, Scheduler, SchedulerErrorKind, SystemClock, UuidIdSource,
};
use zeta_agent::{
    AbortReason, AbortSignal, AgentErrorKind, AgentInvocation, AgentObserver, AgentProposal,
    AgentRunResult, ArgumentAdapter, Capability, Clock,
    DeliverySemantics as AgentDeliverySemantics, DraftRecorder, IdSource, Observation,
    PromptEnvironment, PromptTransform, ResolvedCapability, RunStopReason, ToolProfile,
};
use zeta_dispatch::{
    AttemptCompletionDisposition, AttemptControl, ClaimToken, Dispatch, EventPattern,
    QueueItemStatus, Route, RuntimeEventIdentity, SessionRule, WaitStatus,
};
use zeta_journal::{DraftEvent, Event, EventFilter};
use zeta_manifest::{
    compile_project, execution_manifest, parse_agent, project_manifest, verify_execution_manifest,
    AgentProjectInput, CapabilitySpec, DeliverySemantics as AuthoredDeliverySemantics,
    EventRegistry, ExecutionManifest, ExecutorProviderSpec, ImplementationFingerprint,
    ModelSelectionSpec, ProjectManifest,
};

fn draft() -> DraftEvent {
    DraftEvent {
        event_type: "runtime.effect.started".to_owned(),
        source: "capability:test.effect".to_owned(),
        payload: Map::new(),
        idempotency_key: Some("runtime.effect.started:effect-1".to_owned()),
        caused_by: Some("call-1".to_owned()),
        session_id: Some("session-1".to_owned()),
        run_id: Some("run-1".to_owned()),
        turn_id: Some("turn-1".to_owned()),
    }
}

fn object(value: Value) -> Map<String, Value> {
    let Value::Object(value) = value else {
        panic!("the test value must be an object")
    };
    value
}

fn implementation(byte: u8) -> ImplementationFingerprint {
    let byte = format!("{byte:02x}");
    let hash = format!("b3:{}", byte.repeat(32));
    serde_json::from_value(Value::String(hash)).expect("the test fingerprint must be valid")
}

fn capability(
    id: &str,
    name: &str,
    delivery_semantics: Option<AuthoredDeliverySemantics>,
) -> CapabilitySpec {
    capability_with_schema(
        id,
        name,
        delivery_semantics,
        object(json!({
            "type": "object",
            "additionalProperties": false,
        })),
    )
}

fn capability_with_schema(
    id: &str,
    name: &str,
    delivery_semantics: Option<AuthoredDeliverySemantics>,
    input_schema: Map<String, Value>,
) -> CapabilitySpec {
    CapabilitySpec {
        id: id.parse().expect("the test capability id must be valid"),
        name: name.to_owned(),
        description: format!("Runs {name}."),
        input_schema,
        delivery_semantics,
        owner: None,
        implementation: implementation(3),
    }
}

fn selected_model(model: &str, url: &str, api: &str, tool_profile: &str) -> ModelSelectionSpec {
    ModelSelectionSpec {
        profile: "test".to_owned(),
        model: model.to_owned(),
        url: url.to_owned(),
        thinking: Some("high".to_owned()),
        api: api.to_owned(),
        tool_profile: tool_profile.to_owned(),
        implementation: implementation(4),
    }
}

fn manifest_pair(
    declarations: &str,
    events: EventRegistry,
    capabilities: Vec<CapabilitySpec>,
    model: Option<ModelSelectionSpec>,
) -> (ProjectManifest, ExecutionManifest) {
    let source = format!(
        "---\nname: Worker\ndescription: Routes authored work.\nexecutor:\n  provider: scripted\n  config:\n    mode: fixture\n{declarations}---\nExecute the supplied objective.\n"
    );
    let agent = parse_agent("worker", source.as_bytes()).expect("the test agent must parse");
    let project = compile_project(AgentProjectInput {
        agents: vec![agent],
        events,
        skill_resources: Vec::new(),
        skill_specs: Vec::new(),
        connectors: Vec::new(),
        capabilities,
        executor_providers: vec![ExecutorProviderSpec {
            id: "scripted".to_owned(),
            implementation: implementation(2),
        }],
        model,
        runtime_fingerprint: implementation(1),
    })
    .expect("the test project must compile");
    let project_manifest = project_manifest(&project).expect("the project manifest must build");
    let execution = execution_manifest(&project, &project_manifest.id, "worker")
        .expect("the execution manifest must build");
    (project_manifest, execution)
}

fn scheduled_manifest(declarations: &str) -> ProjectManifest {
    let (project, _execution) = manifest_pair(declarations, EventRegistry::new(), Vec::new(), None);
    project
}

struct ScriptedIds {
    values: VecDeque<String>,
}

impl ScriptedIds {
    fn new(values: &[&str]) -> Self {
        let mut ids = VecDeque::new();
        for value in values {
            ids.push_back((*value).to_owned());
        }
        Self { values: ids }
    }
}

impl IdSource for ScriptedIds {
    fn next_id(&mut self) -> Result<String, zeta_agent::AgentError> {
        let Some(id) = self.values.pop_front() else {
            return Err(zeta_agent::AgentError::identity(
                "the scripted scheduler identity source is exhausted",
            ));
        };
        Ok(id)
    }
}

#[derive(Default)]
struct CountingIds {
    next: u64,
}

impl IdSource for CountingIds {
    fn next_id(&mut self) -> Result<String, zeta_agent::AgentError> {
        self.next += 1;
        Ok(format!("scheduler-event-{}", self.next))
    }
}

fn scheduling_vector_manifest(case: &Value) -> ProjectManifest {
    let agent = case["agent_id"]
        .as_str()
        .expect("the vector agent id must be a string");
    let cron = case["schedule"]["cron"]
        .as_str()
        .expect("the vector cron must be a string");
    let enabled = case.get("enabled").and_then(Value::as_bool).unwrap_or(true);
    let mut schedule = format!(
        "enabled: {enabled}\nschedules:\n  - cron: {}\n",
        serde_json::to_string(cron).expect("the vector cron must serialize")
    );
    if let Some(timezone) = case["schedule"].get("timezone").and_then(Value::as_str) {
        schedule.push_str(&format!(
            "    timezone: {}\n",
            serde_json::to_string(timezone).expect("the vector timezone must serialize")
        ));
    }
    if let Some(catchup) = case["schedule"].get("catchup").and_then(Value::as_str) {
        schedule.push_str(&format!(
            "    catchup: {}\n",
            serde_json::to_string(catchup).expect("the vector catch-up value must serialize")
        ));
    }
    let source = format!(
        "---\nname: Reporter\ndescription: Reports on schedule.\nexecutor:\n  provider: scripted\n  config: {{}}\n{schedule}---\nReport.\n"
    );
    let agent = parse_agent(agent, source.as_bytes()).expect("the vector agent must parse");
    let project = compile_project(AgentProjectInput {
        agents: vec![agent],
        events: EventRegistry::new(),
        skill_resources: Vec::new(),
        skill_specs: Vec::new(),
        connectors: Vec::new(),
        capabilities: Vec::new(),
        executor_providers: vec![ExecutorProviderSpec {
            id: "scripted".to_owned(),
            implementation: implementation(2),
        }],
        model: None,
        runtime_fingerprint: implementation(1),
    })
    .expect("the vector project must compile");
    project_manifest(&project).expect("the vector project manifest must build")
}

fn normalize_scheduler_aliases(value: &mut Value, aliases: &BTreeMap<String, String>) {
    match value {
        Value::Null => {}
        Value::Bool(_value) => {}
        Value::Number(_value) => {}
        Value::String(value) => {
            if let Some(alias) = aliases.get(value) {
                *value = alias.clone();
            }
        }
        Value::Array(values) => {
            for value in values {
                normalize_scheduler_aliases(value, aliases);
            }
        }
        Value::Object(values) => {
            for value in values.values_mut() {
                normalize_scheduler_aliases(value, aliases);
            }
        }
    }
}

fn invocation_inputs(project_directory: PathBuf) -> InvocationInputs {
    InvocationInputs {
        objective: "Deliver the requested work.".to_owned(),
        timeline: vec![object(json!({
            "type": "work.requested",
            "payload": {"text": "hello"},
        }))],
        context: "Project context.".to_owned(),
        project_directory,
        home_directory: Some(PathBuf::from("/home/remi")),
        base_directory_override: None,
        calendar_date: "2026-08-12".to_owned(),
        model_session_id: Some("model-session-1".to_owned()),
        max_model_calls: 7,
        max_tokens: 512,
        tool_choice: json!({"type": "function", "function": {"name": "deliver"}}),
        source_queue_item_id: Some("queue-item-1".to_owned()),
        effect_scope: Some("attempt-1".to_owned()),
        source_session_id: Some("source-session-1".to_owned()),
        caused_by: Some("event-1".to_owned()),
        event_source: "agent:worker".to_owned(),
        session_id: Some("session-1".to_owned()),
        run_id: Some("run-1".to_owned()),
        turn_id: Some("turn-1".to_owned()),
        prompt_transform: PromptTransform::StructuralTrim {
            max_content_chars: 4_096,
        },
        compaction_threshold_tokens: Some(8_000),
        deadline_ms: Some(1_800_000_000_000),
    }
}

#[test]
fn system_clock_reports_unix_milliseconds() {
    let before = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("the system clock must be after the Unix epoch")
        .as_millis() as i64;
    let now = SystemClock.now_millis();
    let after = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("the system clock must be after the Unix epoch")
        .as_millis() as i64;

    assert!((before..=after).contains(&now));
}

#[test]
fn cancellation_token_shares_the_first_reason() {
    let token = CancellationToken::new();
    let peer = token.clone();

    assert_eq!(token.reason(), None);
    assert!(peer.cancel(AbortReason::DeadlineExceeded));
    assert!(!token.cancel(AbortReason::Cancelled));
    assert_eq!(token.reason(), Some(AbortReason::DeadlineExceeded));
}

#[test]
fn uuid_id_source_returns_unique_opaque_values() {
    let mut source = UuidIdSource::new("event");

    let first = source.next_id().expect("the first UUID must be available");
    let second = source.next_id().expect("the second UUID must be available");

    assert_ne!(first, second);
    assert!(first.starts_with("event_"));
    assert_eq!(first.len(), "event_".len() + 36);
    assert_eq!(&first[20..21], "4");
}

#[test]
fn callback_observer_forwards_transient_values_in_order() {
    let observed = Arc::new(Mutex::new(Vec::new()));
    let captured = Arc::clone(&observed);
    let mut observer = CallbackObserver::new(move |observation| {
        captured.lock().expect("observation lock").push(observation);
    });

    observer.observe(Observation::TextDelta {
        text: "hello".to_owned(),
    });
    observer.observe(Observation::ReasoningDelta {
        text: "think".to_owned(),
    });

    assert_eq!(
        *observed.lock().expect("observation lock"),
        vec![
            Observation::TextDelta {
                text: "hello".to_owned(),
            },
            Observation::ReasoningDelta {
                text: "think".to_owned(),
            },
        ]
    );
}

#[test]
fn callback_draft_recorder_propagates_durability_failure() {
    let mut recorder = CallbackDraftRecorder::new(|_draft: &DraftEvent| {
        Err::<(), String>("storage offline".to_owned())
    });

    let error = recorder
        .record(&draft())
        .expect_err("the callback failure must stop the durability boundary");

    assert_eq!(error.kind, AgentErrorKind::Durability);
    assert_eq!(error.message, "storage offline");
}

#[test]
fn agent_result_becomes_typed_dispatch_completion_without_reordering_controls() {
    let mut result = AgentRunResult {
        final_answer: "done".to_owned(),
        final_object_id: Some("obj-final".to_owned()),
        stop_reason: Some(RunStopReason::ToolStop),
        events: vec![draft()],
        proposals: vec![
            AgentProposal::Publish {
                handle: "pub-first".to_owned(),
                event_type: "work.first".to_owned(),
                payload: object(json!({"position": 0})),
                at: None,
                position: 0,
            },
            AgentProposal::Wait {
                handle: "wait-middle".to_owned(),
                event_type: "work.ready".to_owned(),
                fields: object(json!({"work_id": "42"})),
                deadline: Some("2030-01-02T03:04:05Z".to_owned()),
                position: 1,
            },
            AgentProposal::Cancel {
                handle: "wait-old".to_owned(),
                reason: Some("superseded".to_owned()),
                source_agent_id: "worker".to_owned(),
                source_session_id: "agent/worker/session".to_owned(),
                position: 2,
            },
        ],
        ..AgentRunResult::default()
    };
    result.telemetry.insert(
        "usage".to_owned(),
        json!({"input_tokens": 12, "output_tokens": 4}),
    );

    let completion = attempt_completion("2026-08-12T10:00:01Z", &result).unwrap();

    assert_eq!(completion.finished_at(), "2026-08-12T10:00:01Z");
    assert_eq!(
        completion.disposition(),
        AttemptCompletionDisposition::Succeeded
    );
    assert_eq!(
        completion.metadata()["final_answer"],
        Value::String("done".to_owned())
    );
    assert_eq!(completion.metadata()["final_object_id"], "obj-final");
    assert_eq!(completion.metadata()["stop_reason"], "tool_stop");
    assert_eq!(
        completion.metadata()["usage"],
        json!({"input_tokens": 12, "output_tokens": 4})
    );
    assert_eq!(
        completion.metadata()["events"],
        json!([{
            "type": "runtime.effect.started",
            "source": "capability:test.effect",
            "payload": {},
            "idempotency_key": "runtime.effect.started:effect-1",
            "caused_by": "call-1",
            "session_id": "session-1",
            "run_id": "run-1",
            "turn_id": "turn-1",
        }])
    );
    assert_eq!(
        completion.controls(),
        &[
            AttemptControl::publish(
                "pub-first",
                "work.first",
                object(json!({"position": 0})),
                None,
                0,
            ),
            AttemptControl::wait(
                "wait-middle",
                "work.ready",
                object(json!({"work_id": "42"})),
                Some("2030-01-02T03:04:05Z".to_owned()),
                1,
            ),
            AttemptControl::cancel(
                "wait-old",
                Some("superseded".to_owned()),
                "worker",
                "agent/worker/session",
                2,
            ),
        ]
    );
    assert!(!completion.metadata().contains_key("publish_event_requests"));
}

#[test]
fn agent_result_records_the_maximum_model_call_stop_reason() {
    let result = AgentRunResult {
        stop_reason: Some(RunStopReason::MaxModelCalls),
        ..AgentRunResult::default()
    };

    let completion = attempt_completion("2026-08-12T10:00:01Z", &result).unwrap();

    assert_eq!(completion.metadata()["stop_reason"], "max_model_calls");
}

#[test]
fn agent_result_preserves_null_draft_event_evidence_fields() {
    let result = AgentRunResult {
        events: vec![DraftEvent {
            event_type: "trace.empty".to_owned(),
            source: "agent:worker".to_owned(),
            payload: object(json!({"nested": {"value": 1}})),
            idempotency_key: None,
            caused_by: None,
            session_id: None,
            run_id: None,
            turn_id: None,
        }],
        ..AgentRunResult::default()
    };

    let completion = attempt_completion("2026-08-12T10:00:01Z", &result).unwrap();

    assert_eq!(
        completion.metadata()["events"],
        json!([{
            "type": "trace.empty",
            "source": "agent:worker",
            "payload": {"nested": {"value": 1}},
            "idempotency_key": null,
            "caused_by": null,
            "session_id": null,
            "run_id": null,
            "turn_id": null,
        }])
    );
}

#[test]
fn agent_result_omits_empty_evidence_controls_and_usage() {
    let result = AgentRunResult {
        final_answer: "done".to_owned(),
        ..AgentRunResult::default()
    };

    let completion = attempt_completion("2026-08-12T10:00:01Z", &result).unwrap();

    assert_eq!(
        completion.metadata(),
        &object(json!({"final_answer": "done"}))
    );
    assert!(completion.controls().is_empty());
}

#[test]
fn agent_result_rejects_non_object_usage() {
    let mut result = AgentRunResult::default();
    result.telemetry.insert("usage".to_owned(), json!(12));

    let error = attempt_completion("2026-08-12T10:00:01Z", &result).unwrap_err();

    assert_eq!(error.kind(), CompletionHandoffErrorKind::MalformedUsage);
    assert_eq!(error.reason(), "malformed_usage");
}

#[test]
fn agent_result_rejects_unsupported_content_promotion() {
    let result = AgentRunResult {
        proposals: vec![AgentProposal::ContentPromotion {
            scope: "agent/worker/session".to_owned(),
            key: "answer".to_owned(),
            object_id: Some("obj-final".to_owned()),
            expected_head: None,
            expected_object_id: None,
            source_head: "head-run".to_owned(),
            reason: "selected final answer".to_owned(),
        }],
        ..AgentRunResult::default()
    };

    let error = attempt_completion("2026-08-12T10:00:01Z", &result).unwrap_err();

    assert_eq!(
        error.kind(),
        CompletionHandoffErrorKind::UnsupportedContentPromotion
    );
    assert_eq!(error.reason(), "unsupported_content_promotion");
}

#[test]
fn agent_result_completion_commits_typed_controls_through_dispatch() {
    let mut dispatch = Dispatch::open_in_memory().unwrap();
    let input = Event {
        id: "evt-host-completion".to_owned(),
        event_type: "work.requested".to_owned(),
        source: "test".to_owned(),
        payload: Map::new(),
        idempotency_key: Some("ingress:host-completion".to_owned()),
        caused_by: None,
        session_id: None,
        run_id: None,
        turn_id: Some("turn-host-completion".to_owned()),
        timestamp_ms: 100,
        cursor: None,
    };
    dispatch.ingest_event(input).unwrap();
    let route = Route::new(
        "worker",
        vec![EventPattern::exact("work.requested")],
        SessionRule::PerEvent,
        Vec::new(),
        Some("generation-1".to_owned()),
    );
    dispatch
        .route_ingress_event(
            "evt-host-completion",
            &[route],
            &[RuntimeEventIdentity::new("host-available", 101).unwrap()],
        )
        .unwrap();
    let claim = dispatch
        .claim_next_queue_item(
            "local",
            ClaimToken::new("host-completion-token").unwrap(),
            1_000,
            200,
        )
        .unwrap()
        .unwrap();
    dispatch
        .start_claimed_attempt(
            &claim,
            201,
            RuntimeEventIdentity::new("host-claimed", 201).unwrap(),
            RuntimeEventIdentity::new("host-started", 202).unwrap(),
            "2026-08-12T10:00:00Z",
            None,
        )
        .unwrap();
    let result = AgentRunResult {
        final_answer: "done".to_owned(),
        stop_reason: Some(RunStopReason::ToolStop),
        proposals: vec![
            AgentProposal::Publish {
                handle: "pub-host".to_owned(),
                event_type: "work.completed".to_owned(),
                payload: object(json!({"work_id": "42"})),
                at: None,
                position: 0,
            },
            AgentProposal::Wait {
                handle: "wait-host".to_owned(),
                event_type: "review.completed".to_owned(),
                fields: object(json!({"work_id": "42"})),
                deadline: None,
                position: 1,
            },
        ],
        ..AgentRunResult::default()
    };
    let completion = attempt_completion("2026-08-12T10:00:01Z", &result).unwrap();

    let events = dispatch
        .complete_claimed_attempt(
            &claim,
            203,
            &[
                RuntimeEventIdentity::new("host-completed", 203).unwrap(),
                RuntimeEventIdentity::new("host-published", 204).unwrap(),
                RuntimeEventIdentity::new("host-wait-created", 205).unwrap(),
                RuntimeEventIdentity::new("host-queue-completed", 206).unwrap(),
            ],
            &completion,
        )
        .unwrap();

    let mut event_types = Vec::new();
    for event in &events {
        event_types.push(event.event_type.as_str());
    }
    assert_eq!(
        event_types,
        [
            "runtime.attempt.completed",
            "work.completed",
            "runtime.wait.created",
            "runtime.queue_item.completed",
        ]
    );
    assert_eq!(
        events[0].payload["result"]["publish_event_requests"][0]["handle"],
        "pub-host"
    );
    assert_eq!(
        events[0].payload["result"]["wait_requests"][0]["handle"],
        "wait-host"
    );
    assert_eq!(events[0].payload["summary"], "done");
    assert_eq!(
        dispatch.list_waits().unwrap()[0].status(),
        WaitStatus::Active
    );
    assert_eq!(
        dispatch
            .queue_item(claim.queue_item_id())
            .unwrap()
            .unwrap()
            .status(),
        QueueItemStatus::Completed
    );
}

#[test]
fn prepared_agent_preserves_the_verified_authored_projection() {
    let work_schema = object(json!({
        "type": "object",
        "required": ["result"],
        "properties": {"result": {"type": "string"}},
        "additionalProperties": false,
    }));
    let mut events = EventRegistry::new();
    events
        .register("work.completed", Some(work_schema.clone()))
        .expect("the typed publish event must register");
    events
        .register("audit.signal", None)
        .expect("the schema-less publish event must register");
    let capabilities = vec![
        capability(
            "provider.unsafe",
            "unsafe-call",
            Some(AuthoredDeliverySemantics::UnsafeToRetry),
        ),
        capability(
            "provider.deduplicated",
            "deliver",
            Some(AuthoredDeliverySemantics::ConnectorDeduplicated),
        ),
        capability(
            "provider.idempotent",
            "retry-safe",
            Some(AuthoredDeliverySemantics::IdempotentWithKey),
        ),
        capability(
            "provider.at-least-once",
            "replay",
            Some(AuthoredDeliverySemantics::AtLeastOnce),
        ),
    ];
    let model = selected_model(
        "project-model",
        "https://project.example/v1/responses",
        "codex-responses",
        "codex",
    );
    let declarations = "model:\n  name: agent-model\n  url: https://agent.example/v1/responses\ntools: [unsafe-call, deliver, retry-safe, replay]\npublishes: [work.completed, audit.signal]\nbase_dir: agent-work\n";
    let (project, execution) = manifest_pair(declarations, events, capabilities, Some(model));

    verify_execution_manifest(&execution, &project)
        .expect("the fixture must be a verified project/execution pair");
    let prepared = prepare_agent(&project, &execution).expect("the verified pair must prepare");

    assert_eq!(prepared.execution_manifest_id(), execution.id);
    assert_eq!(prepared.project_generation_id(), project.id);
    assert_eq!(prepared.agent_slug(), "worker");
    assert_eq!(prepared.agent_description(), "Routes authored work.");
    let ExecutorSelection {
        provider_id,
        implementation: provider_implementation,
        config,
    } = prepared.executor_selection();
    assert_eq!(provider_id, "scripted");
    assert_eq!(provider_implementation, &implementation(2));
    assert_eq!(config, &object(json!({"mode": "fixture"})));

    let mut ids = Vec::new();
    let mut names = Vec::new();
    let mut semantics = Vec::new();
    for ResolvedCapability {
        canonical,
        model_name,
        model_description,
        model_input_schema,
        argument_adapter,
    } in prepared.capabilities()
    {
        let Capability {
            id,
            description,
            input_schema,
            delivery_semantics,
        } = canonical;
        ids.push(id.as_str());
        names.push(model_name.as_str());
        semantics.push(*delivery_semantics);
        assert_eq!(model_description, description);
        assert_eq!(model_input_schema, input_schema);
        assert_eq!(argument_adapter, &ArgumentAdapter::Identity);
    }
    assert_eq!(
        ids,
        [
            "provider.unsafe",
            "provider.deduplicated",
            "provider.idempotent",
            "provider.at-least-once",
        ]
    );
    assert_eq!(names, ["unsafe-call", "deliver", "retry-safe", "replay"]);
    assert_eq!(
        semantics,
        [
            Some(AgentDeliverySemantics::UnsafeToRetry),
            Some(AgentDeliverySemantics::ConnectorDeduplicated),
            Some(AgentDeliverySemantics::IdempotentWithKey),
            Some(AgentDeliverySemantics::AtLeastOnce),
        ]
    );

    let project_directory = PathBuf::from("/projects/zeta");
    let invocation = prepared
        .invocation(invocation_inputs(project_directory.clone()))
        .expect("the invocation inputs must resolve");
    let AgentInvocation {
        objective,
        timeline,
        context,
        system_prompt,
        allowed_capabilities,
        tool_profile,
        max_model_calls,
        model_name,
        model_url,
        model_api,
        thinking,
        model_session_id,
        max_tokens,
        tool_choice,
        base_directory,
        effect_scope,
        source_queue_item_id,
        source_agent_id,
        source_session_id,
        caused_by,
        event_source,
        session_id,
        run_id,
        turn_id,
        environment,
        prompt_transform,
        compaction_threshold_tokens,
        deadline_ms,
        publishable_events,
    } = invocation;
    assert_eq!(objective, "Deliver the requested work.");
    assert_eq!(
        timeline,
        [object(json!({
            "type": "work.requested",
            "payload": {"text": "hello"},
        }))]
    );
    assert_eq!(context, "Project context.");
    assert_eq!(system_prompt.as_deref(), Some("Routes authored work."));
    let mut allowed_ids = Vec::new();
    for id in &allowed_capabilities {
        allowed_ids.push(id.as_str());
    }
    assert_eq!(allowed_ids, ids);
    assert_eq!(tool_profile, ToolProfile::Codex);
    assert_eq!(max_model_calls, 7);
    assert_eq!(model_name.as_deref(), Some("agent-model"));
    assert_eq!(
        model_url.as_deref(),
        Some("https://agent.example/v1/responses")
    );
    assert_eq!(model_api.as_deref(), Some("codex-responses"));
    assert_eq!(thinking.as_deref(), Some("high"));
    assert_eq!(model_session_id.as_deref(), Some("model-session-1"));
    assert_eq!(max_tokens, 512);
    assert_eq!(
        tool_choice,
        json!({"type": "function", "function": {"name": "deliver"}})
    );
    let expected_directory = project_directory.join("agent-work");
    let expected_directory = expected_directory.display().to_string();
    assert_eq!(base_directory.as_deref(), Some(expected_directory.as_str()));
    assert_eq!(effect_scope.as_deref(), Some("attempt-1"));
    assert_eq!(source_queue_item_id.as_deref(), Some("queue-item-1"));
    assert_eq!(source_agent_id.as_deref(), Some("worker"));
    assert_eq!(source_session_id.as_deref(), Some("source-session-1"));
    assert_eq!(caused_by.as_deref(), Some("event-1"));
    assert_eq!(event_source, "agent:worker");
    assert_eq!(session_id.as_deref(), Some("session-1"));
    assert_eq!(run_id.as_deref(), Some("run-1"));
    assert_eq!(turn_id.as_deref(), Some("turn-1"));
    let PromptEnvironment {
        working_directory,
        calendar_date,
    } = environment;
    assert_eq!(working_directory, expected_directory);
    assert_eq!(calendar_date, "2026-08-12");
    assert_eq!(
        prompt_transform,
        PromptTransform::StructuralTrim {
            max_content_chars: 4_096,
        }
    );
    assert_eq!(compaction_threshold_tokens, Some(8_000));
    assert_eq!(deadline_ms, Some(1_800_000_000_000));
    let mut expected_publishable_events = Map::new();
    expected_publishable_events.insert("work.completed".to_owned(), Value::Object(work_schema));
    expected_publishable_events.insert("audit.signal".to_owned(), Value::Null);
    assert_eq!(publishable_events, expected_publishable_events);
}

#[test]
fn preparation_rejects_tampered_execution_and_project_manifests() {
    let (project, execution) = manifest_pair("", EventRegistry::new(), Vec::new(), None);
    let mut tampered_execution = execution.clone();
    tampered_execution.agent.description = "Tampered.".to_owned();

    let error = match prepare_agent(&project, &tampered_execution) {
        Ok(prepared) => panic!(
            "tampered execution unexpectedly prepared for {}",
            prepared.agent_slug()
        ),
        Err(error) => error,
    };
    assert_eq!(error.kind(), PrepareAgentErrorKind::InvalidManifest);

    let mut tampered_project = project;
    tampered_project
        .agents
        .get_mut("worker")
        .expect("the worker must be present")
        .description = "Tampered.".to_owned();
    let error = match prepare_agent(&tampered_project, &execution) {
        Ok(prepared) => panic!(
            "tampered project unexpectedly prepared for {}",
            prepared.agent_slug()
        ),
        Err(error) => error,
    };
    assert_eq!(error.kind(), PrepareAgentErrorKind::InvalidManifest);
}

#[test]
fn preparation_rejects_model_facing_alias_collisions_after_profile_adaptation() {
    let bash = capability_with_schema(
        "zeta.bash",
        "shell",
        None,
        object(json!({
            "type": "object",
            "required": ["command"],
            "properties": {"command": {"type": "string"}},
            "additionalProperties": false,
        })),
    );
    let command = capability("provider.command", "exec_command", None);
    let model = selected_model(
        "codex-model",
        "https://model.example/v1/responses",
        "codex-responses",
        "codex",
    );
    let declarations = "tools: [shell, exec_command]\n";
    let (project, execution) = manifest_pair(
        declarations,
        EventRegistry::new(),
        vec![bash, command],
        Some(model),
    );

    let error = match prepare_agent(&project, &execution) {
        Ok(prepared) => panic!(
            "ambiguous aliases unexpectedly prepared for {}",
            prepared.agent_slug()
        ),
        Err(error) => error,
    };

    assert_eq!(error.kind(), PrepareAgentErrorKind::DuplicateToolName);
}

#[test]
fn model_selection_uses_agent_overrides_and_project_fallbacks() {
    let project_model = selected_model(
        "project-model",
        "https://project.example/v1/responses",
        "codex-responses",
        "codex",
    );
    let declarations = "model:\n  name: agent-model\n  url: https://agent.example/v1/responses\n";
    let (project, execution) = manifest_pair(
        declarations,
        EventRegistry::new(),
        Vec::new(),
        Some(project_model.clone()),
    );
    let prepared = prepare_agent(&project, &execution).expect("the model override must prepare");
    let invocation = prepared
        .invocation(invocation_inputs(PathBuf::from("/projects/override")))
        .expect("the model override invocation must resolve");

    assert_eq!(invocation.model_name.as_deref(), Some("agent-model"));
    assert_eq!(
        invocation.model_url.as_deref(),
        Some("https://agent.example/v1/responses")
    );
    assert_eq!(invocation.model_api.as_deref(), Some("codex-responses"));
    assert_eq!(invocation.thinking.as_deref(), Some("high"));
    assert_eq!(invocation.tool_profile, ToolProfile::Codex);

    let (project, execution) =
        manifest_pair("", EventRegistry::new(), Vec::new(), Some(project_model));
    let prepared = prepare_agent(&project, &execution).expect("the project model must prepare");
    let invocation = prepared
        .invocation(invocation_inputs(PathBuf::from("/projects/fallback")))
        .expect("the project model invocation must resolve");

    assert_eq!(invocation.model_name.as_deref(), Some("project-model"));
    assert_eq!(
        invocation.model_url.as_deref(),
        Some("https://project.example/v1/responses")
    );
    assert_eq!(invocation.model_api.as_deref(), Some("codex-responses"));
    assert_eq!(invocation.thinking.as_deref(), Some("high"));
    assert_eq!(invocation.tool_profile, ToolProfile::Codex);
}

#[test]
fn model_selection_uses_native_chat_defaults_without_a_project_model() {
    let declarations =
        "model:\n  name: agent-model\n  url: https://agent.example/v1/chat/completions\n";
    let (project, execution) = manifest_pair(declarations, EventRegistry::new(), Vec::new(), None);
    let prepared = prepare_agent(&project, &execution).expect("the agent model must prepare");
    let invocation = prepared
        .invocation(invocation_inputs(PathBuf::from("/projects/agent-only")))
        .expect("the agent-only model invocation must resolve");

    assert_eq!(invocation.model_name.as_deref(), Some("agent-model"));
    assert_eq!(
        invocation.model_url.as_deref(),
        Some("https://agent.example/v1/chat/completions")
    );
    assert_eq!(invocation.model_api.as_deref(), Some("chat-completions"));
    assert_eq!(invocation.thinking, None);
    assert_eq!(invocation.tool_profile, ToolProfile::Native);

    let (project, execution) = manifest_pair("", EventRegistry::new(), Vec::new(), None);
    let prepared = prepare_agent(&project, &execution).expect("the default model must prepare");
    let invocation = prepared
        .invocation(invocation_inputs(PathBuf::from("/projects/no-model")))
        .expect("the default model invocation must resolve");

    assert_eq!(invocation.model_name, None);
    assert_eq!(invocation.model_url, None);
    assert_eq!(invocation.model_api.as_deref(), Some("chat-completions"));
    assert_eq!(invocation.thinking, None);
    assert_eq!(invocation.tool_profile, ToolProfile::Native);
}

#[test]
fn invocation_resolves_every_authored_directory_branch() {
    let cases = [
        (
            "base_dir: /srv/authored\n",
            Some("/srv/caller"),
            Some("/users/remi"),
            PathBuf::from("/srv/caller"),
        ),
        (
            "base_dir: /srv/authored\n",
            None,
            Some("/users/remi"),
            PathBuf::from("/srv/authored"),
        ),
        (
            "base_dir: '~'\n",
            None,
            Some("/users/remi"),
            PathBuf::from("/users/remi"),
        ),
        (
            "base_dir: ~/vault\n",
            None,
            Some("/users/remi"),
            PathBuf::from("/users/remi/vault"),
        ),
        (
            "base_dir: agents/worker\n",
            None,
            Some("/users/remi"),
            PathBuf::from("/projects/zeta/agents/worker"),
        ),
        (
            "",
            None,
            Some("/users/remi"),
            PathBuf::from("/projects/zeta"),
        ),
    ];

    for (declarations, override_directory, home_directory, expected) in cases {
        let (project, execution) =
            manifest_pair(declarations, EventRegistry::new(), Vec::new(), None);
        let prepared = prepare_agent(&project, &execution)
            .expect("the directory fixture must prepare successfully");
        let mut inputs = invocation_inputs(PathBuf::from("/projects/zeta"));
        inputs.base_directory_override = override_directory.map(PathBuf::from);
        inputs.home_directory = home_directory.map(PathBuf::from);

        let invocation = prepared
            .invocation(inputs)
            .expect("the directory must resolve without process state");
        let expected = expected.display().to_string();

        assert_eq!(
            invocation.base_directory.as_deref(),
            Some(expected.as_str()),
            "{declarations:?}"
        );
        assert_eq!(
            invocation.environment.working_directory, expected,
            "{declarations:?}"
        );
    }
}

#[test]
fn home_relative_directory_requires_an_explicit_home() {
    let (project, execution) = manifest_pair(
        "base_dir: ~/vault\n",
        EventRegistry::new(),
        Vec::new(),
        None,
    );
    let prepared = prepare_agent(&project, &execution).expect("the home path must prepare");
    let mut inputs = invocation_inputs(PathBuf::from("/projects/zeta"));
    inputs.home_directory = None;

    let error = match prepared.invocation(inputs) {
        Ok(invocation) => panic!(
            "home-relative path unexpectedly resolved as {:?}",
            invocation.base_directory
        ),
        Err(error) => error,
    };

    assert_eq!(error.kind(), PrepareAgentErrorKind::MissingHomeDirectory);
}

#[cfg(unix)]
#[test]
fn invocation_rejects_a_non_utf8_resolved_directory() {
    use std::ffi::OsString;
    use std::os::unix::ffi::OsStringExt;

    let (project, execution) = manifest_pair("", EventRegistry::new(), Vec::new(), None);
    let prepared = prepare_agent(&project, &execution).expect("the default path must prepare");
    let mut inputs = invocation_inputs(PathBuf::from("/projects/zeta"));
    inputs.base_directory_override = Some(PathBuf::from(OsString::from_vec(vec![b'/', 0xff])));

    let error = match prepared.invocation(inputs) {
        Ok(invocation) => panic!(
            "non-UTF-8 path unexpectedly resolved as {:?}",
            invocation.base_directory
        ),
        Err(error) => error,
    };

    assert_eq!(error.kind(), PrepareAgentErrorKind::NonUtf8Directory);
}

#[test]
fn scheduler_rejects_invalid_calendar_declarations_before_writing() {
    let cases = [
        (
            "schedules:\n  - cron: '* * * * * *'\n    timezone: UTC\n",
            SchedulerErrorKind::InvalidCron,
            "* * * * * *",
        ),
        (
            "schedules:\n  - cron: '61 * * * *'\n    timezone: UTC\n",
            SchedulerErrorKind::InvalidCron,
            "61 * * * *",
        ),
        (
            "schedules:\n  - cron: '* * * * *'\n    timezone: Mars/Olympus\n",
            SchedulerErrorKind::UnknownTimezone,
            "Mars/Olympus",
        ),
    ];

    for (declarations, expected_kind, expected_detail) in cases {
        let manifest = scheduled_manifest(declarations);
        let error = Scheduler::from_project(&manifest)
            .expect_err("the invalid calendar declaration must fail before a tick");

        assert_eq!(error.kind(), expected_kind);
        assert_eq!(error.reason(), expected_kind.reason());
        assert!(error.detail().contains(expected_detail));
    }
}

#[test]
fn scheduler_emits_the_current_utc_minute_and_exact_audit_fact() {
    let manifest = scheduled_manifest("schedules:\n  - cron: '* * * * *'\n    timezone: UTC\n");
    let scheduler = Scheduler::from_project(&manifest).expect("the schedule must compile");
    let mut dispatch = Dispatch::open_in_memory().expect("the journal must open");
    let mut ids = ScriptedIds::new(&["scheduled-current", "decision-current"]);
    let now_ms = 1_786_615_633_000;

    let requested = scheduler
        .tick(&mut dispatch, now_ms, &mut ids)
        .expect("the current minute must tick");

    assert_eq!(requested.len(), 1);
    let occurrence = &requested[0];
    assert_eq!(occurrence.id, "scheduled-current");
    assert_eq!(occurrence.event_type, "agent.worker.scheduled");
    assert_eq!(occurrence.source, "zeta:scheduler");
    assert_eq!(
        occurrence.payload,
        object(json!({
            "date": "2026-08-13",
            "timestamp": "2026-08-13T10:07:00+00:00",
        }))
    );
    assert_eq!(
        occurrence.idempotency_key.as_deref(),
        Some("schedule:worker:* * * * *:2026-08-13T10:07:00+00:00")
    );
    assert_eq!(occurrence.caused_by, None);
    assert_eq!(occurrence.timestamp_ms, now_ms);

    let events = dispatch
        .list_events(&EventFilter::default())
        .expect("the scheduler facts must be readable");
    assert_eq!(events.len(), 2);
    let decision = &events[1];
    assert_eq!(decision.id, "decision-current");
    assert_eq!(decision.event_type, "zeta.scheduler.tick.published");
    assert_eq!(decision.source, "zeta:scheduler");
    assert_eq!(decision.caused_by.as_deref(), Some("scheduled-current"));
    assert_eq!(
        decision.idempotency_key.as_deref(),
        Some("scheduler:published:worker:0:* * * * *:UTC:2026-08-13T10:07:00+00:00")
    );
    assert_eq!(
        decision.payload,
        object(json!({
            "agent": "worker",
            "schedule_index": 0,
            "event_type": "agent.worker.scheduled",
            "cron": "* * * * *",
            "timezone": "UTC",
            "scheduled_at": "2026-08-13T10:07:00+00:00",
            "observed_at": "2026-08-13T10:07:13+00:00",
            "next_at": "2026-08-13T10:08:00+00:00",
            "status": "published",
            "reason": "due now",
            "published_event_id": "scheduled-current",
        }))
    );
    assert_eq!(
        dispatch
            .unrouted_ingress_events()
            .expect("unrouted ingress must remain discoverable"),
        ["scheduled-current"]
    );
    assert_eq!(dispatch.list_queue_items().expect("queue items").len(), 1);
}

#[test]
fn latest_schedule_activates_before_publishing() {
    let manifest = scheduled_manifest(
        "schedules:\n  - cron: '* * * * *'\n    timezone: UTC\n    catchup: latest\n",
    );
    let scheduler = Scheduler::from_project(&manifest).expect("the schedule must compile");
    let mut dispatch = Dispatch::open_in_memory().expect("the journal must open");
    let mut ids = ScriptedIds::new(&["activation-first", "scheduled-first", "decision-first"]);

    let requested = scheduler
        .tick(&mut dispatch, 1_786_615_530_000, &mut ids)
        .expect("the latest schedule must tick");

    assert_eq!(requested.len(), 1);
    let events = dispatch
        .list_events(&EventFilter::default())
        .expect("the scheduler facts must be readable");
    let mut event_types = Vec::new();
    for event in &events {
        event_types.push(event.event_type.as_str());
    }
    assert_eq!(
        event_types,
        [
            "zeta.scheduler.tick.activated",
            "agent.worker.scheduled",
            "zeta.scheduler.tick.published",
        ]
    );
    let activation = &events[0];
    assert_eq!(
        activation.idempotency_key.as_deref(),
        Some("scheduler:activated:worker:0:* * * * *:UTC:latest")
    );
    assert_eq!(
        activation.payload,
        object(json!({
            "agent": "worker",
            "schedule_index": 0,
            "event_type": "agent.worker.scheduled",
            "cron": "* * * * *",
            "timezone": "UTC",
            "catchup": "latest",
            "observed_at": "2026-08-13T10:05:30+00:00",
            "status": "activated",
            "reason": "schedule first observed",
        }))
    );
}

#[test]
fn duplicate_scheduler_tick_records_one_skipped_decision() {
    let manifest = scheduled_manifest("schedules:\n  - cron: '* * * * *'\n    timezone: UTC\n");
    let scheduler = Scheduler::from_project(&manifest).expect("the schedule must compile");
    let mut dispatch = Dispatch::open_in_memory().expect("the journal must open");
    let mut ids = ScriptedIds::new(&[
        "scheduled-first",
        "published-first",
        "scheduled-retry",
        "skipped-retry",
        "scheduled-later-retry",
        "skipped-later-retry",
    ]);
    let now_ms = 1_786_615_633_000;

    let first = scheduler
        .tick(&mut dispatch, now_ms, &mut ids)
        .expect("the first tick must publish");
    let retry = scheduler
        .tick(&mut dispatch, now_ms, &mut ids)
        .expect("the retry must resolve its duplicate");
    let later_retry = scheduler
        .tick(&mut dispatch, now_ms, &mut ids)
        .expect("the complete retry must remain idempotent");

    assert_eq!(first.len(), 1);
    assert!(retry.is_empty());
    assert!(later_retry.is_empty());
    let events = dispatch
        .list_events(&EventFilter::default())
        .expect("the scheduler facts must be readable");
    assert_eq!(events.len(), 3);
    assert_eq!(events[2].event_type, "zeta.scheduler.tick.skipped");
    assert_eq!(events[2].caused_by.as_deref(), Some("scheduled-first"));
    assert_eq!(
        events[2].idempotency_key.as_deref(),
        Some("scheduler:skipped:worker:0:* * * * *:UTC:2026-08-13T10:07:00+00:00")
    );
    assert_eq!(events[2].payload["status"], "skipped");
    assert_eq!(events[2].payload["reason"], "already published");
    assert_eq!(events[2].payload["published_event_id"], "scheduled-first");
}

#[test]
fn two_scheduler_handles_resolve_one_occurrence_key() {
    let manifest = scheduled_manifest("schedules:\n  - cron: '* * * * *'\n    timezone: UTC\n");
    let scheduler = Scheduler::from_project(&manifest).expect("the schedule must compile");
    let directory = tempdir().expect("the scheduler directory must exist");
    let path = directory.path().join("scheduler-race.sqlite3");
    drop(Dispatch::open(&path).expect("the shared journal must initialize"));
    let barrier = Arc::new(Barrier::new(3));
    let mut threads = Vec::new();
    for number in 1..=2 {
        let scheduler = scheduler.clone();
        let path = path.clone();
        let barrier = Arc::clone(&barrier);
        threads.push(thread::spawn(move || {
            let mut dispatch = Dispatch::open(path).expect("the scheduler handle must open");
            let occurrence = format!("scheduled-race-{number}");
            let decision = format!("decision-race-{number}");
            let mut ids = ScriptedIds::new(&[&occurrence, &decision]);
            barrier.wait();
            scheduler
                .tick(&mut dispatch, 1_786_615_633_000, &mut ids)
                .expect("the concurrent tick must resolve")
        }));
    }
    barrier.wait();
    let mut published = 0;
    for thread in threads {
        published += thread
            .join()
            .expect("the concurrent scheduler must finish")
            .len();
    }

    assert_eq!(published, 1);
    let dispatch = Dispatch::open(&path).expect("the shared journal must reopen");
    let events = dispatch
        .list_events(&EventFilter::default())
        .expect("the scheduler facts must be readable");
    let occurrences: Vec<_> = events
        .iter()
        .filter(|event| event.event_type == "agent.worker.scheduled")
        .collect();
    let published_decisions: Vec<_> = events
        .iter()
        .filter(|event| event.event_type == "zeta.scheduler.tick.published")
        .collect();
    let skipped_decisions: Vec<_> = events
        .iter()
        .filter(|event| event.event_type == "zeta.scheduler.tick.skipped")
        .collect();
    assert_eq!(occurrences.len(), 1);
    assert_eq!(published_decisions.len(), 1);
    assert!(skipped_decisions.len() <= 1);
    for decision in published_decisions.iter().chain(&skipped_decisions) {
        assert_eq!(
            decision.caused_by.as_deref(),
            Some(occurrences[0].id.as_str())
        );
    }
    assert_eq!(dispatch.list_queue_items().expect("queue items").len(), 1);
}

#[test]
fn scheduler_reuses_a_retained_activation_after_interruption() {
    let manifest = scheduled_manifest(
        "schedules:\n  - cron: '* * * * *'\n    timezone: UTC\n    catchup: latest\n",
    );
    let scheduler = Scheduler::from_project(&manifest).expect("the schedule must compile");
    let mut dispatch = Dispatch::open_in_memory().expect("the journal must open");
    dispatch
        .append_trusted_event(Event {
            id: "activation-retained".to_owned(),
            event_type: "zeta.scheduler.tick.activated".to_owned(),
            source: "zeta:scheduler".to_owned(),
            payload: object(json!({
                "agent": "worker",
                "schedule_index": 0,
                "event_type": "agent.worker.scheduled",
                "cron": "* * * * *",
                "timezone": "UTC",
                "catchup": "latest",
                "observed_at": "2026-08-13T10:05:30+00:00",
                "status": "activated",
                "reason": "schedule first observed",
            })),
            idempotency_key: Some("scheduler:activated:worker:0:* * * * *:UTC:latest".to_owned()),
            caused_by: None,
            session_id: None,
            run_id: None,
            turn_id: None,
            timestamp_ms: 1_786_615_530_000,
            cursor: None,
        })
        .expect("the retained activation must be seeded");
    let mut ids = ScriptedIds::new(&[
        "activation-retry",
        "scheduled-after-activation",
        "decision-after-activation",
    ]);

    let requested = scheduler
        .tick(&mut dispatch, 1_786_615_570_000, &mut ids)
        .expect("the interrupted tick must resume");

    assert_eq!(requested.len(), 1);
    let events = dispatch
        .list_events(&EventFilter::default())
        .expect("the scheduler facts must be readable");
    assert_eq!(events.len(), 3);
    assert_eq!(events[0].id, "activation-retained");
    assert_eq!(events[1].event_type, "agent.worker.scheduled");
    assert_eq!(events[2].payload["status"], "published");
}

#[test]
fn scheduler_repairs_an_occurrence_without_a_decision() {
    let manifest = scheduled_manifest("schedules:\n  - cron: '* * * * *'\n    timezone: UTC\n");
    let scheduler = Scheduler::from_project(&manifest).expect("the schedule must compile");
    let mut dispatch = Dispatch::open_in_memory().expect("the journal must open");
    dispatch
        .ingest_event(Event {
            id: "scheduled-retained".to_owned(),
            event_type: "agent.worker.scheduled".to_owned(),
            source: "zeta:scheduler".to_owned(),
            payload: object(json!({
                "date": "2026-08-13",
                "timestamp": "2026-08-13T10:07:00+00:00",
            })),
            idempotency_key: Some("schedule:worker:* * * * *:2026-08-13T10:07:00+00:00".to_owned()),
            caused_by: None,
            session_id: None,
            run_id: None,
            turn_id: None,
            timestamp_ms: 1_786_615_620_000,
            cursor: None,
        })
        .expect("the retained occurrence must be seeded");
    let mut ids = ScriptedIds::new(&["scheduled-retry", "decision-repair"]);

    let requested = scheduler
        .tick(&mut dispatch, 1_786_615_633_000, &mut ids)
        .expect("the missing decision must be repaired");

    assert!(requested.is_empty());
    let events = dispatch
        .list_events(&EventFilter::default())
        .expect("the scheduler facts must be readable");
    assert_eq!(events.len(), 2);
    assert_eq!(events[1].id, "decision-repair");
    assert_eq!(events[1].event_type, "zeta.scheduler.tick.published");
    assert_eq!(events[1].caused_by.as_deref(), Some("scheduled-retained"));
    assert_eq!(
        events[1].payload["published_event_id"],
        "scheduled-retained"
    );
}

#[test]
fn scheduler_status_is_derived_from_retained_tick_facts() {
    let manifest = scheduled_manifest("schedules:\n  - cron: '* * * * *'\n    timezone: UTC\n");
    let scheduler = Scheduler::from_project(&manifest).expect("the schedule must compile");
    let mut dispatch = Dispatch::open_in_memory().expect("the journal must open");
    let now_ms = 1_786_615_633_000;

    let pending = scheduler
        .status(&dispatch, now_ms)
        .expect("pending status must be readable");
    assert_eq!(
        pending,
        [ScheduleStatus {
            agent: "worker".to_owned(),
            cron: "* * * * *".to_owned(),
            timezone: Some("UTC".to_owned()),
            status: "pending".to_owned(),
            last_published_at: None,
            next_at: "2026-08-13T10:08:00+00:00".to_owned(),
            reason: "next tick is in the future".to_owned(),
        }]
    );

    let mut ids = ScriptedIds::new(&["scheduled-status", "decision-status"]);
    scheduler
        .tick(&mut dispatch, now_ms, &mut ids)
        .expect("the current minute must publish");
    let published = scheduler
        .status(&dispatch, now_ms)
        .expect("published status must be readable");

    assert_eq!(
        published,
        [ScheduleStatus {
            agent: "worker".to_owned(),
            cron: "* * * * *".to_owned(),
            timezone: Some("UTC".to_owned()),
            status: "published".to_owned(),
            last_published_at: Some("2026-08-13T10:07:00+00:00".to_owned()),
            next_at: "2026-08-13T10:08:00+00:00".to_owned(),
            reason: "due now".to_owned(),
        }]
    );
}

#[test]
fn scheduler_runtime_vectors_match_the_python_ground_truth() {
    let document: Value = serde_json::from_str(include_str!(
        "../../../spec/vectors/scheduling/runtime.json"
    ))
    .expect("the scheduling runtime vector must be valid JSON");
    assert_eq!(document["format"], "zeta-scheduling-runtime-v0");
    let cases = document["recurring_schedules"]
        .as_array()
        .expect("the scheduling runtime cases must be an array");

    for case in cases {
        let name = case["name"]
            .as_str()
            .expect("the vector case name must be a string");
        let manifest = scheduling_vector_manifest(case);
        let scheduler = Scheduler::from_project(&manifest)
            .unwrap_or_else(|error| panic!("scheduler vector {name:?} must compile: {error}"));
        let mut dispatch = Dispatch::open_in_memory().expect("the vector journal must open");
        let mut ids = CountingIds::default();
        let ticks = case["ticks"]
            .as_array()
            .expect("the vector ticks must be an array");
        let mut published_per_tick = Vec::new();
        for tick in ticks {
            let tick = tick
                .as_str()
                .expect("the vector tick must be an RFC 3339 string");
            let tick = chrono::DateTime::parse_from_rfc3339(tick)
                .expect("the vector tick must be valid RFC 3339");
            let published = scheduler
                .tick(&mut dispatch, tick.timestamp_millis(), &mut ids)
                .unwrap_or_else(|error| panic!("scheduler vector {name:?} tick failed: {error}"));
            published_per_tick.push(published.len());
        }
        assert_eq!(
            serde_json::to_value(&published_per_tick).expect("the published counts must serialize"),
            case["expected"]["published_per_tick"],
            "scheduler vector {name:?} published counts diverged"
        );

        let events = dispatch
            .list_events(&EventFilter::default())
            .unwrap_or_else(|error| panic!("scheduler vector {name:?} journal failed: {error}"));
        let expected_events = case["expected"]["events"]
            .as_array()
            .expect("the expected scheduler events must be an array");
        assert_eq!(
            events.len(),
            expected_events.len(),
            "scheduler vector {name:?} event count diverged"
        );
        let mut aliases = BTreeMap::new();
        for (event, expected) in events.iter().zip(expected_events) {
            let alias = expected["alias"]
                .as_str()
                .expect("the expected scheduler alias must be a string");
            aliases.insert(event.id.clone(), alias.to_owned());
        }
        let mut contracts = Vec::new();
        for (event, expected) in events.iter().zip(expected_events) {
            let mut contract = json!({
                "alias": expected["alias"],
                "type": event.event_type,
                "idempotency_key": event.idempotency_key,
                "caused_by": event.caused_by,
                "payload": event.payload,
            });
            normalize_scheduler_aliases(&mut contract, &aliases);
            contracts.push(contract);
        }
        assert_eq!(
            Value::Array(contracts),
            case["expected"]["events"],
            "scheduler vector {name:?} event contract diverged"
        );

        let tick = ticks
            .last()
            .and_then(Value::as_str)
            .expect("the vector must have a final RFC 3339 tick");
        let tick = chrono::DateTime::parse_from_rfc3339(tick)
            .expect("the final vector tick must be valid RFC 3339");
        let status = scheduler
            .status(&dispatch, tick.timestamp_millis())
            .unwrap_or_else(|error| panic!("scheduler vector {name:?} status failed: {error}"));
        assert_eq!(
            serde_json::to_value(status).expect("the scheduler status must serialize"),
            case["expected"]["read_model"],
            "scheduler vector {name:?} status diverged"
        );
    }
}
