//! Shared Python and Rust native-tool conformance vectors.

use std::collections::VecDeque;
use std::fs;
use std::future::Future;
use std::io;
use std::net::IpAddr;
use std::path::{Path, PathBuf};
use std::pin::pin;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::task::{Context, Poll, Wake, Waker};
use std::time::Duration;

use serde::Deserialize;
use serde_json::{json, Map, Value};
use zeta_agent::{
    bounded_output, native_capabilities, AbortReason, AbortSignal, CapabilityInvocation,
    CommandOutput, CommandRunner, HttpFuture, HttpResponse, HttpTransport, NativeToolExecutor,
    SystemCommandRunner, ToolExecutor, WebSearchFuture, WebSearchProvider, WebSearchResult,
    WebSearchSource,
};

static NEXT_TEMP_ID: AtomicU64 = AtomicU64::new(0);

#[derive(Deserialize)]
struct ToolVectors {
    bounds: Bounds,
    capabilities: Vec<Value>,
    cases: Vec<ToolCase>,
}

#[derive(Deserialize)]
struct Bounds {
    bash_output: BashOutputBound,
}

#[derive(Deserialize)]
struct BashOutputBound {
    expected_text: String,
    expected_truncated: bool,
    input: String,
    limit: usize,
}

#[derive(Deserialize)]
struct ToolCase {
    name: String,
    capability: String,
    fixture: ToolFixture,
    input: Map<String, Value>,
    expected: Value,
    #[serde(default)]
    expected_provider_calls: Vec<Value>,
    #[serde(default)]
    expected_artifact: Option<String>,
    #[serde(default)]
    expected_files: Map<String, Value>,
}

#[derive(Default, Deserialize)]
struct ToolFixture {
    #[serde(default)]
    command: Option<String>,
    #[serde(default)]
    command_output: Option<String>,
    #[serde(default)]
    directories: Vec<String>,
    #[serde(default)]
    files: Map<String, Value>,
    #[serde(default)]
    provider_result: Option<Value>,
    #[serde(default)]
    resolved_addresses: Vec<IpAddr>,
    #[serde(default)]
    response: Option<HttpFixtureResponse>,
}

#[derive(Clone, Deserialize)]
struct HttpFixtureResponse {
    body_utf8: String,
    content_type: String,
}

struct TempWorkspace {
    path: PathBuf,
}

impl TempWorkspace {
    fn new() -> Self {
        let id = NEXT_TEMP_ID.fetch_add(1, Ordering::Relaxed);
        let path = std::env::temp_dir().join(format!(
            "zeta-agent-tool-vectors-{}-{id}",
            std::process::id()
        ));
        fs::create_dir(&path).unwrap();
        TempWorkspace { path }
    }
}

impl Drop for TempWorkspace {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.path).unwrap();
    }
}

struct FixtureCommands {
    fallback_grep: bool,
    outputs: VecDeque<CommandOutput>,
}

impl CommandRunner for FixtureCommands {
    fn run(
        &mut self,
        program: &str,
        _arguments: &[String],
        _base_directory: &Path,
        _abort: &dyn AbortSignal,
    ) -> io::Result<CommandOutput> {
        if program == "rg" && self.fallback_grep {
            return Err(io::Error::new(io::ErrorKind::NotFound, "fixture fallback"));
        }
        let Some(output) = self.outputs.pop_front() else {
            return Err(io::Error::new(
                io::ErrorKind::NotFound,
                format!("unexpected fixture command: {program}"),
            ));
        };
        Ok(output)
    }

    fn run_shell(
        &mut self,
        command: &str,
        _base_directory: &Path,
        _timeout: Duration,
        _abort: &dyn AbortSignal,
    ) -> io::Result<CommandOutput> {
        Err(io::Error::new(
            io::ErrorKind::Unsupported,
            format!("unexpected fixture shell command: {command}"),
        ))
    }
}

struct FixtureHttp {
    addresses: Vec<IpAddr>,
    response: Option<HttpFixtureResponse>,
    fetches: Arc<Mutex<Vec<String>>>,
}

