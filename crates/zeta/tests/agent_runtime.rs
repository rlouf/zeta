use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Map, Value};
use tempfile::TempDir;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::task::JoinHandle;
use zeta::{
    prepare_agent, CallbackDraftRecorder, CallbackObserver, CancellationToken, ExecutorSelection,
    InvocationInputs, ProcessExecutor, ProcessLaunch, SystemClock, UuidIdSource,
};
use zeta_agent::{
    native_capabilities, resolve_capabilities, AgentInvocation, AgentRunResult, AgentRunner,
    Capability, HttpModelGateway, HttpModelGatewayConfig, ModelHttpEndpoint,
    ModelTransportTimeouts, NativeToolExecutor, Observation, PromptEnvironment, PromptTransform,
    RunStopReason, ToolProfile,
};
use zeta_authoring::{
    compile_project, execution_manifest, parse_agent, project_manifest, verify_execution_manifest,
    AgentProjectInput, CapabilitySpec, EventRegistry, ExecutorProviderSpec,
    ImplementationFingerprint, ModelSelectionSpec,
};
use zeta_dispatch::{route_event, Dispatch, QueueItemStatus, RuntimeEventIdentity};
use zeta_journal::{DraftEvent, Event};

async fn scripted_chat_server(responses: Vec<String>) -> (String, JoinHandle<Vec<Value>>) {
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("the model fixture must bind");
    let address = listener
        .local_addr()
        .expect("the model fixture must have an address");
    let task = tokio::spawn(async move {
        let mut requests = Vec::new();
        for response in responses {
            let accepted = tokio::time::timeout(Duration::from_secs(5), listener.accept())
                .await
                .expect("the model fixture must receive a request")
                .expect("the model fixture must accept a request");
            let (mut socket, _peer) = accepted;
            requests.push(read_request(&mut socket).await);
            let headers = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                response.len(),
            );
            socket
                .write_all(headers.as_bytes())
                .await
                .expect("the model fixture must write headers");
            socket
                .write_all(response.as_bytes())
                .await
                .expect("the model fixture must write its response");
        }
        requests
    });
    (format!("http://{address}/v1/chat/completions"), task)
}

async fn read_request(socket: &mut TcpStream) -> Value {
    let mut request = Vec::new();
    let mut buffer = [0_u8; 1024];
    loop {
        let count = socket
            .read(&mut buffer)
            .await
            .expect("the model fixture must read a request");
        assert!(count > 0, "the request ended before its body arrived");
        request.extend_from_slice(&buffer[..count]);
        let Some(headers_end) = find_bytes(&request, b"\r\n\r\n") else {
            continue;
        };
        let headers = String::from_utf8_lossy(&request[..headers_end]);
        let mut content_length = None;
        for line in headers.lines() {
            let line = line.to_ascii_lowercase();
            let Some(value) = line.strip_prefix("content-length:") else {
                continue;
            };
            content_length = Some(
                value
                    .trim()
                    .parse::<usize>()
                    .expect("content length must be numeric"),
            );
        }
        let content_length = content_length.expect("the request must have a content length");
        let body_start = headers_end + 4;
        if request.len() < body_start + content_length {
            continue;
        }
        return serde_json::from_slice(&request[body_start..body_start + content_length])
            .expect("the model request body must be JSON");
    }
}

fn find_bytes(bytes: &[u8], needle: &[u8]) -> Option<usize> {
    if needle.is_empty() || needle.len() > bytes.len() {
        return None;
    }
    bytes
        .windows(needle.len())
        .position(|window| window == needle)
}

fn tool_call_stream(call_id: &str, name: &str, arguments: Value) -> String {
    let arguments = serde_json::to_string(&arguments).expect("tool arguments must serialize");
    let chunk = json!({
        "id": "chat-tool",
        "choices": [{
            "index": 0,
            "delta": {
                "role": "assistant",
                "tool_calls": [{
                    "index": 0,
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }],
            },
            "finish_reason": "tool_calls",
        }],
    });
    format!("data: {chunk}\n\ndata: [DONE]\n\n")
}

