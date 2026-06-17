"""Bash tool implementation."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Any

from zeta.tools.base import (
    CapabilityId,
    CapabilitySpec,
    error_result,
    proposed_command_effect,
)

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

SPEC = CapabilitySpec(
    CapabilityId("sigil", "bash"),
    "Execute or stage a shell command, depending on the active workflow.",
    SCHEMA,
    interactive=True,
    effects=("execute",),
    aliases=("bash",),
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
    result: dict[str, Any] = {
        "ok": status == 0 and not timed_out,
        "content": [
            {
                "type": "text",
                "text": direct_output_text(
                    command, status, stdout, stderr, timed_out=timed_out
                ),
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