impl HttpTransport for FixtureHttp {
    fn resolve_host<'a>(
        &'a mut self,
        _host: &'a str,
        _abort: &'a dyn AbortSignal,
    ) -> HttpFuture<'a, Vec<IpAddr>> {
        let addresses = self.addresses.clone();
        Box::pin(async move { Ok(addresses) })
    }

    fn fetch<'a>(
        &'a mut self,
        url: &'a str,
        _addresses: &'a [IpAddr],
        _timeout: Duration,
        _abort: &'a dyn AbortSignal,
    ) -> HttpFuture<'a, HttpResponse> {
        self.fetches.lock().unwrap().push(url.to_owned());
        let response = self.response.clone();
        Box::pin(async move {
            let Some(response) = response else {
                return Err(io::Error::other("unexpected fixture fetch"));
            };
            Ok(HttpResponse::new(
                response.body_utf8.into_bytes(),
                response.content_type,
            ))
        })
    }
}

struct FixtureWebSearch {
    config: Value,
    result: Option<WebSearchResult>,
    calls: Arc<Mutex<Vec<Value>>>,
}

impl WebSearchProvider for FixtureWebSearch {
    fn search<'a>(
        &'a mut self,
        query: &'a str,
        limit: usize,
        _abort: &'a dyn AbortSignal,
    ) -> WebSearchFuture<'a> {
        self.calls.lock().unwrap().push(json!({
            "query": query,
            "config": self.config,
        }));
        let result = self.result.clone();
        Box::pin(async move {
            let Some(result) = result else {
                return Err("unexpected fixture search".to_owned());
            };
            assert!(limit > 0);
            Ok(result)
        })
    }
}

struct NoopWake;

struct ActiveAbort;

impl AbortSignal for ActiveAbort {
    fn reason(&self) -> Option<AbortReason> {
        None
    }
}

#[derive(Clone, Default)]
struct SharedAbort {
    cancelled: Arc<AtomicBool>,
}

impl SharedAbort {
    fn cancel(&self) {
        self.cancelled.store(true, Ordering::SeqCst);
    }
}

impl AbortSignal for SharedAbort {
    fn reason(&self) -> Option<AbortReason> {
        self.cancelled
            .load(Ordering::SeqCst)
            .then_some(AbortReason::Cancelled)
    }
}

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

fn vectors() -> ToolVectors {
    let workspace = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap();
    let path = workspace.join("spec/vectors/agent/tools.json");
    serde_json::from_slice(&fs::read(path).unwrap()).unwrap()
}

fn materialize_fixture(case: &ToolCase, workspace: &Path) {
    for directory in &case.fixture.directories {
        fs::create_dir_all(workspace.join(directory)).unwrap();
    }
    for (path, value) in &case.fixture.files {
        let path = workspace.join(path);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, value.as_str().unwrap()).unwrap();
    }
}

fn fixture_commands(case: &ToolCase, workspace: &Path) -> FixtureCommands {
    let mut outputs = VecDeque::new();
    if let Some(output) = &case.fixture.command_output {
        outputs.push_back(CommandOutput {
            status: 0,
            stdout: output.replace("<workspace>", &workspace.display().to_string()),
            stderr: String::new(),
            timed_out: false,
            duration_ms: 0,
        });
    }
    FixtureCommands {
        fallback_grep: case.fixture.command.as_deref() == Some("fallback"),
        outputs,
    }
}

fn normalize(value: Value, workspace: &Path) -> Value {
    let marker = "<workspace>";
    let workspace = workspace.display().to_string();
    match value {
        Value::Null => Value::Null,
        Value::Bool(value) => Value::Bool(value),
        Value::Number(value) => Value::Number(value),
        Value::String(value) => Value::String(value.replace(&workspace, marker)),
        Value::Array(values) => {
            let mut normalized = Vec::with_capacity(values.len());
            for value in values {
                normalized.push(normalize(value, Path::new(&workspace)));
            }
            Value::Array(normalized)
        }
        Value::Object(values) => {
            let mut normalized = Map::new();
            for (key, value) in values {
                let key = key.replace(&workspace, marker);
                normalized.insert(key, normalize(value, Path::new(&workspace)));
            }
            Value::Object(normalized)
        }
    }
}