fn answer_stream(parts: &[&str]) -> String {
    let mut stream = String::new();
    for (index, part) in parts.iter().enumerate() {
        let finish_reason = if index + 1 == parts.len() {
            Value::String("stop".to_owned())
        } else {
            Value::Null
        };
        let role = if index == 0 {
            Value::String("assistant".to_owned())
        } else {
            Value::Null
        };
        let chunk = json!({
            "id": "chat-answer",
            "choices": [{
                "index": 0,
                "delta": {"role": role, "content": part},
                "finish_reason": finish_reason,
            }],
        });
        stream.push_str(&format!("data: {chunk}\n\n"));
    }
    stream.push_str("data: [DONE]\n\n");
    stream
}

fn model_gateway(url: String) -> HttpModelGateway {
    let timeouts = ModelTransportTimeouts::new(
        Duration::from_secs(2),
        Duration::from_secs(2),
        Duration::from_secs(5),
    );
    let endpoint = ModelHttpEndpoint::new(url);
    let config = HttpModelGatewayConfig::new(Some(endpoint), None).with_timeouts(timeouts);
    HttpModelGateway::new(config).expect("the model gateway config must be valid")
}

fn invocation(capability_id: &str, base_directory: &Path) -> AgentInvocation {
    let capability_id = capability_id
        .parse()
        .expect("the test capability id must be valid");
    let base_directory = base_directory.display().to_string();
    AgentInvocation {
        objective: "Use the granted capability, then report the result.".to_owned(),
        allowed_capabilities: vec![capability_id],
        model_name: Some("unit-model".to_owned()),
        model_api: Some("chat-completions".to_owned()),
        max_tokens: 128,
        base_directory: Some(base_directory.clone()),
        environment: PromptEnvironment {
            working_directory: base_directory,
            calendar_date: "2026-08-12".to_owned(),
        },
        ..AgentInvocation::default()
    }
}

fn resolved_native(capability_id: &str) -> Vec<zeta_agent::ResolvedCapability> {
    let mut declarations = Vec::new();
    for capability in native_capabilities() {
        if capability.id.as_str() == capability_id {
            declarations.push(capability);
        }
    }
    assert_eq!(declarations.len(), 1, "the native capability must exist");
    resolve_capabilities(&declarations, ToolProfile::Native)
}

fn event_types(result: &AgentRunResult) -> Vec<String> {
    let mut types = Vec::new();
    for event in &result.events {
        types.push(event.event_type.clone());
    }
    types
}

fn tool_result_from_request(request: &Value) -> Value {
    let messages = request["messages"]
        .as_array()
        .expect("the model request must contain messages");
    for message in messages {
        if message["role"] != "tool" {
            continue;
        }
        let content = message["content"]
            .as_str()
            .expect("the tool message content must be text");
        return serde_json::from_str(content).expect("the tool message must contain JSON");
    }
    panic!("the follow-up model request must contain a tool result")
}

fn fixture_path() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("fixtures")
        .join("capability_provider.py")
}

fn authored_implementation(byte: u8) -> ImplementationFingerprint {
    let byte = format!("{byte:02x}");
    let hash = format!("b3:{}", byte.repeat(32));
    serde_json::from_value(Value::String(hash)).expect("the test fingerprint must be valid")
}

