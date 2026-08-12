//! Implements bounded shell execution with process-group deadlines.

use std::path::Path;
use std::time::Duration;

use serde_json::{json, Map, Value};

use super::{bounded_output, error_result, object, string_param, CommandOutput, CommandRunner};
use crate::{AbortSignal, AgentError};

const DEFAULT_TIMEOUT_SECONDS: f64 = 120.0;
const MAX_TIMEOUT_SECONDS: f64 = 600.0;
const MAX_OUTPUT_CHARS: usize = 12_000;

pub(super) fn bash<C: CommandRunner>(
    params: &Map<String, Value>,
    base_directory: &Path,
    commands: &mut C,
    abort: &dyn AbortSignal,
) -> Result<Map<String, Value>, AgentError> {
    let command = string_param(params, "command");
    let command = command.trim();
    if command.is_empty() {
        return Ok(error_result("missing-command", "missing command"));
    }
    let timeout = timeout_seconds(params.get("timeout"));
    let output = match commands.run_shell(
        command,
        base_directory,
        Duration::from_secs_f64(timeout),
        abort,
    ) {
        Ok(output) => output,
        Err(error) if error.kind() == std::io::ErrorKind::Interrupted => {
            return Err(AgentError::tool(error.to_string()))
        }
        Err(error) => return Ok(error_result("bash-failed", error.to_string())),
    };
    Ok(render_output(command, timeout, output))
}

fn timeout_seconds(value: Option<&Value>) -> f64 {
    let Some(value) = value.and_then(Value::as_f64) else {
        return DEFAULT_TIMEOUT_SECONDS;
    };
    value.clamp(1.0, MAX_TIMEOUT_SECONDS)
}

fn render_output(command: &str, timeout: f64, output: CommandOutput) -> Map<String, Value> {
    let (stdout, stdout_truncated) = bounded_output(&output.stdout, MAX_OUTPUT_CHARS);
    let (stderr, stderr_truncated) = bounded_output(&output.stderr, MAX_OUTPUT_CHARS);
    let text = direct_output_text(
        command,
        output.status,
        &stdout,
        &stderr,
        output.timed_out,
        timeout,
    );
    let ok = output.status == 0 && !output.timed_out;
    let mut result = object(json!({
        "ok": ok,
        "content": [{"type": "text", "text": text}],
        "metadata": {
            "command": command,
            "status": output.status,
            "duration_ms": output.duration_ms,
            "timed_out": output.timed_out,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
        },
    }));
    if output.timed_out {
        result.insert(
            "error".to_owned(),
            json!({
                "code": "bash-timeout",
                "message": format!(
                    "command timed out after {}s and was killed",
                    display_seconds(timeout),
                ),
            }),
        );
    } else if !ok {
        result.insert(
            "error".to_owned(),
            json!({
                "code": "bash-failed",
                "message": failure_message(&text, output.status),
            }),
        );
    }
    result
}

fn direct_output_text(
    command: &str,
    status: i32,
    stdout: &str,
    stderr: &str,
    timed_out: bool,
    timeout: f64,
) -> String {
    let mut sections = vec![format!("$ {command}"), format!("exit {status}")];
    if timed_out {
        sections.push(format!("timed out after {}s", display_seconds(timeout)));
    }
    if !stdout.is_empty() {
        sections.push("stdout:".to_owned());
        sections.push(stdout.to_owned());
    }
    if !stderr.is_empty() {
        sections.push("stderr:".to_owned());
        sections.push(stderr.to_owned());
    }
    sections.join("\n")
}

fn failure_message(text: &str, status: i32) -> String {
    let summary = failure_summary(text);
    if !summary.is_empty() {
        return summary;
    }
    let flattened = flatten_text(text);
    if !flattened.is_empty() {
        return flattened;
    }
    format!("exit status {status}")
}

fn failure_summary(text: &str) -> String {
    let markers = [
        "error:",
        "Error:",
        "Exception:",
        "exceptions.",
        "TimeoutError:",
        "Unexpected",
        "No such file",
        "not found",
        "/bin/sh:",
    ];
    for line in text.lines().rev() {
        let line = line.trim();
        if line.starts_with("raise ") {
            continue;
        }
        for marker in markers {
            if line.contains(marker) {
                return line.to_owned();
            }
        }
    }
    String::new()
}

fn flatten_text(text: &str) -> String {
    let mut words = Vec::new();
    for word in text.split_whitespace() {
        words.push(word);
    }
    words.join(" ")
}

fn display_seconds(seconds: f64) -> String {
    if seconds.fract() == 0.0 {
        return format!("{seconds:.0}");
    }
    seconds.to_string()
}
