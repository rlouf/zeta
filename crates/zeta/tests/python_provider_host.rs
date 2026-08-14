use std::fs;
use std::path::{Path, PathBuf};

use serde_json::{Map, Value, json};
use tempfile::TempDir;
use zeta::{PythonProviderHost, PythonProviderHostConfig};
use zeta_agent::{AbortReason, AbortSignal};

struct ActiveAbort;

impl AbortSignal for ActiveAbort {
    fn reason(&self) -> Option<AbortReason> {
        None
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
            project
                .path()
                .join("tools/echo.py")
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