#[tokio::test]
async fn runner_streams_http_reads_native_file_and_finishes() {
    let temp = TempDir::new().expect("temporary directory");
    let source = temp.path().join("input.txt");
    fs::write(&source, "ready\n").expect("the source fixture must be writable");
    let responses = vec![
        tool_call_stream("call-read", "read", json!({"path": "input.txt"})),
        answer_stream(&["The file says ", "ready."]),
    ];
    let (url, server) = scripted_chat_server(responses).await;
    let capabilities = resolved_native("zeta.read");
    let invocation = invocation("zeta.read", temp.path());
    let mut gateway = model_gateway(url);
    let mut executor = NativeToolExecutor::default();
    let observations = Arc::new(Mutex::new(Vec::new()));
    let captured_observations = Arc::clone(&observations);
    let mut observer = CallbackObserver::new(move |observation: Observation| {
        captured_observations
            .lock()
            .expect("the observation lock must be available")
            .push(observation);
    });
    let drafts = Arc::new(Mutex::new(Vec::new()));
    let captured_drafts = Arc::clone(&drafts);
    let mut recorder = CallbackDraftRecorder::new(move |draft: &DraftEvent| {
        captured_drafts
            .lock()
            .expect("the draft lock must be available")
            .push(draft.clone());
        Ok::<(), String>(())
    });
    let mut ids = UuidIdSource::new("event");
    let abort = CancellationToken::new();
    let clock = SystemClock;

    let result = AgentRunner::new(
        &capabilities,
        &mut gateway,
        &mut executor,
        &mut observer,
        &mut recorder,
        &mut ids,
        &abort,
        &clock,
    )
    .run(&invocation)
    .await
    .expect("the agent run must succeed");
    let requests = server.await.expect("the model fixture must finish");

    assert_eq!(result.final_answer, "The file says ready.");
    assert_eq!(result.stop_reason, Some(RunStopReason::Finished));
    assert!(result.answer_streamed);
    assert_eq!(
        *observations
            .lock()
            .expect("the observation lock must be available"),
        [
            Observation::TextDelta {
                text: "The file says ".to_owned(),
            },
            Observation::TextDelta {
                text: "ready.".to_owned(),
            },
        ],
    );
    assert_eq!(requests.len(), 2);
    assert_eq!(requests[0]["tools"].as_array().map(Vec::len), Some(1));
    assert_eq!(requests[0]["tools"][0]["function"]["name"], "read");
    let tool_result = tool_result_from_request(&requests[1]);
    assert_eq!(tool_result["ok"], true);
    assert!(tool_result["content"][0]["text"]
        .as_str()
        .expect("the native read must return text")
        .contains("1:ready\n"));
    assert_eq!(
        event_types(&result),
        [
            "zeta.model_call.completed",
            "zeta.tool_call.started",
            "zeta.tool_call.completed",
            "zeta.model_call.completed",
        ],
    );
    assert_eq!(
        *drafts.lock().expect("the draft lock must be available"),
        result.events,
    );
    assert_eq!(
        fs::read_to_string(source).expect("the source fixture must remain readable"),
        "ready\n",
    );
}

#[tokio::test]
async fn runner_records_effect_barriers_around_native_write() {
    let temp = TempDir::new().expect("temporary directory");
    let target = temp.path().join("output.txt");
    let responses = vec![
        tool_call_stream(
            "call-write",
            "write",
            json!({"path": "output.txt", "content": "durable\n"}),
        ),
        answer_stream(&["Written."]),
    ];
    let (url, server) = scripted_chat_server(responses).await;
    let capabilities = resolved_native("zeta.write");
    let mut invocation = invocation("zeta.write", temp.path());
    invocation.effect_scope = Some("attempt-e2e".to_owned());
    let mut gateway = model_gateway(url);
    let mut executor = NativeToolExecutor::default();
    let mut observer = CallbackObserver::new(|_observation: Observation| {});
    let recorded_states = Arc::new(Mutex::new(Vec::new()));
    let captured_states = Arc::clone(&recorded_states);
    let inspected_target = target.clone();
    let mut recorder = CallbackDraftRecorder::new(move |draft: &DraftEvent| {
        captured_states
            .lock()
            .expect("the state lock must be available")
            .push((draft.event_type.clone(), inspected_target.exists()));
        Ok::<(), String>(())
    });
    let mut ids = UuidIdSource::new("event");
    let abort = CancellationToken::new();
    let clock = SystemClock;

    let result = AgentRunner::new(
        &capabilities,
        &mut gateway,
        &mut executor,
        &mut observer,
        &mut recorder,
        &mut ids,
        &abort,
        &clock,
    )
    .run(&invocation)
    .await
    .expect("the agent run must succeed");
    let requests = server.await.expect("the model fixture must finish");

    assert_eq!(requests.len(), 2);
    assert_eq!(
        fs::read_to_string(&target).expect("the native write must create the target"),
        "durable\n",
    );
    assert_eq!(
        *recorded_states
            .lock()
            .expect("the state lock must be available"),
        [
            ("zeta.model_call.completed".to_owned(), false),
            ("zeta.tool_call.started".to_owned(), false),
            ("runtime.effect.planned".to_owned(), false),
            ("runtime.effect.started".to_owned(), false),
            ("zeta.tool_call.completed".to_owned(), true),
            ("runtime.effect.completed".to_owned(), true),
            ("zeta.model_call.completed".to_owned(), true),
        ],
    );
    let mut effect_key = None;
    let mut effect_count = 0;
    for event in &result.events {
        if !event.event_type.starts_with("runtime.effect.") {
            continue;
        }
        effect_count += 1;
        let key = event.payload["effect_key"]
            .as_str()
            .expect("the effect must have a key");
        assert!(key.starts_with("effect:b3:"));
        match &effect_key {
            Some(expected) => assert_eq!(key, expected),
            None => effect_key = Some(key.to_owned()),
        }
        assert_eq!(event.payload["operation"], "zeta.write");
        assert_eq!(event.payload["semantics"], "idempotent_with_key");
        assert_eq!(event.payload["scope"], "attempt-e2e");
        assert_eq!(event.caused_by.as_deref(), Some("call-write"));
    }
    assert_eq!(effect_count, 3);
}

