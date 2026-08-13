use std::collections::BTreeMap;
use std::fs;
use std::io::{BufRead as _, BufReader as StdBufReader, Read as _};
use std::os::unix::fs::{symlink, MetadataExt, PermissionsExt};
use std::os::unix::net::UnixListener as StdUnixListener;
use std::path::{Path, PathBuf};
use std::process::{Child, Command as StdCommand, Stdio};
use std::sync::{mpsc, Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Map, Value};
use tempfile::TempDir;
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::net::unix::{OwnedReadHalf, OwnedWriteHalf};
use tokio::net::{TcpListener, TcpStream, UnixStream};
use tokio::task::JoinHandle;
use zeta::runtime_services::{
    RuntimeLease, RuntimeMetadata, RuntimeOwnerMode, RuntimePaths, RuntimePhase,
};
use zeta::{
    prepare_agent, CallbackDraftRecorder, CallbackObserver, CancellationToken, ExecutorSelection,
    InvocationInputs, LocalSocketConfig, LocalSocketServer, ProcessExecutor, ProcessLaunch,
    Project, Scheduler, SystemClock, UuidIdSource,
};
use zeta_agent::{
    native_capabilities, resolve_capabilities, AgentInvocation, AgentRunResult, AgentRunner,
    Capability, HttpModelGateway, HttpModelGatewayConfig, ModelHttpEndpoint,
    ModelTransportTimeouts, NativeToolExecutor, Observation, PromptEnvironment, PromptTransform,
    RunStopReason, ToolProfile,
};
use zeta_dispatch::{route_event, Dispatch, QueueItemStatus, RuntimeEventIdentity};
use zeta_ipc::{
    ErrorObject, ErrorResponse, Message, Notification, Request, SuccessResponse, INVALID_PARAMS,
    MAX_FRAME_BYTES, METHOD_NOT_FOUND, PARSE_ERROR, SERVER_ERROR,
};
use zeta_journal::{DraftEvent, Event, EventFilter};
use zeta_manifest::{
    compile_project, execution_manifest, parse_agent, project_manifest, verify_execution_manifest,
    AgentProjectInput, CapabilitySpec, EventRegistry, ExecutorProviderSpec,
    ImplementationFingerprint, ModelSelectionSpec,
};

struct SocketClient {
    reader: BufReader<OwnedReadHalf>,
    writer: OwnedWriteHalf,
}

impl SocketClient {
    async fn connect(path: &Path) -> Self {
        let stream = UnixStream::connect(path)
            .await
            .expect("the test client must connect");
        let (reader, writer) = stream.into_split();
        Self {
            reader: BufReader::new(reader),
            writer,
        }
    }

    async fn initialize(&mut self) -> Value {
        self.send_json(json!({
            "jsonrpc": "2.0",
            "id": "initialize",
            "method": "initialize",
            "params": {
                "protocol_versions": [0],
                "peer": {"name": "socket-test", "version": "0.1.0"},
                "roles": ["client"],
                "heartbeat_seconds": 10,
                "max_in_flight": 16
            }
        }))
        .await;
        let Message::Success(SuccessResponse { id: _, result }) = self.receive().await else {
            panic!("socket initialization must succeed")
        };
        result
    }

    async fn ping(&mut self, id: u64) {
        self.send_json(json!({
            "jsonrpc": "2.0",
            "id": id,
            "method": "ping",
            "params": {}
        }))
        .await;
        let Message::Success(SuccessResponse {
            id: response_id,
            result,
        }) = self.receive().await
        else {
            panic!("ping must succeed")
        };
        assert_eq!(response_id, id.into());
        assert_eq!(result, json!({}));
    }

    async fn send_json(&mut self, value: Value) {
        self.send_line(value.to_string().as_bytes()).await;
    }

    async fn send_line(&mut self, line: &[u8]) {
        self.writer
            .write_all(line)
            .await
            .expect("the test client must write a frame");
        self.writer
            .write_all(b"\n")
            .await
            .expect("the test client must terminate a frame");
        self.writer
            .flush()
            .await
            .expect("the test client must flush a frame");
    }

    async fn receive(&mut self) -> Message {
        let mut line = String::new();
        let count = tokio::time::timeout(Duration::from_secs(5), self.reader.read_line(&mut line))
            .await
            .expect("the socket server must answer in time")
            .expect("the test client must read a frame");
        assert!(count > 0, "the socket server closed before answering");
        Message::parse_str(line.trim_end()).expect("the socket server must emit valid IPC")
    }
}

