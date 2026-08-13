use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{json, Map, Value};
use zeta::{
    attempt_completion, prepare_agent, CallbackDraftRecorder, CallbackObserver, CancellationToken,
    CompletionHandoffErrorKind, ExecutorSelection, InvocationInputs, PrepareAgentErrorKind,
    SystemClock, UuidIdSource,
};
use zeta_agent::{
    AbortReason, AbortSignal, AgentErrorKind, AgentInvocation, AgentObserver, AgentProposal,
    AgentRunResult, ArgumentAdapter, Capability, Clock,
    DeliverySemantics as AgentDeliverySemantics, DraftRecorder, IdSource, Observation,
    PromptEnvironment, PromptTransform, ResolvedCapability, RunStopReason, ToolProfile,
};
use zeta_authoring::{
    compile_project, execution_manifest, parse_agent, project_manifest, verify_execution_manifest,
    AgentProjectInput, CapabilitySpec, DeliverySemantics as AuthoredDeliverySemantics,
    EventRegistry, ExecutionManifest, ExecutorProviderSpec, ImplementationFingerprint,
    ModelSelectionSpec, ProjectManifest,
};
use zeta_dispatch::{
    AttemptCompletionDisposition, AttemptControl, ClaimToken, Dispatch, EventPattern,
    QueueItemStatus, Route, RuntimeEventIdentity, SessionRule, WaitStatus,
};
use zeta_journal::{DraftEvent, Event};

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