#[tokio::test]
async fn runner_calls_fake_ipc_provider_and_returns_result_to_model() {
    let temp = TempDir::new().expect("temporary directory");
    let marker = temp.path().join("provider-runs");
    let responses = vec![
        tool_call_stream("call-deliver", "deliver", json!({"text": "from-model"})),
        answer_stream(&["Delivered."]),
    ];
    let (url, server) = scripted_chat_server(responses).await;
    let capability = Capability {
        id: "test.deliver"
            .parse()
            .expect("the provider capability id must be valid"),
        description: "Returns the supplied text through IPC.".to_owned(),
        input_schema: object(json!({
            "type": "object",
            "required": ["text"],
            "properties": {"text": {"type": "string"}},
            "additionalProperties": false,
        })),
        delivery_semantics: None,
    };
    let capabilities = resolve_capabilities(&[capability], ToolProfile::Native);
    let invocation = invocation("test.deliver", temp.path());
    let launch = ProcessLaunch {
        extension_id: "test.reference".to_owned(),
        argv: vec![
            "python3".to_owned(),
            fixture_path().display().to_string(),
            marker.display().to_string(),
        ],
        working_directory: None,
        environment: BTreeMap::new(),
    };
    let mut gateway = model_gateway(url);
    let mut executor = ProcessExecutor::new(launch).expect("the process launch must be valid");
    let mut observer = CallbackObserver::new(|_observation: Observation| {});
    let mut recorder = CallbackDraftRecorder::new(|_draft: &DraftEvent| Ok::<(), String>(()));
    let mut ids = UuidIdSource::new("event");
    let abort = CancellationToken::new();
    let clock = SystemClock;

    let result = AgentRunner::new(
        &capabilities,
        &mut gateway,
        &mut executor,
        &mut observer,
        &mut recorder,
        &mut ids,
        &abort,
        &clock,
    )
    .run(&invocation)
    .await
    .expect("the agent run must succeed");
    let requests = server.await.expect("the model fixture must finish");

    assert_eq!(result.final_answer, "Delivered.");
    assert_eq!(requests.len(), 2);
    let tool_result = tool_result_from_request(&requests[1]);
    assert_eq!(tool_result["ok"], true);
    assert_eq!(tool_result["delivered"], "from-model");
    assert_eq!(tool_result["effect_key"], Value::Null);
    assert_eq!(tool_result["base_dir"], temp.path().display().to_string(),);
    assert_eq!(executor.initialization_count(), 1);
    assert!(executor.is_running());
    assert_eq!(
        fs::read_to_string(marker).expect("the provider marker must be readable"),
        "1",
    );
    executor.shutdown().expect("the provider must shut down");
    assert!(!executor.is_running());
}