fn socket_request(request: Request) -> Result<Value, ErrorObject> {
    let Request {
        id: _,
        method,
        params,
    } = request;
    match method.as_str() {
        "events.list" => Ok(json!({"method": method, "params": params})),
        "session.list" => Err(ErrorObject::application(
            SERVER_ERROR,
            "fixture_failure",
            "The fixture rejected the request",
            zeta_ipc::Retryability::Final,
        )),
        "events.publish" | "session.start" | "session.send" | "session.status"
        | "session.cancel" => Ok(json!({})),
        "initialize" | "event" | "ping" | "shutdown" => {
            panic!("reserved methods must stay inside the IPC session")
        }
        unexpected => panic!("unexpected test request {unexpected:?}"),
    }
}

fn notification_event(cursor: u64) -> Event {
    Event {
        id: format!("evt_{cursor}"),
        event_type: "test.event".to_owned(),
        source: "socket-test".to_owned(),
        payload: object(json!({"cursor": cursor})),
        idempotency_key: None,
        caused_by: None,
        session_id: Some("session-1".to_owned()),
        run_id: Some("run-1".to_owned()),
        turn_id: None,
        timestamp_ms: i64::try_from(cursor).expect("the test cursor must fit i64"),
        cursor: Some(cursor),
    }
}

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
    let project_revision = manifest.id.to_string();
    assert_eq!(
        decisions[0].project_revision(),
        Some(project_revision.as_str())
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
    let scheduler = Scheduler::from_project(&manifest).unwrap();
    let mut dispatch = Dispatch::open_in_memory().unwrap();
    let mut scheduler_ids = UuidIdSource::new("scheduler");
    let now_ms = 1_786_600_830_000;

    let requested = scheduler
        .tick(&mut dispatch, now_ms, &mut scheduler_ids)
        .unwrap();

    assert_eq!(requested.len(), 1);
    let occurrence = &requested[0];
    assert_eq!(occurrence.event_type, "agent.digest.scheduled");
    assert_eq!(
        occurrence.payload,
        object(json!({
            "date": "2026-08-13",
            "timestamp": "2026-08-13T08:00:00+02:00",
        }))
    );
    assert_eq!(
        occurrence.idempotency_key.as_deref(),
        Some("schedule:digest:0 8 * * *:2026-08-13T08:00:00+02:00")
    );
    assert_eq!(
        dispatch.unrouted_ingress_events().unwrap(),
        [occurrence.id.as_str()]
    );
    let queue_items = dispatch.list_queue_items().unwrap();
    assert_eq!(queue_items.len(), 1);
    assert_eq!(queue_items[0].target_agent(), "");
    assert_eq!(queue_items[0].status(), QueueItemStatus::Pending);
    assert_eq!(
        dispatch
            .list_events(&EventFilter {
                event_type_prefix: Some("zeta.scheduler.tick.".to_owned()),
                ..EventFilter::default()
            })
            .unwrap()
            .len(),
        1
    );

    let routed = dispatch
        .route_ingress_event(
            &occurrence.id,
            &routes,
            &[RuntimeEventIdentity::new("scheduled-digest-available", now_ms + 1).unwrap()],
        )
        .unwrap();
    let retried = scheduler
        .tick(&mut dispatch, now_ms, &mut scheduler_ids)
        .unwrap();

    assert!(retried.is_empty());
    assert_eq!(routed.events().len(), 1);
    assert!(dispatch.unrouted_ingress_events().unwrap().is_empty());
    let queue_items = dispatch.list_queue_items().unwrap();
    assert_eq!(queue_items.len(), 1);
    assert_eq!(queue_items[0].target_agent(), "digest");
    assert_eq!(queue_items[0].status(), QueueItemStatus::Available);
}

#[tokio::test]
async fn local_socket_binds_owner_only_and_reports_invalid_paths() {
    let directory = TempDir::new().expect("the socket test directory must exist");
    let path = directory.path().join("runtime.sock");
    let server = LocalSocketServer::bind(
        &path,
        LocalSocketConfig::application("application-instance"),
        socket_request,
    )
    .await
    .expect("the local socket must bind");
    let mode = fs::symlink_metadata(&path)
        .expect("the socket entry must exist")
        .permissions()
        .mode()
        & 0o777;
    assert_eq!(mode, 0o600);

    server
        .shutdown()
        .await
        .expect("the local socket must stop cleanly");
    assert!(!path.exists());

    let relative = Path::new("relative-runtime.sock");
    let error = LocalSocketServer::bind(
        relative,
        LocalSocketConfig::application("application-instance"),
        socket_request,
    )
    .await
    .expect_err("a relative local socket path must be rejected");
    assert_eq!(error.reason(), "relative_path");
    assert!(error.to_string().contains("relative-runtime.sock"));

    let path = directory.path().join("x".repeat(256));
    let error = LocalSocketServer::bind(
        &path,
        LocalSocketConfig::application("application-instance"),
        socket_request,
    )
    .await
    .expect_err("an overlong local socket path must be rejected");
    assert!(error
        .to_string()
        .contains(&path.to_string_lossy().to_string()));
}