fn normalize_artifact(actual: &mut Value, expected_artifact: &Option<String>, workspace: &Path) {
    let Some(expected_artifact) = expected_artifact else {
        return;
    };
    let metadata = actual
        .as_object_mut()
        .unwrap()
        .get_mut("metadata")
        .unwrap()
        .as_object_mut()
        .unwrap();
    let artifact = metadata.get("artifact").unwrap().as_str().unwrap();
    let artifact = PathBuf::from(artifact);
    let artifact_text = fs::read_to_string(&artifact).unwrap();
    fs::remove_file(artifact).unwrap();
    metadata.insert(
        "artifact".to_owned(),
        Value::String("<artifact>".to_owned()),
    );
    assert_eq!(
        normalize(Value::String(artifact_text), workspace),
        Value::String(expected_artifact.clone())
    );
}

fn verify_files(case: &ToolCase, workspace: &Path) {
    for (path, expected) in &case.expected_files {
        assert_eq!(
            fs::read_to_string(workspace.join(path)).unwrap(),
            expected.as_str().unwrap(),
            "file output for {}",
            case.name
        );
    }
}

fn execute_system(capability: &str, params: Map<String, Value>, workspace: &Path) -> Value {
    let mut executor = NativeToolExecutor::<SystemCommandRunner>::default();
    let invocation = CapabilityInvocation {
        capability_id: capability.parse().unwrap(),
        params,
        base_directory: Some(workspace.display().to_string()),
        effect_key: None,
    };
    Value::Object(block_on(executor.execute(&invocation, &ActiveAbort)).unwrap())
}

fn fixture_search_result(value: Option<&Value>) -> Option<WebSearchResult> {
    let value = value?.as_object()?;
    let mut sources = Vec::new();
    for source in value.get("sources")?.as_array()? {
        let source = source.as_object()?;
        sources.push(WebSearchSource {
            title: source.get("title")?.as_str()?.to_owned(),
            url: source.get("url")?.as_str()?.to_owned(),
            snippet: source.get("snippet")?.as_str()?.to_owned(),
        });
    }
    Some(WebSearchResult {
        answer: value.get("answer")?.as_str()?.to_owned(),
        sources,
        request_id: value
            .get("request_id")
            .and_then(Value::as_str)
            .map(str::to_owned),
        model: value
            .get("model")
            .and_then(Value::as_str)
            .map(str::to_owned),
        usage: value.get("usage").and_then(Value::as_object).cloned(),
    })
}

fn normalize_duration(actual: &mut Value) {
    let metadata = actual
        .as_object_mut()
        .unwrap()
        .get_mut("metadata")
        .unwrap()
        .as_object_mut()
        .unwrap();
    assert!(metadata.get("duration_ms").unwrap().as_u64().is_some());
    metadata.insert(
        "duration_ms".to_owned(),
        Value::String("<duration-ms>".to_owned()),
    );
}

fn is_local_case(case: &ToolCase) -> bool {
    if case.capability == "zeta.bash" || case.capability == "zeta.web_search" {
        return false;
    }
    let Some(path) = case.input.get("path").and_then(Value::as_str) else {
        return true;
    };
    !path.starts_with("http://") && !path.starts_with("https://")
}

#[test]
fn native_capability_declarations_match_python_ground_truth() {
    let vectors = vectors();
    assert_eq!(
        serde_json::to_value(native_capabilities()).unwrap(),
        Value::Array(vectors.capabilities)
    );
}

#[test]
fn local_native_tool_vectors_match_python_ground_truth() {
    let vectors = vectors();
    let mut exercised = Vec::new();
    for case in &vectors.cases {
        if !is_local_case(case) {
            continue;
        }
        let workspace = TempWorkspace::new();
        materialize_fixture(case, &workspace.path);
        let commands = fixture_commands(case, &workspace.path);
        let mut executor = NativeToolExecutor::new(commands);
        let invocation = CapabilityInvocation {
            capability_id: case.capability.parse().unwrap(),
            params: case.input.clone(),
            base_directory: Some(workspace.path.display().to_string()),
            effect_key: None,
        };
        let mut actual =
            Value::Object(block_on(executor.execute(&invocation, &ActiveAbort)).unwrap());
        normalize_artifact(&mut actual, &case.expected_artifact, &workspace.path);
        assert_eq!(
            normalize(actual, &workspace.path),
            case.expected,
            "native tool case {}",
            case.name
        );
        verify_files(case, &workspace.path);
        exercised.push(case.name.as_str());
    }
    assert_eq!(
        exercised,
        vec![
            "read_utf8_lines",
            "write_existing_file",
            "edit_exact_replacement",
            "patch_updates_file",
            "list_directory",
            "grep_limited_matches",
            "ast_grep_structural_match",
        ]
    );
}