#[tokio::test]
async fn compiled_authored_agent_routes_an_alias_to_the_canonical_executor_capability() {
    let temp = TempDir::new().expect("temporary directory");
    let marker = temp.path().join("authored-provider-runs");
    let responses = vec![
        tool_call_stream("call-ship", "ship", json!({"text": "from-authored-agent"})),
        answer_stream(&["Delivered through the authored route."]),
    ];
    let (url, server) = scripted_chat_server(responses).await;
    let source = b"---\nname: Worker\ndescription: Delivers work through the scripted provider.\nexecutor:\n  provider: scripted\n  config:\n    route: canonical\ntools: [ship]\n---\nUse the delivery capability.\n";
    let agent = parse_agent("worker", source).expect("the authored agent must parse");
    let provider_implementation = authored_implementation(2);
    let capability = CapabilitySpec {
        id: "test.deliver"
            .parse()
            .expect("the authored capability id must be valid"),
        name: "ship".to_owned(),
        description: "Delivers the supplied text.".to_owned(),
        input_schema: object(json!({
            "type": "object",
            "required": ["text"],
            "properties": {"text": {"type": "string"}},
            "additionalProperties": false,
        })),
        delivery_semantics: None,
        owner: None,
        implementation: authored_implementation(3),
    };
    let project = compile_project(AgentProjectInput {
        agents: vec![agent],
        events: EventRegistry::new(),
        skill_resources: Vec::new(),
        skill_specs: Vec::new(),
        connectors: Vec::new(),
        capabilities: vec![capability],
        executor_providers: vec![ExecutorProviderSpec {
            id: "scripted".to_owned(),
            implementation: provider_implementation.clone(),
        }],
        model: Some(ModelSelectionSpec {
            profile: "integration".to_owned(),
            model: "unit-model".to_owned(),
            url: url.clone(),
            thinking: None,
            api: "chat-completions".to_owned(),
            tool_profile: "native".to_owned(),
            implementation: authored_implementation(4),
        }),
        runtime_fingerprint: authored_implementation(1),
    })
    .expect("the authored project must compile");
    let project_manifest = project_manifest(&project).expect("the project manifest must build");
    let execution = execution_manifest(&project, &project_manifest.id, "worker")
        .expect("the execution manifest must build");
    verify_execution_manifest(&execution, &project_manifest)
        .expect("the authored execution projection must verify");
    let prepared =
        prepare_agent(&project_manifest, &execution).expect("the authored agent must prepare");
    let ExecutorSelection {
        provider_id,
        implementation,
        config,
    } = prepared.executor_selection();
    assert_eq!(provider_id, "scripted");
    assert_eq!(implementation, &provider_implementation);
    assert_eq!(config, &object(json!({"route": "canonical"})));

    let invocation = prepared
        .invocation(InvocationInputs {
            objective: "Deliver one message.".to_owned(),
            timeline: Vec::new(),
            context: "Integration fixture.".to_owned(),
            project_directory: temp.path().to_path_buf(),
            home_directory: None,
            base_directory_override: None,
            calendar_date: "2026-08-12".to_owned(),
            model_session_id: Some("model-session-authored".to_owned()),
            max_model_calls: 3,
            max_tokens: 128,
            tool_choice: Value::String("auto".to_owned()),
            source_queue_item_id: None,
            effect_scope: None,
            source_session_id: Some("source-session-authored".to_owned()),
            caused_by: Some("event-authored".to_owned()),
            event_source: "agent:worker".to_owned(),
            session_id: Some("session-authored".to_owned()),
            run_id: Some("run-authored".to_owned()),
            turn_id: Some("turn-authored".to_owned()),
            prompt_transform: PromptTransform::None,
            compaction_threshold_tokens: None,
            deadline_ms: None,
        })
        .expect("the prepared invocation must resolve");
    assert_eq!(
        invocation.system_prompt.as_deref(),
        Some("Delivers work through the scripted provider.")
    );
    assert_eq!(invocation.source_agent_id.as_deref(), Some("worker"));
    assert_eq!(
        invocation.base_directory.as_deref(),
        Some(temp.path().display().to_string().as_str())
    );
    assert_eq!(prepared.capabilities().len(), 1);
    assert_eq!(prepared.capabilities()[0].model_name, "ship");
    assert_eq!(
        prepared.capabilities()[0].canonical.id.as_str(),
        "test.deliver"
    );

    let launch = ProcessLaunch {
        extension_id: provider_id.clone(),
        argv: vec![
            "python3".to_owned(),
            fixture_path().display().to_string(),
            marker.display().to_string(),
        ],
        working_directory: None,
        environment: BTreeMap::new(),
    };
    let mut gateway = model_gateway(url);
    let mut executor = ProcessExecutor::new(launch).expect("the process launch must be valid");
    let mut observer = CallbackObserver::new(|_observation: Observation| {});
    let mut recorder = CallbackDraftRecorder::new(|_draft: &DraftEvent| Ok::<(), String>(()));
    let mut ids = UuidIdSource::new("event");
    let abort = CancellationToken::new();
    let clock = SystemClock;

    let result = AgentRunner::new(
        prepared.capabilities(),
        &mut gateway,
        &mut executor,
        &mut observer,
        &mut recorder,
        &mut ids,
        &abort,
        &clock,
    )
    .run(&invocation)
    .await
    .expect("the prepared authored agent must run");
    let requests = server.await.expect("the model fixture must finish");

    assert_eq!(result.final_answer, "Delivered through the authored route.");
    assert_eq!(requests.len(), 2);
    assert_eq!(requests[0]["tools"].as_array().map(Vec::len), Some(1));
    assert_eq!(requests[0]["tools"][0]["function"]["name"], "ship");
    let tool_result = tool_result_from_request(&requests[1]);
    assert_eq!(tool_result["ok"], true);
    assert_eq!(tool_result["delivered"], "from-authored-agent");
    assert_eq!(tool_result["base_dir"], temp.path().display().to_string());
    let mut routed_call = None;
    for event in &result.events {
        if event.event_type == "zeta.tool_call.started" {
            routed_call = Some(event);
            break;
        }
    }
    let Some(routed_call) = routed_call else {
        panic!("the run must record its routed tool call")
    };
    assert_eq!(routed_call.payload["name"], "ship");
    assert_eq!(routed_call.payload["capability_id"], "test.deliver");
    assert_eq!(executor.initialization_count(), 1);
    executor.shutdown().expect("the provider must shut down");
}

