"""Bash tool implementation."""

import os
import signal
import subprocess
import time
from typing import Any

from zeta.capabilities.execution import error_result, proposed_command_effect
from zeta.capabilities.types import Capability, CapabilityId

DEFAULT_TIMEOUT_SECONDS = 120.0
MAX_OUTPUT_CHARS = 12_000

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["command"],
    "properties": {
        "command": {"type": "string"},
        "reason": {"type": "string"},
    },
}

SPEC = Capability(
    CapabilityId("commas", "bash"),
    "Execute or stage a shell command, depending on the active workflow.",
    SCHEMA,
)


def stage(params: dict[str, Any]) -> dict[str, Any]:
    command = str(params.get("command") or "").strip()
    if not command:
        return error_result("missing-command", "missing command")
    return proposed_command_effect(
        command,
        str(params.get("reason") or "Run the proposed command."),
    )


def run(params: dict[str, Any]) -> dict[str, Any]:
    command = str(params.get("command") or "").strip()
    if not command:
        return error_result("missing-command", "missing command")
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return error_result("bash-failed", str(exc))
    timed_out = False
    try:
        stdout_bytes, stderr_bytes = proc.communicate(timeout=DEFAULT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_process_group(proc)
        stdout_bytes, stderr_bytes = proc.communicate()
    duration_ms = int((time.monotonic() - started) * 1000)
    stdout, stdout_truncated = bounded_output(decode_output(stdout_bytes))
    stderr, stderr_truncated = bounded_output(decode_output(stderr_bytes))
    status = proc.returncode
    text = direct_output_text(command, status, stdout, stderr, timed_out=timed_out)
    result: dict[str, Any] = {
        "ok": status == 0 and not timed_out,
        "content": [
            {
                "type": "text",
                "text": text,
            }
        ],
        "metadata": {
            "mode": "direct",
            "command": command,
            "status": status,
            "duration_ms": duration_ms,
            "timed_out": timed_out,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
        },
    }
    if timed_out:
        result["error"] = {
            "code": "bash-timeout",
            "message": (
                f"command timed out after {DEFAULT_TIMEOUT_SECONDS:g}s and was killed"
            ),
        }
    elif result["ok"] is False:
        result["error"] = {
            "code": "bash-failed",
            "message": bash_failure_message(text, status),
        }
    return result


def kill_process_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()


def decode_output(data: bytes | None) -> str:
    return (data or b"").decode("utf-8", errors="replace")


def bounded_output(text: str, limit: int = MAX_OUTPUT_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    head = text[: limit // 2]
    tail = text[len(text) - limit // 2 :]
    omitted = len(text) - len(head) - len(tail)
    return f"{head}\n... {omitted} characters truncated ...\n{tail}", True


def direct_output_text(
    command: str,
    status: int,
    stdout: str,
    stderr: str,
    *,
    timed_out: bool = False,
) -> str:
    sections = [
        f"$ {command}",
        f"exit {status}",
    ]
    if timed_out:
        sections.append(f"timed out after {DEFAULT_TIMEOUT_SECONDS:g}s")
    if stdout:
        sections.extend(["stdout:", stdout])
    if stderr:
        sections.extend(["stderr:", stderr])
    return "\n".join(sections)


def bash_failure_message(text: str, status: int) -> str:
    return (
        bash_failure_summary(text) or flatten_tool_text(text) or f"exit status {status}"
    )


def bash_failure_summary(text: str) -> str:
    markers = (
        "error:",
        "Error:",
        "Exception:",
        "exceptions.",
        "TimeoutError:",
        "Unexpected",
        "No such file",
        "not found",
        "/bin/sh:",
    )
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped.startswith("raise "):
            continue
        if any(marker in stripped for marker in markers):
            return stripped
    return ""


def flatten_tool_text(text: str) -> str:
    return " ".join(text.strip().split())
