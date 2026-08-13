use std::collections::BTreeMap;
use std::future::Future;
use std::path::{Path, PathBuf};
use std::time::Duration;

use serde_json::{json, Map, Value};
use tempfile::TempDir;
use zeta::{CancellationToken, ProcessExecutor, ProcessExecutorConfig, ProcessLaunch};
use zeta_agent::{AbortReason, AbortSignal, CapabilityExecutor, CapabilityInvocation};

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

fn fixture_path() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("fixtures")
        .join("capability_provider.py")
}

fn marker_launch(marker: &Path) -> ProcessLaunch {
    ProcessLaunch {
        extension_id: "test.reference".to_owned(),
        argv: vec![
            "python3".to_owned(),
            fixture_path().display().to_string(),
            marker.display().to_string(),
        ],
        working_directory: None,
        environment: BTreeMap::new(),
    }
}

fn effectful_invocation(input: Value) -> CapabilityInvocation {
    CapabilityInvocation {
        capability_id: "test.deliver".parse().expect("valid capability id"),
        params: object(input),
        base_directory: Some("/workspace/project".to_owned()),
        effect_key: Some("effect-vector".to_owned()),
    }
}

fn read_only_invocation(input: Value) -> CapabilityInvocation {
    CapabilityInvocation {
        capability_id: "test.deliver".parse().expect("valid capability id"),
        params: object(input),
        base_directory: Some("/workspace/project".to_owned()),
        effect_key: None,
    }
}

fn run<T>(future: impl Future<Output = T>) -> T {
    let mut future = Box::pin(future);
    let waker = std::task::Waker::noop();
    let mut context = std::task::Context::from_waker(waker);
    match future.as_mut().poll(&mut context) {
        std::task::Poll::Ready(value) => value,
        std::task::Poll::Pending => panic!("the blocking executor future was pending"),
    }
}

#[test]
fn process_executor_initializes_and_calls_a_declared_direct_method() {
    let temp = TempDir::new().expect("temporary directory");
    let marker = temp.path().join("runs");
    let mut executor = ProcessExecutor::new(marker_launch(&marker)).expect("valid launch");

    let invocation = effectful_invocation(json!({"text": "hello"}));
    let result =
        run(executor.execute(&invocation, &ActiveAbort)).expect("the provider call must succeed");

    assert_eq!(
        result,
        object(json!({
            "ok": true,
            "delivered": "hello",
            "effect_key": "effect-vector",
            "base_dir": "/workspace/project",
        }))
    );
    assert_eq!(executor.initialization_count(), 1);
}

#[test]
fn process_executor_calls_read_only_methods_without_an_effect_key() {
    let temp = TempDir::new().expect("temporary directory");
    let marker = temp.path().join("runs");
    let mut executor = ProcessExecutor::new(marker_launch(&marker)).expect("valid launch");
    let invocation = read_only_invocation(json!({"text": "inspect"}));

    let result =
        run(executor.execute(&invocation, &ActiveAbort)).expect("the provider call must succeed");

    assert_eq!(result["delivered"], "inspect");
    assert_eq!(result["effect_key"], Value::Null);
    assert_eq!(result["base_dir"], "/workspace/project");
}

#[test]
fn process_executor_preserves_provider_retryability() {
    let temp = TempDir::new().expect("temporary directory");
    let marker = temp.path().join("runs");
    let mut executor = ProcessExecutor::new(marker_launch(&marker)).expect("valid launch");

    let invocation = effectful_invocation(json!({"fail": true}));
    let error = run(executor.execute(&invocation, &ActiveAbort))
        .expect_err("the provider must reject the call");

    assert_eq!(
        error.message,
        "provider call failed [provider_rejected, retryable=true]: rejected by fixture"
    );

    let again = effectful_invocation(json!({"text": "again"}));
    let result =
        run(executor.execute(&again, &ActiveAbort)).expect("the provider must remain reusable");
    assert_eq!(result["delivered"], "again");
    assert_eq!(executor.initialization_count(), 1);
}

#[test]
fn process_executor_rejects_undeclared_methods_locally() {
    let temp = TempDir::new().expect("temporary directory");
    let marker = temp.path().join("runs");
    let mut executor = ProcessExecutor::new(marker_launch(&marker)).expect("valid launch");
    let mut invocation = effectful_invocation(json!({}));
    invocation.capability_id = "test.undeclared".parse().expect("valid capability id");

    let error =
        run(executor.execute(&invocation, &ActiveAbort)).expect_err("method must be rejected");

    assert_eq!(
        error.message,
        "provider 'test.reference' did not declare method 'test.undeclared'"
    );
}