#[test]
fn bounded_output_matches_python_ground_truth() {
    let bound = vectors().bounds.bash_output;
    assert_eq!(
        bounded_output(&bound.input, bound.limit),
        (bound.expected_text, bound.expected_truncated)
    );
}

#[test]
fn bash_vector_matches_python_ground_truth() {
    let vectors = vectors();
    let case = vectors
        .cases
        .iter()
        .find(|case| case.name == "bash_success")
        .unwrap();
    let workspace = TempWorkspace::new();
    let mut actual = execute_system(&case.capability, case.input.clone(), &workspace.path);
    normalize_duration(&mut actual);
    assert_eq!(actual, case.expected);
}

#[test]
fn bash_reports_nonzero_exit_and_bounds_both_output_streams() {
    let workspace = TempWorkspace::new();
    let command = "printf '%012010d' 0; printf '%012011d' 0 >&2; exit 7";
    let result = execute_system(
        "zeta.bash",
        object(json!({"command": command, "timeout": 5})),
        &workspace.path,
    );
    assert_eq!(result["ok"], false);
    assert_eq!(result["error"]["code"], "bash-failed");
    assert_eq!(result["metadata"]["status"], 7);
    assert_eq!(result["metadata"]["timed_out"], false);
    assert_eq!(result["metadata"]["stdout_truncated"], true);
    assert_eq!(result["metadata"]["stderr_truncated"], true);
    let text = result["content"][0]["text"].as_str().unwrap();
    assert!(text.contains("... 10 characters truncated ..."));
    assert!(text.contains("... 11 characters truncated ..."));
}

#[cfg(unix)]
#[test]
fn bash_timeout_kills_the_descendant_process_group() {
    let workspace = TempWorkspace::new();
    let command = "(sleep 2; printf survived > descendant.txt) & wait";
    let result = execute_system(
        "zeta.bash",
        object(json!({"command": command, "timeout": 1})),
        &workspace.path,
    );
    assert_eq!(result["ok"], false);
    assert_eq!(result["error"]["code"], "bash-timeout");
    assert_eq!(
        result["error"]["message"],
        "command timed out after 1s and was killed"
    );
    assert_eq!(result["metadata"]["status"], -9);
    assert_eq!(result["metadata"]["timed_out"], true);
    std::thread::sleep(Duration::from_millis(1_250));
    assert!(!workspace.path.join("descendant.txt").exists());
}

#[cfg(unix)]
#[test]
fn bash_cancellation_kills_the_descendant_process_group() {
    let workspace = TempWorkspace::new();
    let mut executor = NativeToolExecutor::<SystemCommandRunner>::default();
    let invocation = CapabilityInvocation {
        capability_id: "zeta.bash".parse().unwrap(),
        params: object(json!({
            "command": "(sleep 1; printf survived > descendant.txt) & wait",
            "timeout": 5,
        })),
        base_directory: Some(workspace.path.display().to_string()),
        effect_key: None,
    };
    let abort = SharedAbort::default();
    let trigger = abort.clone();
    let canceller = std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(50));
        trigger.cancel();
    });

    let error = block_on(executor.execute(&invocation, &abort)).unwrap_err();

    canceller.join().unwrap();
    assert_eq!(error.message, "tool execution aborted: cancelled");
    std::thread::sleep(Duration::from_millis(1_100));
    assert!(!workspace.path.join("descendant.txt").exists());
}

#[cfg(unix)]
#[test]
fn command_cancellation_kills_the_descendant_process_group() {
    let workspace = TempWorkspace::new();
    let mut runner = SystemCommandRunner;
    let abort = SharedAbort::default();
    let trigger = abort.clone();
    let canceller = std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(50));
        trigger.cancel();
    });
    let arguments = vec![
        "-c".to_owned(),
        "(sleep 1; printf survived > descendant.txt) & wait".to_owned(),
    ];

    let error = match runner.run("/bin/sh", &arguments, &workspace.path, &abort) {
        Ok(_output) => panic!("the cancelled command must stop"),
        Err(error) => error,
    };

    canceller.join().unwrap();
    assert_eq!(error.kind(), io::ErrorKind::Interrupted);
    std::thread::sleep(Duration::from_millis(1_100));
    assert!(!workspace.path.join("descendant.txt").exists());
}