#[test]
fn authored_project_routes_use_slug_order_and_exact_accepts() {
    let alpha = parse_agent(
        "alpha",
        b"---\nname: Alpha\ndescription: Alpha agent.\naccepts: [work.requested]\nsession: shared\nlocks: [repo:zeta]\n---\n",
    )
    .unwrap();
    let wildcard = parse_agent(
        "wildcard",
        b"---\nname: Wildcard\ndescription: Literal wildcard agent.\naccepts: ['work.*']\n---\n",
    )
    .unwrap();
    let disabled = parse_agent(
        "disabled",
        b"---\nname: Disabled\ndescription: Disabled agent.\nenabled: false\naccepts: [work.requested]\n---\n",
    )
    .unwrap();
    let mut events = EventRegistry::new();
    events.register("work.requested", None).unwrap();
    events.register("work.*", None).unwrap();
    let project = compile_project(AgentProjectInput {
        agents: vec![wildcard, disabled, alpha],
        events,
        skill_resources: Vec::new(),
        skill_specs: Vec::new(),
        connectors: Vec::new(),
        capabilities: Vec::new(),
        executor_providers: vec![ExecutorProviderSpec {
            id: "local".to_owned(),
            implementation: authored_implementation(5),
        }],
        model: None,
        runtime_fingerprint: authored_implementation(6),
    })
    .unwrap();
    let manifest = project_manifest(&project).unwrap();
    let routes = zeta::routes_from_project(&manifest).unwrap();
    let mut agent_ids = Vec::new();
    for route in &routes {
        agent_ids.push(route.agent_id());
    }
    assert_eq!(agent_ids, ["alpha", "wildcard"]);
    let event = Event {
        id: "evt_authored".to_owned(),
        event_type: "work.requested".to_owned(),
        source: "test".to_owned(),
        payload: Map::new(),
        idempotency_key: None,
        caused_by: None,
        session_id: None,
        run_id: None,
        turn_id: None,
        timestamp_ms: 1,
        cursor: None,
    };

    let decisions = route_event(&event, &routes).unwrap();

    assert_eq!(decisions.len(), 1);
    assert_eq!(decisions[0].agent_id(), "alpha");
    assert_eq!(decisions[0].session_id().as_str(), "agent/alpha");
    assert_eq!(decisions[0].lock_keys(), ["repo:zeta"]);
    let project_generation = manifest.id.to_string();
    assert_eq!(
        decisions[0].project_generation(),
        Some(project_generation.as_str())
    );

    let literal_wildcard = Event {
        id: "evt_literal_wildcard".to_owned(),
        event_type: "work.*".to_owned(),
        source: "test".to_owned(),
        payload: Map::new(),
        idempotency_key: None,
        caused_by: None,
        session_id: None,
        run_id: None,
        turn_id: None,
        timestamp_ms: 2,
        cursor: None,
    };
    let decisions = route_event(&literal_wildcard, &routes).unwrap();
    assert_eq!(decisions.len(), 1);
    assert_eq!(decisions[0].agent_id(), "wildcard");
    assert_eq!(
        decisions[0].session_id().as_str(),
        "agent/wildcard/evt_literal_wildcard"
    );

    let mut tampered = manifest.clone();
    tampered.agents.get_mut("alpha").unwrap().session = "per-event".to_owned();
    assert!(zeta::routes_from_project(&tampered).is_err());
}

