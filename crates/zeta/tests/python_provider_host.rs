use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use serde_json::{Map, Value, json};
use tempfile::TempDir;
use zeta::{PythonModelGateway, PythonProviderHost, PythonProviderHostConfig};
use zeta_agent::{
    AbortReason, AbortSignal, AgentObserver, ModelGateway, ModelInput, ModelRequest, Observation,
};

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


@tool("fail")
async def fail(request, context):
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
    let host = PythonProviderHost::with_config(project.path(), host_config())
        .expect("Python host starts");
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

    assert_eq!(output.message, object(json!({"role": "assistant", "content": "Hello"})));
    assert!(output.streamed_content);
    assert_eq!(
        observer.observations,
        vec![Observation::TextDelta {
            text: "Hello".to_owned()
        }]
    );
}
