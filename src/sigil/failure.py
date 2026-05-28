"""Failure context captured by shell hooks for later comma proposals."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .ansi import LOVE, RESET
from .security import create_trust_metadata
from .state import append_event, read_json, write_json

MAX_SNIPPET_CHARS = 4000
MAX_CONTEXT_LINES = 40
SECRET_PATTERNS = (
    (
        re.compile(r"(?i)(authorization:\s*bearer\s+)([A-Za-z0-9._~+/=-]+)"),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY)[A-Z0-9_]*=)(\S+)"
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(r"\b(AKIA[0-9A-Z]{16})\b"),
        "[REDACTED_AWS_KEY]",
    ),
)
RECOVERY_PROMPTS = {
    "fix",
    "fix it",
    "suggest a fix",
    "recover",
    "why failed",
    "why did it fail",
    "why did that fail",
    "what failed",
}


def truncate_snippet(value: str | None, limit: int = MAX_SNIPPET_CHARS) -> str:
    """Bound captured command output before storing or sending to the model."""
    if not value:
        return ""
    redacted = redact_snippet(value)
    if len(redacted) <= limit:
        return redacted
    return redacted[-limit:]


def redact_snippet(value: str) -> str:
    """Redact common secret-bearing output patterns."""
    redacted = value
    for pattern, replacement in SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def is_recovery_prompt(prompt: str) -> bool:
    """Return true for short prompts that refer to the latest failure."""
    normalized = " ".join(prompt.strip().lower().split())
    if normalized in RECOVERY_PROMPTS:
        return True
    return normalized.startswith(("fix ", "why failed ", "why did it fail "))


def run_context_command(args: list[str], cwd: str) -> str:
    """Run a local context command with conservative timeout and output bounds."""
    try:
        proc = subprocess.run(
            args,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=0.5,
            check=False,
        )
    except Exception:
        return ""
    return truncate_snippet(proc.stdout, 2000).strip()


def cwd_context(cwd: str) -> dict[str, Any]:
    """Collect safe local context for a failed command."""
    context: dict[str, Any] = {"cwd": cwd}
    path = Path(cwd)
    entries = []
    try:
        for item in path.iterdir():
            entries.append(item.name + ("/" if item.is_dir() else ""))
            if len(entries) >= MAX_CONTEXT_LINES:
                break
    except OSError:
        entries = []
    if entries:
        context["entries"] = entries

    git_root = run_context_command(["git", "rev-parse", "--show-toplevel"], cwd)
    if git_root:
        context["git_root"] = git_root
        branch = run_context_command(["git", "branch", "--show-current"], cwd)
        if branch:
            context["git_branch"] = branch
        status = run_context_command(["git", "status", "--short"], cwd)
        if status:
            context["git_status"] = status.splitlines()[:MAX_CONTEXT_LINES]
    return context


def record_failure(
    command: str,
    status: int,
    cwd: str | None = None,
    stdout_snippet: str | None = None,
    stderr_snippet: str | None = None,
) -> None:
    """Persist the last nonzero shell command for the current session."""
    failure_cwd = cwd or os.getcwd()
    stdout_text = truncate_snippet(stdout_snippet)
    stderr_text = truncate_snippet(stderr_snippet)
    context = cwd_context(failure_cwd)
    security = create_trust_metadata(
        glyph="failure",
        mode="propose",
    )
    event = append_event(
        {
            "type": "failure_recorded",
            "command": command,
            "status": status,
            "failure_cwd": failure_cwd,
            "stdout_snippet": stdout_text,
            "stderr_snippet": stderr_text,
            "context": context,
            **security,
        }
    )
    write_json(
        "last-failure.json",
        {
            "command": command,
            "status": status,
            "cwd": failure_cwd,
            "stdout_snippet": stdout_text,
            "stderr_snippet": stderr_text,
            "context": context,
            "time": time.time(),
            "event_id": event["id"],
            **security,
        },
    )


def last_failure() -> dict[str, Any]:
    """Load the last recorded failure or exit with a terminal-friendly error."""
    failure = last_failure_or_none()
    if failure is None:
        print(f"{LOVE}✗ no failed command recorded{RESET}", file=sys.stderr)
        raise SystemExit(1)
    return failure


def last_failure_or_none() -> dict[str, Any] | None:
    """Load the last recorded failure without printing terminal output."""
    failure = read_json("last-failure.json")
    if not isinstance(failure, dict) or not failure.get("command"):
        return None
    return failure


def failure_context_prompt(failure: dict[str, Any]) -> str:
    """Build proposal context from a failed command without inventing output."""
    context = failure.get("context") if isinstance(failure.get("context"), dict) else {}
    prompt_lines = [
        f"Failed command: {failure['command']}",
        f"Exit status: {failure.get('status', 'unknown')}",
        f"Working directory: {failure.get('cwd', '')}",
    ]
    if failure.get("stderr_snippet"):
        prompt_lines.extend(["", "Recent stderr:", str(failure["stderr_snippet"])])
    else:
        prompt_lines.extend(["", "Recent stderr: <not captured>"])
    if failure.get("stdout_snippet"):
        prompt_lines.extend(["", "Recent stdout:", str(failure["stdout_snippet"])])
    else:
        prompt_lines.extend(["", "Recent stdout: <not captured>"])
    prompt_lines.extend(
        [
            "",
            "Failure-context guidance:",
            "- Do not invent missing stdout or stderr.",
            "- Use the command, exit status, cwd, git status, and cwd entries first.",
            "- If output is not captured, say so in the candidate note when relevant.",
        ]
    )
    if context:
        prompt_lines.extend(["", "Safe local context:"])
        for key in ("git_root", "git_branch"):
            if context.get(key):
                prompt_lines.append(f"{key}: {context[key]}")
        if context.get("git_status"):
            prompt_lines.append("git_status:")
            prompt_lines.extend(f"  {line}" for line in context["git_status"])
        if context.get("entries"):
            prompt_lines.append("cwd_entries:")
            prompt_lines.extend(f"  {entry}" for entry in context["entries"])
    return "\n".join(prompt_lines)