#[test]
fn public_url_read_vector_matches_python_ground_truth() {
    let vectors = vectors();
    let case = vectors
        .cases
        .iter()
        .find(|case| case.name == "read_public_html_url")
        .unwrap();
    let workspace = TempWorkspace::new();
    let fetches = Arc::new(Mutex::new(Vec::new()));
    let http = FixtureHttp {
        addresses: case.fixture.resolved_addresses.clone(),
        response: case.fixture.response.clone(),
        fetches: Arc::clone(&fetches),
    };
    let web = FixtureWebSearch {
        config: Value::Null,
        result: None,
        calls: Arc::new(Mutex::new(Vec::new())),
    };
    let mut executor = NativeToolExecutor::with_network(SystemCommandRunner, http, web);
    let invocation = CapabilityInvocation {
        capability_id: case.capability.parse().unwrap(),
        params: case.input.clone(),
        base_directory: Some(workspace.path.display().to_string()),
        effect_key: None,
    };
    let actual = Value::Object(block_on(executor.execute(&invocation, &ActiveAbort)).unwrap());
    assert_eq!(actual, case.expected);
    assert_eq!(
        fetches.lock().unwrap().as_slice(),
        ["https://example.com/page"]
    );
}

#[test]
fn private_url_read_is_blocked_before_fetch() {
    let vectors = vectors();
    let case = vectors
        .cases
        .iter()
        .find(|case| case.name == "read_blocks_private_url")
        .unwrap();
    let workspace = TempWorkspace::new();
    let fetches = Arc::new(Mutex::new(Vec::new()));
    let http = FixtureHttp {
        addresses: case.fixture.resolved_addresses.clone(),
        response: None,
        fetches: Arc::clone(&fetches),
    };
    let web = FixtureWebSearch {
        config: Value::Null,
        result: None,
        calls: Arc::new(Mutex::new(Vec::new())),
    };
    let mut executor = NativeToolExecutor::with_network(SystemCommandRunner, http, web);
    let invocation = CapabilityInvocation {
        capability_id: case.capability.parse().unwrap(),
        params: case.input.clone(),
        base_directory: Some(workspace.path.display().to_string()),
        effect_key: None,
    };
    let actual = Value::Object(block_on(executor.execute(&invocation, &ActiveAbort)).unwrap());
    assert_eq!(actual, case.expected);
    assert!(fetches.lock().unwrap().is_empty());
}

#[test]
fn web_search_vector_matches_python_ground_truth() {
    let vectors = vectors();
    let case = vectors
        .cases
        .iter()
        .find(|case| case.name == "web_search_formats_provider_result")
        .unwrap();
    let workspace = TempWorkspace::new();
    let calls = Arc::new(Mutex::new(Vec::new()));
    let config = case.expected_provider_calls[0]["config"].clone();
    let http = FixtureHttp {
        addresses: Vec::new(),
        response: None,
        fetches: Arc::new(Mutex::new(Vec::new())),
    };
    let web = FixtureWebSearch {
        config,
        result: fixture_search_result(case.fixture.provider_result.as_ref()),
        calls: Arc::clone(&calls),
    };
    let mut executor = NativeToolExecutor::with_network(SystemCommandRunner, http, web);
    let invocation = CapabilityInvocation {
        capability_id: case.capability.parse().unwrap(),
        params: case.input.clone(),
        base_directory: Some(workspace.path.display().to_string()),
        effect_key: None,
    };
    let actual = Value::Object(block_on(executor.execute(&invocation, &ActiveAbort)).unwrap());
    assert_eq!(actual, case.expected);
    assert_eq!(*calls.lock().unwrap(), case.expected_provider_calls);
}

fn object(value: Value) -> Map<String, Value> {
    let Value::Object(value) = value else {
        panic!("fixture params must be a JSON object")
    };
    value
}