#[tokio::test]
async fn local_socket_refuses_every_preexisting_entry() {
    let directory = TempDir::new().expect("the socket test directory must exist");

    let regular = directory.path().join("regular.sock");
    fs::write(&regular, "occupied").expect("the regular fixture must exist");
    let error = LocalSocketServer::bind(
        &regular,
        LocalSocketConfig::application("application-instance"),
        socket_request,
    )
    .await
    .expect_err("a regular file must not be replaced");
    assert_eq!(error.reason(), "path_occupied");

    let target = directory.path().join("target");
    let link = directory.path().join("link.sock");
    symlink(&target, &link).expect("the symlink fixture must exist");
    let error = LocalSocketServer::bind(
        &link,
        LocalSocketConfig::application("application-instance"),
        socket_request,
    )
    .await
    .expect_err("a symlink must not be replaced");
    assert_eq!(error.reason(), "path_occupied");

    let socket = directory.path().join("existing.sock");
    let listener = StdUnixListener::bind(&socket).expect("the live socket fixture must bind");
    let error = LocalSocketServer::bind(
        &socket,
        LocalSocketConfig::application("application-instance"),
        socket_request,
    )
    .await
    .expect_err("a live socket must not be replaced");
    assert_eq!(error.reason(), "path_occupied");
    drop(listener);

    let error = LocalSocketServer::bind(
        &socket,
        LocalSocketConfig::application("application-instance"),
        socket_request,
    )
    .await
    .expect_err("a stale socket must not be removed without an owner lock");
    assert_eq!(error.reason(), "path_occupied");
}

