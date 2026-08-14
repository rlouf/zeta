use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use base64::Engine as _;
use serde_json::{json, Map, Value};
use tempfile::TempDir;
use zeta::{
    BundleFile, ExecutorBundle, ExecutorCapability, ExecutorReuse, PythonModelGateway,
    PythonProviderHost, PythonProviderHostConfig, PythonToolExecutor,
};
use zeta_agent::{
    AbortReason, AbortSignal, AgentObserver, CapabilityExecutor, CapabilityInvocation,
    ModelGateway, ModelInput, ModelRequest, NativeToolExecutor, Observation, SystemCommandRunner,
};
use zeta_substrate::{canonical_json, hash_bytes};

struct ActiveAbort;

impl AbortSignal for ActiveAbort {
    fn reason(&self) -> Option<AbortReason> {
        None
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

fn object(value: Value) -> Map<String, Value> {
    value
        .as_object()
        .cloned()
        .expect("the test fixture must be an object")
}

fn executor_bundle() -> ExecutorBundle {
    let source = b"def read():\n    return 'ok'\n";
    let files = vec![BundleFile {
        path: "tools/workspace.py".to_owned(),
        content_address: hash_bytes(source).to_string(),
        content_base64: base64::engine::general_purpose::STANDARD.encode(source),
    }];
    let capabilities = vec![
        ExecutorCapability {
            id: "workspace.grep".to_owned(),
            description: "Search workspace files.".to_owned(),
            input_schema: object(json!({
                "type": "object",
                "required": ["query"],
                "properties": {"query": {"type": "string"}}
            })),
            output_schema: Some(object(json!({"type": "object"}))),
            source_path: "tools/workspace.py".to_owned(),
        },
        ExecutorCapability {
            id: "workspace.read".to_owned(),
            description: "Read one workspace file.".to_owned(),
            input_schema: object(json!({
                "type": "object",
                "required": ["path"],
                "properties": {"path": {"type": "string"}}
            })),
            output_schema: Some(object(json!({"type": "object"}))),
            source_path: "tools/workspace.py".to_owned(),
        },
    ];
    let workspace_id = format!(
        "workspace:{}",
        hash_bytes(&canonical_json(&json!({"files": files})).expect("workspace fixture encodes"))
    );
    let tool_id = format!(
        "tools:{}",
        hash_bytes(
            &canonical_json(&json!({"files": files, "capabilities": capabilities}))
                .expect("tool fixture encodes")
        )
    );
    serde_json::from_value(json!({
        "workspace": {
            "id": workspace_id,
            "files": files
        },
        "tools": {
            "id": tool_id,
            "files": files,
            "capabilities": capabilities
        }
    }))
    .expect("fixture executor bundle")
}

fn sdk_source() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .join("sdk")
        .join("python")
        .join("src")
}

fn host_config() -> PythonProviderHostConfig {
    let mut config = PythonProviderHostConfig::for_project(Path::new("/missing"));
    config
        .environment
        .insert("PYTHONPATH".to_owned(), sdk_source().display().to_string());
    config
}

fn host_config_with_fixture(project: &TempDir) -> PythonProviderHostConfig {
    let mut config = PythonProviderHostConfig::for_project(Path::new("/missing"));
    let python_path = std::env::join_paths([sdk_source(), project.path().to_path_buf()])
        .expect("the Python path is valid");
    config.environment.insert(
        "PYTHONPATH".to_owned(),
        python_path.to_string_lossy().into_owned(),
    );
    config
}

fn write_project_file(project: &TempDir, path: &str, content: &str) {
    let path = project.path().join(path);
    let parent = path.parent().expect("the fixture path has a parent");
    fs::create_dir_all(parent).expect("the fixture directory must exist");
    fs::write(path, content).expect("the fixture file must be written");
}

#[test]
fn starts_the_python_host_and_reads_its_catalog() {
    let project = TempDir::new().expect("temporary project");
    write_project_file(
        &project,
        "tools/echo.py",
        r#"
from zeta_plugin import tool


@tool("echo", input_schema={"type": "object"})
async def echo(request, context):
    """Return the supplied value."""
    return {"value": request["value"], "base_dir": context["base_dir"]}
"#,
    );

    let mut host =
        PythonProviderHost::with_config(project.path(), host_config()).expect("Python host starts");

    let provider = host
        .catalog()
        .tools()
        .get("echo")
        .expect("the catalog contains the local tool");
    assert_eq!(
        provider.source.path.as_deref(),
        Some(
            fs::canonicalize(project.path().join("tools/echo.py"))
                .expect("provider source path")
                .to_str()
                .expect("UTF-8 path")
        )
    );
    assert_eq!(
        provider.input_schema,
        Some(object(json!({"type": "object"})))
    );
    assert_eq!(
        provider.description.as_deref(),
        Some("Return the supplied value.")
    );
    assert_eq!(provider.fingerprint.len(), 64);

    let result = host
        .invoke(
            "echo",
            object(json!({"value": "ok"})),
            Some("/workspace".to_owned()),
            None,
            &ActiveAbort,
        )
        .expect("the local tool succeeds");
    assert_eq!(
        result,
        object(json!({"value": "ok", "base_dir": "/workspace"}))
    );
}

#[test]
fn python_host_reports_a_provider_failure() {
    let project = TempDir::new().expect("temporary project");
    write_project_file(
        &project,
        "tools/fail.py",
        r#"
from zeta_plugin import tool


@tool("fail", input_schema={"type": "object"})
async def fail(request, context):
    """Fail the provider call for this test."""
    raise RuntimeError("fixture failure")
"#,
    );

    let mut host =
        PythonProviderHost::with_config(project.path(), host_config()).expect("Python host starts");
    let error = host
        .invoke("fail", Map::new(), None, None, &ActiveAbort)
        .expect_err("the provider fails");

    assert_eq!(
        error.message,
        "provider call failed [provider_failed, retryable=false]: Provider 'fail' failed: fixture failure"
    );
}

#[test]
fn python_model_observations_reach_the_model_observer() {
    let project = TempDir::new().expect("temporary project");
    write_project_file(
        &project,
        "models/fixture.py",
        r#"
from zeta_plugin import model


@model("fixture")
async def fixture(request, context):
    context["observe"]({"kind": "text_delta", "text": "Hello"})
    return {
        "message": {"role": "assistant", "content": "Hello"},
        "telemetry": {},
        "streamed_content": True,
    }
"#,
    );
    let host =
        PythonProviderHost::with_config(project.path(), host_config()).expect("Python host starts");
    let host = Arc::new(Mutex::new(host));
    let mut gateway = PythonModelGateway::new(Arc::clone(&host), "fixture");
    let input = ModelInput {
        messages: Vec::new(),
        tools: Vec::new(),
        tool_choice: Value::String("none".to_owned()),
        max_tokens: 64,
        selected_model: Some("fixture".to_owned()),
        session_id: Some("session-1".to_owned()),
        thinking: None,
    };
    let request = ModelRequest {
        api: None,
        model: Some("fixture".to_owned()),
        url: None,
        thinking: None,
        session_id: Some("session-1".to_owned()),
    };
    let mut observer = RecordingObserver::default();
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("runtime");

    let output = runtime
        .block_on(gateway.generate(&input, &request, &mut observer, &ActiveAbort))
        .expect("model call succeeds");

    assert_eq!(
        output.message,
        object(json!({"role": "assistant", "content": "Hello"}))
    );
    assert!(output.streamed_content);
    assert_eq!(
        observer.observations,
        vec![Observation::TextDelta {
            text: "Hello".to_owned()
        }]
    );
}

#[test]
fn executor_driver_runs_one_capability_call() {
    let project = TempDir::new().expect("temporary project");
    write_project_file(
        &project,
        "zeta_executor_fixture.py",
        r#"
from zeta_plugin import executor, providers


@executor("fixture")
class Fixture:
    def __init__(self):
        self.open_count = 0

    async def open(self, request, context):
        if not request["workspace_bundle"]["id"].startswith("workspace:b3:"):
            raise ValueError("the workspace bundle is invalid")
        if not request["tool_bundle"]["id"].startswith("tools:b3:"):
            raise ValueError("the tool bundle is invalid")
        if request["capabilities"] != ["workspace.grep", "workspace.read"]:
            raise ValueError("the capability allow-list is invalid")
        self.open_count += 1
        name = request.get("instance_name", "call")
        return {"handle": f"{request['profile']}:{name}:handle"}

    async def call(self, request, context):
        if not request["handle"].startswith("isolated-code:"):
            raise ValueError("the handle is invalid")
        return {
            "capability": request["capability"],
            "input": request["input"],
            "effect_key": request.get("effect_key"),
            "open_count": self.open_count,
        }

    async def close(self, request, context):
        return {"closed": request["handle"]}


collection = providers(Fixture)
"#,
    );
    write_project_file(
        &project,
        "zeta_executor_fixture-1.0.dist-info/METADATA",
        "Metadata-Version: 2.1\nName: zeta-executor-fixture\nVersion: 1.0\n",
    );
    write_project_file(
        &project,
        "zeta_executor_fixture-1.0.dist-info/entry_points.txt",
        "[zeta.providers]\nfixture = zeta_executor_fixture:collection\n",
    );

    let host = PythonProviderHost::with_config(project.path(), host_config_with_fixture(&project))
        .expect("Python host starts");
    assert!(host.catalog().executors().contains_key("fixture"));

    let mut executor = PythonToolExecutor::with_executor(
        Arc::new(Mutex::new(host)),
        NativeToolExecutor::new(SystemCommandRunner),
        "fixture",
        "isolated-code",
        object(json!({"network": "none"})),
        ExecutorReuse::Call,
        None,
        executor_bundle(),
    );
    let invocation = CapabilityInvocation {
        capability_id: "workspace.read".parse().expect("capability id"),
        params: object(json!({"path": "src/lib.rs"})),
        base_directory: Some("/workspace".to_owned()),
        effect_key: Some("effect-1".to_owned()),
    };
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("runtime");
    let result = runtime
        .block_on(executor.execute(&invocation, &ActiveAbort))
        .expect("executor call succeeds");

    assert_eq!(
        result,
        object(json!({
            "capability": "workspace.read",
            "input": {"path": "src/lib.rs"},
            "effect_key": "effect-1",
            "open_count": 1,
        }))
    );
}

#[test]
fn executor_driver_reuses_a_session_handle() {
    let project = TempDir::new().expect("temporary project");
    write_project_file(
        &project,
        "zeta_executor_fixture.py",
        r#"
from zeta_plugin import executor, providers


@executor("fixture")
class Fixture:
    def __init__(self):
        self.open_count = 0
        self.handles = {}
        self.reconnected = {}

    async def open(self, request, context):
        if request["reuse"] != "session":
            raise ValueError("the reuse mode is invalid")
        if request["instance_name"] != "zeta-test-session":
            raise ValueError("the instance name is invalid")
        self.open_count += 1
        name = request["instance_name"]
        handle = self.handles.get(name)
        attached = handle is not None
        if handle is None:
            handle = "session:handle"
            self.handles[name] = handle
        self.reconnected[handle] = attached
        return {"handle": handle}

    async def call(self, request, context):
        return {
            "open_count": self.open_count,
            "handle": request["handle"],
            "reconnected": self.reconnected[request["handle"]],
        }

    async def close(self, request, context):
        if request["disposition"] != "release":
            raise ValueError("the close disposition is invalid")
        return {"closed": request["handle"]}


collection = providers(Fixture)
"#,
    );
    write_project_file(
        &project,
        "zeta_executor_fixture-1.0.dist-info/METADATA",
        "Metadata-Version: 2.1\nName: zeta-executor-fixture\nVersion: 1.0\n",
    );
    write_project_file(
        &project,
        "zeta_executor_fixture-1.0.dist-info/entry_points.txt",
        "[zeta.providers]\nfixture = zeta_executor_fixture:collection\n",
    );
    let host = Arc::new(Mutex::new(
        PythonProviderHost::with_config(project.path(), host_config_with_fixture(&project))
            .expect("Python host starts"),
    ));
    let mut executor = PythonToolExecutor::with_executor(
        Arc::clone(&host),
        NativeToolExecutor::new(SystemCommandRunner),
        "fixture",
        "isolated-code",
        object(json!({"network": "none"})),
        ExecutorReuse::Session,
        Some("zeta-test-session".to_owned()),
        executor_bundle(),
    );
    let invocation = CapabilityInvocation {
        capability_id: "workspace.read".parse().expect("capability id"),
        params: Map::new(),
        base_directory: Some("/workspace".to_owned()),
        effect_key: None,
    };
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("runtime");
    let first = runtime
        .block_on(executor.execute(&invocation, &ActiveAbort))
        .expect("first executor call succeeds");
    let second = runtime
        .block_on(executor.execute(&invocation, &ActiveAbort))
        .expect("second executor call succeeds");
    executor
        .finish()
        .expect("executor releases its session handle");
    let mut resumed = PythonToolExecutor::with_executor(
        host,
        NativeToolExecutor::new(SystemCommandRunner),
        "fixture",
        "isolated-code",
        object(json!({"network": "none"})),
        ExecutorReuse::Session,
        Some("zeta-test-session".to_owned()),
        executor_bundle(),
    );
    let resumed_result = runtime
        .block_on(resumed.execute(&invocation, &ActiveAbort))
        .expect("resumed executor call succeeds");
    resumed
        .finish()
        .expect("resumed executor releases its session handle");

    assert_eq!(
        first,
        object(json!({
            "open_count": 1,
            "handle": "session:handle",
            "reconnected": false,
        }))
    );
    assert_eq!(
        second,
        object(json!({
            "open_count": 1,
            "handle": "session:handle",
            "reconnected": false,
        }))
    );
    assert_eq!(
        resumed_result,
        object(json!({
            "open_count": 2,
            "handle": "session:handle",
            "reconnected": true,
        }))
    );
}