#[test]
fn process_executor_times_out_and_recycles_the_child() {
    let temp = TempDir::new().expect("temporary directory");
    let marker = temp.path().join("runs");
    let config = ProcessExecutorConfig {
        handshake_timeout: Duration::from_secs(2),
        call_timeout: Duration::from_millis(50),
        shutdown_timeout: Duration::from_millis(200),
    };
    let mut executor =
        ProcessExecutor::with_config(marker_launch(&marker), config).expect("valid launch");

    let slow = effectful_invocation(json!({"slow": true}));
    let error = run(executor.execute(&slow, &ActiveAbort)).expect_err("the provider must time out");
    assert_eq!(error.message, "provider call 'test.deliver' timed out");

    let again = effectful_invocation(json!({"text": "again"}));
    let result =
        run(executor.execute(&again, &ActiveAbort)).expect("the recycled provider must succeed");
    assert_eq!(result["delivered"], "again");
    assert_eq!(executor.initialization_count(), 2);
}

#[test]
fn process_executor_recycles_after_child_exit() {
    let temp = TempDir::new().expect("temporary directory");
    let marker = temp.path().join("runs");
    let mut executor = ProcessExecutor::new(marker_launch(&marker)).expect("valid launch");

    let exit = effectful_invocation(json!({"exit": true}));
    let error =
        run(executor.execute(&exit, &ActiveAbort)).expect_err("the exiting provider must fail");
    assert!(error.message.contains("closed stdout"));

    let again = effectful_invocation(json!({"text": "again"}));
    let result =
        run(executor.execute(&again, &ActiveAbort)).expect("the recycled provider must succeed");
    assert_eq!(result["delivered"], "again");
    assert_eq!(executor.initialization_count(), 2);
}

#[test]
fn process_executor_recycles_after_a_non_object_result() {
    let temp = TempDir::new().expect("temporary directory");
    let marker = temp.path().join("runs");
    let mut executor = ProcessExecutor::new(marker_launch(&marker)).expect("valid launch");

    let invalid = effectful_invocation(json!({"non_object": true}));
    let error = run(executor.execute(&invalid, &ActiveAbort))
        .expect_err("the provider result must be an object");
    assert_eq!(
        error.message,
        "JSON-RPC error -32600: the result of \"test.deliver\" must be an object"
    );
    assert!(!executor.is_running());

    let again = effectful_invocation(json!({"text": "again"}));
    let result =
        run(executor.execute(&again, &ActiveAbort)).expect("the recycled provider must succeed");
    assert_eq!(result["delivered"], "again");
    assert_eq!(executor.initialization_count(), 2);
}

#[cfg(unix)]
#[test]
fn process_executor_cancellation_kills_descendants_and_recycles() {
    let temp = TempDir::new().expect("temporary directory");
    let runs = temp.path().join("runs");
    let descendant = temp.path().join("descendant");
    let mut executor = ProcessExecutor::new(marker_launch(&runs)).expect("valid launch");
    let invocation = effectful_invocation(json!({
        "descendant_marker": descendant.display().to_string(),
    }));
    let abort = CancellationToken::new();
    let trigger = abort.clone();
    let canceller = std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(100));
        trigger.cancel(AbortReason::Cancelled);
    });

    let error = run(executor.execute(&invocation, &abort))
        .expect_err("the cancelled provider call must stop");

    canceller.join().expect("cancellation thread");
    assert_eq!(
        error.message,
        "provider call 'test.deliver' aborted: cancelled"
    );
    assert!(!executor.is_running());
    std::thread::sleep(Duration::from_millis(1_100));
    assert!(!descendant.exists());

    let again = effectful_invocation(json!({"text": "again"}));
    let result =
        run(executor.execute(&again, &ActiveAbort)).expect("the recycled provider must succeed");
    assert_eq!(result["delivered"], "again");
    assert_eq!(executor.initialization_count(), 2);
}

#[test]
fn process_executor_does_not_spawn_for_a_pre_cancelled_call() {
    let temp = TempDir::new().expect("temporary directory");
    let marker = temp.path().join("runs");
    let mut executor = ProcessExecutor::new(marker_launch(&marker)).expect("valid launch");
    let invocation = effectful_invocation(json!({"text": "never"}));
    let abort = CancellationToken::new();
    assert!(abort.cancel(AbortReason::Cancelled));

    let error = run(executor.execute(&invocation, &abort))
        .expect_err("a pre-cancelled provider call must stop");

    assert_eq!(error.message, "provider call aborted: cancelled");
    assert_eq!(executor.initialization_count(), 0);
    assert!(!executor.is_running());
    assert!(!marker.exists());
}

#[test]
fn process_executor_shutdown_is_orderly() {
    let temp = TempDir::new().expect("temporary directory");
    let marker = temp.path().join("runs");
    let mut executor = ProcessExecutor::new(marker_launch(&marker)).expect("valid launch");
    let invocation = effectful_invocation(json!({"text": "hello"}));
    run(executor.execute(&invocation, &ActiveAbort)).expect("the provider call must succeed");

    executor.shutdown().expect("the provider must shut down");

    assert!(!executor.is_running());
}

#[test]
fn process_launch_rejects_an_empty_command() {
    let error = ProcessExecutor::new(ProcessLaunch {
        extension_id: "test.reference".to_owned(),
        argv: Vec::new(),
        working_directory: None,
        environment: BTreeMap::new(),
    })
    .expect_err("an executable is required");

    assert_eq!(error.message, "process launch argv must not be empty");
}