#[tokio::test]
async fn local_socket_initializes_pings_delegates_and_recovers_frames() {
    let directory = TempDir::new().expect("the socket test directory must exist");
    let path = directory.path().join("runtime.sock");
    let server = LocalSocketServer::bind(
        &path,
        LocalSocketConfig::application("application-instance"),
        socket_request,
    )
    .await
    .expect("the local socket must bind");
    let mut client = SocketClient::connect(&path).await;

    let initialized = client.initialize().await;
    assert_eq!(initialized["roles"], json!(["client"]));
    assert_eq!(initialized["config"]["instance_id"], "application-instance");
    client.ping(1).await;

    client
        .send_json(json!({
            "jsonrpc": "2.0",
            "id": "list-events",
            "method": "events.list",
            "params": {"limit": 3}
        }))
        .await;
    let Message::Success(SuccessResponse { id: _, result }) = client.receive().await else {
        panic!("the delegated request must succeed")
    };
    assert_eq!(result["method"], "events.list");
    assert_eq!(result["params"], json!({"limit": 3}));

    client
        .send_json(json!({
            "jsonrpc": "2.0",
            "id": "list-sessions",
            "method": "session.list",
            "params": {}
        }))
        .await;
    let Message::Error(ErrorResponse { id: _, error }) = client.receive().await else {
        panic!("the delegated fixture failure must be returned")
    };
    assert_eq!(
        error.data.expect("the error must carry details")["code"],
        "fixture_failure"
    );

    client.send_line(b"not json").await;
    let Message::Error(ErrorResponse { id, error }) = client.receive().await else {
        panic!("a malformed frame must produce an error")
    };
    assert_eq!(id, None);
    assert_eq!(error.code, PARSE_ERROR);
    client.ping(2).await;

    client
        .send_line(br#"{"jsonrpc":"2.0","id":"invalid-shape","method":""}"#)
        .await;
    let Message::Error(ErrorResponse { id, error: _error }) = client.receive().await else {
        panic!("an invalid request must produce an error")
    };
    assert_eq!(id, Some("invalid-shape".into()));

    let oversized = vec![b'x'; MAX_FRAME_BYTES + 1];
    client.send_line(&oversized).await;
    let Message::Error(ErrorResponse { id, error }) = client.receive().await else {
        panic!("an oversized frame must produce an error")
    };
    assert_eq!(id, None);
    assert_eq!(error.code, PARSE_ERROR);
    client.ping(3).await;

    let stream = UnixStream::connect(&path)
        .await
        .expect("the EOF client must connect");
    let (reader, mut writer) = stream.into_split();
    let initialization = json!({
        "jsonrpc": "2.0",
        "id": "final-object",
        "method": "initialize",
        "params": {
            "protocol_versions": [0],
            "peer": {"name": "eof-test", "version": "0.1.0"},
            "roles": ["client"]
        }
    })
    .to_string();
    writer
        .write_all(initialization.as_bytes())
        .await
        .expect("the EOF client must write its final object");
    writer
        .shutdown()
        .await
        .expect("the EOF client must close its writing side");
    let mut reader = BufReader::new(reader);
    let mut line = String::new();
    reader
        .read_line(&mut line)
        .await
        .expect("the EOF client must read initialization");
    let Message::Success(SuccessResponse {
        id,
        result: _result,
    }) = Message::parse_str(line.trim_end()).expect("the EOF response must be valid IPC")
    else {
        panic!("a complete final object at EOF must initialize")
    };
    assert_eq!(id, "final-object".into());

    server
        .shutdown()
        .await
        .expect("the local socket must stop cleanly");
}

#[tokio::test]
async fn local_socket_isolates_clients_and_fans_out_notifications() {
    let directory = TempDir::new().expect("the socket test directory must exist");
    let path = directory.path().join("runtime.sock");
    let server = LocalSocketServer::bind(
        &path,
        LocalSocketConfig::application("application-instance"),
        socket_request,
    )
    .await
    .expect("the local socket must bind");
    let mut first = SocketClient::connect(&path).await;
    let mut second = SocketClient::connect(&path).await;
    first.initialize().await;
    second.initialize().await;

    let event = notification_event(1);
    server
        .notify_event(&event)
        .expect("a valid durable event must be accepted");
    for client in [&mut first, &mut second] {
        let Message::Notification(Notification { method, params }) = client.receive().await else {
            panic!("every initialized client must receive the event")
        };
        assert_eq!(method, "event");
        assert_eq!(params["event"]["id"], "evt_1");
    }

    first
        .send_json(json!({
            "jsonrpc": "2.0",
            "id": "unauthorized-shutdown",
            "method": "shutdown",
            "params": {"reason": "one client exited"}
        }))
        .await;
    let Message::Error(ErrorResponse { id: _, error }) = first.receive().await else {
        panic!("a socket client must not have process shutdown authority")
    };
    assert_eq!(error.code, METHOD_NOT_FOUND);
    drop(first);

    second.ping(4).await;
    let mut third = SocketClient::connect(&path).await;
    third.initialize().await;
    third.ping(5).await;

    server
        .shutdown()
        .await
        .expect("the local socket must stop cleanly");
}

#[tokio::test]
async fn local_socket_shutdown_preserves_a_replacement_entry() {
    let directory = TempDir::new().expect("the socket test directory must exist");
    let path = directory.path().join("runtime.sock");
    let server = LocalSocketServer::bind(
        &path,
        LocalSocketConfig::application("application-instance"),
        socket_request,
    )
    .await
    .expect("the local socket must bind");

    fs::remove_file(&path).expect("the bound name must be replaceable");
    fs::write(&path, "replacement").expect("the replacement fixture must exist");
    server
        .shutdown()
        .await
        .expect("the local socket must stop cleanly");

    assert_eq!(
        fs::read_to_string(&path).expect("the replacement must remain"),
        "replacement"
    );
}

#[tokio::test]
async fn local_control_socket_reports_identity_and_flushes_shutdown_before_notification() {
    let directory = TempDir::new().expect("the control socket test directory must exist");
    let path = directory.path().join("runtime-control.sock");
    let server = LocalSocketServer::bind(
        &path,
        LocalSocketConfig::control("control-instance"),
        |_request| panic!("control requests must not reach the application handler"),
    )
    .await
    .expect("the control socket must bind");
    let mut host_shutdown = server.host_shutdown();

    let mut source = SocketClient::connect(&path).await;
    source
        .send_json(json!({
            "jsonrpc": "2.0",
            "id": "source-initialize",
            "method": "initialize",
            "params": {
                "protocol_versions": [0],
                "peer": {"name": "socket-test", "version": "0.1.0"},
                "roles": ["source"]
            }
        }))
        .await;
    let Message::Error(ErrorResponse { id: _, error }) = source.receive().await else {
        panic!("the control socket must reject the source role")
    };
    assert_eq!(error.code, INVALID_PARAMS);
    drop(source);

    let mut client = SocketClient::connect(&path).await;

    let initialized = client.initialize().await;
    assert_eq!(initialized["roles"], json!(["client"]));
    assert_eq!(initialized["config"]["instance_id"], "control-instance");
    client.ping(1).await;
    client
        .send_json(json!({
            "jsonrpc": "2.0",
            "id": "stop-host",
            "method": "shutdown",
            "params": {"reason": "test complete"}
        }))
        .await;

    host_shutdown
        .changed()
        .await
        .expect("the control request must notify the host");
    assert!(*host_shutdown.borrow());
    let Message::Success(SuccessResponse { id, result }) = client.receive().await else {
        panic!("the flushed shutdown response must remain readable")
    };
    assert_eq!(id, "stop-host".into());
    assert_eq!(result, json!({}));

    server
        .shutdown()
        .await
        .expect("the control socket must stop cleanly");
}

#[tokio::test]
async fn malformed_control_clients_cannot_stop_or_poison_the_control_socket() {
    let directory = TempDir::new().expect("the control socket test directory must exist");
    let path = directory.path().join("runtime-control.sock");
    let server = LocalSocketServer::bind(
        &path,
        LocalSocketConfig::control("control-instance"),
        |_request| panic!("control requests must not reach the application handler"),
    )
    .await
    .expect("the control socket must bind");
    let mut host_shutdown = server.host_shutdown();
    let stream = UnixStream::connect(&path)
        .await
        .expect("the malformed control client must connect");
    let (reader, mut writer) = stream.into_split();
    writer
        .write_all(b"not json\n")
        .await
        .expect("the malformed control client must write");
    writer
        .flush()
        .await
        .expect("the malformed control client must flush");
    let mut reader = BufReader::new(reader);
    let mut line = String::new();
    reader
        .read_line(&mut line)
        .await
        .expect("the malformed control client must receive an error");
    let Message::Error(ErrorResponse { id: _, error }) =
        Message::parse_str(line.trim_end()).expect("the control error must be valid IPC")
    else {
        panic!("malformed control input must produce an error")
    };
    assert_eq!(error.code, PARSE_ERROR);
    drop(reader);
    drop(writer);
    assert!(!*host_shutdown.borrow());

    let mut premature = SocketClient::connect(&path).await;
    premature
        .send_json(json!({
            "jsonrpc": "2.0",
            "id": "premature-shutdown",
            "method": "shutdown",
            "params": {}
        }))
        .await;
    let Message::Error(ErrorResponse { id: _, error: _ }) = premature.receive().await else {
        panic!("shutdown before initialization must be rejected")
    };
    tokio::time::timeout(Duration::from_millis(50), host_shutdown.changed())
        .await
        .expect_err("a rejected shutdown must not notify the host");
    assert!(!*host_shutdown.borrow());

    let mut healthy = SocketClient::connect(&path).await;
    healthy.initialize().await;
    healthy.ping(2).await;
    healthy
        .send_json(json!({
            "jsonrpc": "2.0",
            "id": "application-method",
            "method": "events.list",
            "params": {}
        }))
        .await;
    let Message::Error(ErrorResponse { id: _, error }) = healthy.receive().await else {
        panic!("the control endpoint must reject application methods")
    };
    assert_eq!(error.code, METHOD_NOT_FOUND);
    assert!(!*host_shutdown.borrow());

    server
        .shutdown()
        .await
        .expect("the control socket must stop cleanly");
}

#[test]
fn lifecycle_cli_detaches_reports_status_and_stops_idempotently() {
    let directory = TempDir::new().expect("the lifecycle directory must exist");
    let project = directory.path().join("project");
    let state = directory.path().join("state");
    fs::create_dir(&project).expect("the lifecycle project must exist");
    let binary = env!("CARGO_BIN_EXE_zeta");

    let fresh = StdCommand::new(binary)
        .args(["status", "--state-dir"])
        .arg(&state)
        .output()
        .expect("fresh status must execute");
    assert!(!fresh.status.success());
    assert_eq!(
        String::from_utf8_lossy(&fresh.stdout),
        "Runtime: stopped\nNext: zeta up --project-root <PROJECT_ROOT>\n"
    );
    assert!(!state.exists(), "status must not create runtime state");
    activate_lifecycle_project(&project, &state);

    let started = StdCommand::new(binary)
        .args(["up", "--detach", "--project-root"])
        .arg(&project)
        .arg("--state-dir")
        .arg(&state)
        .output()
        .expect("detached up must execute");
    assert!(
        started.status.success(),
        "detached up failed: {}",
        String::from_utf8_lossy(&started.stderr)
    );
    let started = String::from_utf8(started.stdout).expect("started output must be UTF-8");
    assert!(
        started.starts_with("Runtime started.\nPID: "),
        "unexpected output: {started:?}"
    );
    assert_eq!(
        fs::symlink_metadata(state.join("runtime.log"))
            .expect("detached diagnostics must exist")
            .permissions()
            .mode()
            & 0o777,
        0o600
    );

    let status = StdCommand::new(binary)
        .args(["status", "--state-dir"])
        .arg(&state)
        .output()
        .expect("running status must execute");
    assert!(status.status.success());
    assert!(String::from_utf8_lossy(&status.stdout).starts_with("Runtime: running\n"));

    let already = StdCommand::new(binary)
        .args(["up", "--detach", "--project-root"])
        .arg(&project)
        .arg("--state-dir")
        .arg(&state)
        .output()
        .expect("second detached up must execute");
    assert!(already.status.success());
    assert!(
        String::from_utf8_lossy(&already.stdout).starts_with("Runtime is already running.\nPID: ")
    );

    let json_status = StdCommand::new(binary)
        .args(["status", "--json", "--state-dir"])
        .arg(&state)
        .output()
        .expect("JSON status must execute");
    assert!(json_status.status.success());
    let report: Value =
        serde_json::from_slice(&json_status.stdout).expect("status must emit compact JSON");
    assert_eq!(
        report
            .as_object()
            .expect("status report must be an object")
            .len(),
        12
    );
    assert_eq!(report["schema"], "zeta.status");
    assert_eq!(report["version"], 2);
    assert_eq!(report["status"], "running");
    assert!(report["pid"].is_number());
    assert_eq!(
        report["state_dir"],
        fs::canonicalize(&state)
            .expect("runtime state must resolve")
            .display()
            .to_string()
    );
    assert_eq!(
        report["project_root"],
        fs::canonicalize(&project)
            .expect("project root must resolve")
            .display()
            .to_string()
    );
    assert_eq!(report["active_project"]["agents"][0]["slug"], "worker");
    assert_eq!(report["dispatch"]["queue"], serde_json::json!({}));

    let stopped = StdCommand::new(binary)
        .args(["down", "--state-dir"])
        .arg(&state)
        .output()
        .expect("down must execute");
    assert!(
        stopped.status.success(),
        "down failed: {}",
        String::from_utf8_lossy(&stopped.stderr)
    );
    assert_eq!(
        String::from_utf8_lossy(&stopped.stdout),
        "Runtime is stopped.\n"
    );

    let stopped_again = StdCommand::new(binary)
        .args(["down", "--state-dir"])
        .arg(&state)
        .output()
        .expect("idempotent down must execute");
    assert!(stopped_again.status.success());
    assert_eq!(
        String::from_utf8_lossy(&stopped_again.stdout),
        "Runtime is stopped.\n"
    );
}

#[tokio::test]
async fn lifecycle_application_socket_is_ready_without_shutdown_authority() {
    let directory = TempDir::new().expect("the lifecycle directory must exist");
    let project = directory.path().join("project");
    let state = directory.path().join("state");
    fs::create_dir(&project).expect("the lifecycle project must exist");
    activate_lifecycle_project(&project, &state);
    let binary = env!("CARGO_BIN_EXE_zeta");
    let started = StdCommand::new(binary)
        .args(["up", "--detach", "--project-root"])
        .arg(&project)
        .arg("--state-dir")
        .arg(&state)
        .output()
        .expect("detached up must execute");
    assert!(
        started.status.success(),
        "{}",
        String::from_utf8_lossy(&started.stderr)
    );

    let mut client = SocketClient::connect(&state.join("runtime.sock")).await;
    let initialized = client.initialize().await;
    assert!(initialized["config"]["instance_id"].is_string());
    client
        .send_json(json!({
            "jsonrpc": "2.0",
            "id": "application",
            "method": "agents.list",
            "params": {}
        }))
        .await;
    let Message::Success(SuccessResponse { id: _, result }) = client.receive().await else {
        panic!("the lifecycle application seam must list active agents")
    };
    assert_eq!(result["agents"][0]["slug"], "worker");

    client
        .send_json(json!({
            "jsonrpc": "2.0",
            "id": "publish",
            "method": "events.publish",
            "params": {
                "type": "example.created",
                "payload": {},
                "idempotency_key": "native-reactive-ingress"
            }
        }))
        .await;
    let Message::Success(SuccessResponse { id: _, result }) = client.receive().await else {
        panic!("the lifecycle application seam must accept native ingress")
    };
    assert!(result["inserted"]
        .as_bool()
        .expect("ingress inserted state"));
    assert_eq!(result["route_count"], 0);

    client
        .send_json(json!({
            "jsonrpc": "2.0",
            "id": "runtime-status",
            "method": "runtime.status",
            "params": {}
        }))
        .await;
    let Message::Success(SuccessResponse { id: _, result }) = client.receive().await else {
        panic!("the lifecycle application seam must report reactive state")
    };
    assert!(result["wake_epoch"].as_u64().expect("wake epoch") > 0);
    assert_eq!(result["queue"]["unhandled"], 1);

    write_lifecycle_agent(&project, "replacement");
    client
        .send_json(json!({
            "jsonrpc": "2.0",
            "id": "reload",
            "method": "project.reload",
            "params": {}
        }))
        .await;
    let Message::Success(SuccessResponse { id: _, result }) = client.receive().await else {
        panic!("the lifecycle application seam must reload agents")
    };
    assert_eq!(result["agents"].as_array().expect("agents array").len(), 2);

    client
        .send_json(json!({
            "jsonrpc": "2.0",
            "id": "unauthorized-shutdown",
            "method": "shutdown",
            "params": {}
        }))
        .await;
    let Message::Error(ErrorResponse { id: _, error }) = client.receive().await else {
        panic!("the application socket must reject shutdown")
    };
    assert_eq!(error.code, METHOD_NOT_FOUND);

    let status = StdCommand::new(binary)
        .args(["status", "--state-dir"])
        .arg(&state)
        .output()
        .expect("status must execute");
    assert!(status.status.success());
    let stopped = StdCommand::new(binary)
        .args(["down", "--state-dir"])
        .arg(&state)
        .output()
        .expect("down must execute");
    assert!(stopped.status.success());
}

struct ForegroundRuntime {
    child: Child,
}

impl ForegroundRuntime {
    fn start(project: &Path, state: &Path) -> Self {
        let mut child = StdCommand::new(env!("CARGO_BIN_EXE_zeta"))
            .args(["up", "--project-root"])
            .arg(project)
            .arg("--state-dir")
            .arg(state)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .expect("foreground up must start");
        let stdout = child.stdout.take().expect("up must expose readiness");
        let mut stderr = child.stderr.take();
        let (sender, receiver) = mpsc::channel();
        std::thread::spawn(move || {
            let mut line = String::new();
            let result = StdBufReader::new(stdout).read_line(&mut line).map(|_| line);
            let _sent = sender.send(result);
        });
        let readiness = receiver
            .recv_timeout(Duration::from_secs(5))
            .expect("foreground up must become ready")
            .expect("foreground readiness must be readable");
        if readiness != "Runtime is running.\n" {
            let mut diagnostic = String::new();
            if let Some(stderr) = stderr.take() {
                let _read = StdBufReader::new(stderr).read_to_string(&mut diagnostic);
            }
            panic!("unexpected readiness: {readiness:?}; stderr: {diagnostic}");
        }
        Self { child }
    }

    fn wait_for_clean_exit(&mut self) {
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        loop {
            let status = self
                .child
                .try_wait()
                .expect("foreground runtime status must be readable");
            if let Some(status) = status {
                assert!(status.success(), "foreground runtime exited {status}");
                return;
            }
            assert!(
                std::time::Instant::now() < deadline,
                "foreground runtime did not exit"
            );
            std::thread::sleep(Duration::from_millis(25));
        }
    }
}

impl Drop for ForegroundRuntime {
    fn drop(&mut self) {
        let _killed = self.child.kill();
        let _status = self.child.wait();
    }
}

fn activate_lifecycle_project(project: &Path, state: &Path) {
    write_lifecycle_agent(project, "worker");
    let revision = Project::open(project)
        .expect("project must open")
        .revision()
        .expect("project revision must load");
    let paths = RuntimePaths::new(state);
    let lease = RuntimeLease::acquire(paths.clone()).expect("project state lease");
    revision
        .write(paths.active_project())
        .expect("active project must write");
    drop(lease);
}

fn write_lifecycle_agent(project: &Path, slug: &str) {
    let agents = project.join("agents");
    fs::create_dir_all(&agents).expect("agent directory must exist");
    fs::write(
        agents.join(format!("{slug}.md")),
        format!("---\nname: {slug}\ndescription: Lifecycle fixture.\n---\nReport.\n"),
    )
    .expect("agent source must write");
}

#[test]
fn lifecycle_cli_foreground_is_ready_and_down_releases_ownership() {
    let directory = TempDir::new().expect("the lifecycle directory must exist");
    let project = directory.path().join("project");
    let state = directory.path().join("state");
    fs::create_dir(&project).expect("the lifecycle project must exist");
    activate_lifecycle_project(&project, &state);
    let mut runtime = ForegroundRuntime::start(&project, &state);
    let metadata_before = fs::read(state.join("runtime.json")).expect("metadata must exist");
    let socket_before =
        fs::symlink_metadata(state.join("runtime.sock")).expect("application socket must exist");
    let control_before = fs::symlink_metadata(state.join("runtime-control.sock"))
        .expect("control socket must exist");

    let already = StdCommand::new(env!("CARGO_BIN_EXE_zeta"))
        .args(["up", "--project-root"])
        .arg(&project)
        .arg("--state-dir")
        .arg(&state)
        .output()
        .expect("second foreground up must execute");
    assert!(already.status.success());
    assert!(
        String::from_utf8_lossy(&already.stdout).starts_with("Runtime is already running.\nPID: ")
    );
    assert_eq!(
        fs::read(state.join("runtime.json")).expect("metadata must remain"),
        metadata_before
    );
    let socket_after =
        fs::symlink_metadata(state.join("runtime.sock")).expect("application socket must remain");
    let control_after = fs::symlink_metadata(state.join("runtime-control.sock"))
        .expect("control socket must remain");
    assert_eq!(
        (socket_after.dev(), socket_after.ino()),
        (socket_before.dev(), socket_before.ino())
    );
    assert_eq!(
        (control_after.dev(), control_after.ino()),
        (control_before.dev(), control_before.ino())
    );

    let down = StdCommand::new(env!("CARGO_BIN_EXE_zeta"))
        .args(["down", "--state-dir"])
        .arg(&state)
        .output()
        .expect("down must execute");
    assert!(
        down.status.success(),
        "{}",
        String::from_utf8_lossy(&down.stderr)
    );
    assert_eq!(
        String::from_utf8_lossy(&down.stdout),
        "Runtime is stopped.\n"
    );
    runtime.wait_for_clean_exit();
    assert!(!state.join("runtime.json").exists());
    assert!(!state.join("runtime.sock").exists());
    assert!(!state.join("runtime-control.sock").exists());
    assert!(state.join("runtime.lock").exists());
}

#[test]
fn lifecycle_cli_handles_operator_signals_as_clean_shutdown() {
    for signal in [
        rustix::process::Signal::Int,
        rustix::process::Signal::Term,
        rustix::process::Signal::Hup,
    ] {
        let directory = TempDir::new().expect("the lifecycle directory must exist");
        let project = directory.path().join("project");
        let state = directory.path().join("state");
        fs::create_dir(&project).expect("the lifecycle project must exist");
        activate_lifecycle_project(&project, &state);
        let mut runtime = ForegroundRuntime::start(&project, &state);
        rustix::process::kill_process(rustix::process::Pid::from_child(&runtime.child), signal)
            .expect("the operator signal must be delivered");
        runtime.wait_for_clean_exit();
        assert!(!state.join("runtime.json").exists());
        assert!(!state.join("runtime.sock").exists());
        assert!(!state.join("runtime-control.sock").exists());
    }
}

#[test]
fn lifecycle_cli_reports_stopping_degraded_and_stale_without_repairing_status() {
    let binary = env!("CARGO_BIN_EXE_zeta");
    let directory = TempDir::new().expect("the lifecycle directory must exist");
    let root = fs::canonicalize(directory.path()).expect("the lifecycle root must resolve");
    let project = root.join("project");
    fs::create_dir(&project).expect("the lifecycle project must exist");

    let stopping_state = root.join("stopping");
    let stopping_paths = RuntimePaths::new(&stopping_state);
    let stopping_lease =
        RuntimeLease::acquire(stopping_paths.clone()).expect("the stopping lease must be held");
    let stopping_metadata = RuntimeMetadata::new(
        "00000000-0000-4000-8000-000000000001",
        4242,
        RuntimeOwnerMode::Foreground,
        RuntimePhase::Stopping,
        &project,
        &stopping_paths,
        1,
    );
    stopping_lease
        .write_metadata(&stopping_metadata)
        .expect("stopping metadata must be written");
    let stopping = StdCommand::new(binary)
        .args(["status", "--state-dir"])
        .arg(&stopping_state)
        .output()
        .expect("stopping status must execute");
    assert!(!stopping.status.success());
    assert_eq!(
        String::from_utf8_lossy(&stopping.stdout),
        "Runtime: stopping\nPID: 4242\nProject: ".to_owned()
            + &project.display().to_string()
            + "\n"
    );
    assert_eq!(
        fs::read_to_string(stopping_paths.metadata()).expect("metadata must remain"),
        serde_json::to_string(&stopping_metadata).expect("metadata must serialize") + "\n"
    );
    drop(stopping_lease);

    let degraded_state = root.join("degraded");
    let degraded_paths = RuntimePaths::new(&degraded_state);
    let degraded_lease =
        RuntimeLease::acquire(degraded_paths.clone()).expect("the degraded lease must be held");
    let degraded = StdCommand::new(binary)
        .args(["status", "--json", "--state-dir"])
        .arg(&degraded_state)
        .output()
        .expect("degraded status must execute");
    assert!(!degraded.status.success());
    let degraded_report: Value =
        serde_json::from_slice(&degraded.stdout).expect("degraded status must be JSON");
    assert_eq!(degraded_report["status"], "degraded");
    assert!(degraded_report["detail"].is_string());
    assert!(!degraded_paths.metadata().exists());
    let refused_down = StdCommand::new(binary)
        .args(["down", "--state-dir"])
        .arg(&degraded_state)
        .output()
        .expect("degraded down must execute");
    assert!(!refused_down.status.success());
    drop(degraded_lease);

    let stale_state = root.join("stale");
    fs::create_dir(&stale_state).expect("the stale state directory must exist");
    fs::write(stale_state.join("runtime.json"), "not json\n")
        .expect("invalid stale metadata must exist");
    let stale = StdCommand::new(binary)
        .args(["status", "--state-dir"])
        .arg(&stale_state)
        .output()
        .expect("stale status must execute");
    assert!(!stale.status.success());
    assert_eq!(String::from_utf8_lossy(&stale.stdout), "Runtime: stale\n");
    assert_eq!(
        fs::read_to_string(stale_state.join("runtime.json"))
            .expect("status must preserve stale metadata"),
        "not json\n"
    );
    let cleaned = StdCommand::new(binary)
        .args(["down", "--state-dir"])
        .arg(&stale_state)
        .output()
        .expect("stale down must execute");
    assert!(
        cleaned.status.success(),
        "{}",
        String::from_utf8_lossy(&cleaned.stderr)
    );
    assert!(!stale_state.join("runtime.json").exists());
}

fn object(value: Value) -> Map<String, Value> {
    let Value::Object(value) = value else {
        panic!("the test value must be an object")
    };
    value
}