#[test]
fn authored_schedule_occurrence_uses_ordinary_idempotent_dispatch_ingress() {
    let digest = parse_agent(
        "digest",
        b"---\nname: Digest\ndescription: Summarizes.\nschedules:\n  - cron: '0 8 * * *'\n    timezone: Europe/Paris\n---\nSummarize.\n",
    )
    .unwrap();
    let project = compile_project(AgentProjectInput {
        agents: vec![digest],
        events: EventRegistry::new(),
        skill_resources: Vec::new(),
        skill_specs: Vec::new(),
        connectors: Vec::new(),
        capabilities: Vec::new(),
        executor_providers: vec![ExecutorProviderSpec {
            id: "local".to_owned(),
            implementation: authored_implementation(7),
        }],
        model: None,
        runtime_fingerprint: authored_implementation(8),
    })
    .unwrap();
    let manifest = project_manifest(&project).unwrap();
    let routes = zeta::routes_from_project(&manifest).unwrap();
    let occurrence = Event {
        id: "schedule-digest-2026-08-13".to_owned(),
        event_type: "agent.digest.scheduled".to_owned(),
        source: "zeta:scheduler".to_owned(),
        payload: object(json!({
            "date": "2026-08-13",
            "timestamp": "2026-08-13T08:00:00+02:00",
        })),
        idempotency_key: Some("schedule:digest:0 8 * * *:2026-08-13T08:00:00+02:00".to_owned()),
        caused_by: None,
        session_id: None,
        run_id: None,
        turn_id: None,
        timestamp_ms: 1_755_066_000_000,
        cursor: None,
    };
    let mut dispatch = Dispatch::open_in_memory().unwrap();

    let retained = dispatch.ingest_event(occurrence.clone()).unwrap();
    let routed = dispatch
        .route_ingress_event(
            &retained.event.id,
            &routes,
            &[RuntimeEventIdentity::new("scheduled-digest-available", 1_755_066_000_001).unwrap()],
        )
        .unwrap();
    let retried = dispatch.ingest_event(occurrence).unwrap();

    assert!(retained.inserted);
    assert!(!retried.inserted);
    assert_eq!(retried.event.id, retained.event.id);
    assert_eq!(routed.events().len(), 1);
    let queue_items = dispatch.list_queue_items().unwrap();
    assert_eq!(queue_items.len(), 1);
    assert_eq!(queue_items[0].target_agent(), "digest");
    assert_eq!(queue_items[0].status(), QueueItemStatus::Available);
}

fn object(value: Value) -> Map<String, Value> {
    let Value::Object(value) = value else {
        panic!("the test value must be an object")
    };
    value
}
