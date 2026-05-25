"""Failure recovery for the caret glyph.

`^` turns the last failed shell command into repair candidates. It deliberately
stops at proposal: selected fixes are placed on the prompt for review, never
executed automatically.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .ansi import LOVE, MUTED, RESET
from .commands import COMMAND_SCHEMA, select
from .qwen import chat_json, ensure_server
from .security import inherit_security, create_trust_metadata, normalize_trust_record
from .state import append_event, read_json, write_json

FIX_SYSTEM = (
    "You fix failed macOS zsh commands with the default BSD userland. "
    "Return 2-4 corrected candidate commands, best first, each with a terse "
    "one-line note. Do not invent hidden context. Preserve user intent. "
    "Commands must be directly runnable, but they will be reviewed by a human "
    "before execution."
)
MAX_SNIPPET_CHARS = 4000
MAX_CONTEXT_LINES = 40


def truncate_snippet(value: str | None, limit: int = MAX_SNIPPET_CHARS) -> str:
    """Bound captured command output before storing or sending to the model."""
    if not value:
        return ""
    if len(value) <= limit:
        return value
    return value[-limit:]


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
        glyph="^",
        integrity="human",
        capability="propose",
        taint=[],
        fresh_human=True,
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
    failure = read_json("last-failure.json")
    if not isinstance(failure, dict) or not failure.get("command"):
        print(f"{LOVE}✗ no failed command recorded{RESET}", file=sys.stderr)
        raise SystemExit(1)
    return failure


def fix_prompt(failure: dict[str, Any]) -> str:
    """Build the model prompt for repair without inventing missing output."""
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
            "Repair guidance:",
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


def generate_fixes() -> tuple[str, list[dict[str, str]], dict[str, Any]]:
    """Generate repair candidates for the current session's last failure."""
    failure = normalize_trust_record(last_failure())
    if not ensure_server():
        raise SystemExit(1)

    print(f"{MUTED}❯ sigil ^  · repair · model-authored{RESET}", file=sys.stderr)
    print(f"{MUTED}⟳ thinking…{RESET}", end="", file=sys.stderr, flush=True)
    user = fix_prompt(failure)
    try:
        data = chat_json(FIX_SYSTEM, user, COMMAND_SCHEMA)
    except RuntimeError as exc:
        print("\r\033[K", end="", file=sys.stderr)
        print(f"{LOVE}✗ qwen request failed{RESET}", file=sys.stderr)
        print(f"  {exc}", file=sys.stderr)
        print("  Check that the local model server is still running.", file=sys.stderr)
        raise SystemExit(1) from exc
    except Exception:
        print("\r\033[K", end="", file=sys.stderr)
        print(f"{LOVE}✗ could not generate fix candidates{RESET}", file=sys.stderr)
        raise SystemExit(1)
    print("\r\033[K", end="", file=sys.stderr)

    candidates = [
        {"command": str(item.get("command", "")), "note": str(item.get("note", ""))}
        for item in data.get("commands", [])
        if item.get("command")
    ]
    if not candidates:
        print(f"{LOVE}✗ no fix candidates{RESET}", file=sys.stderr)
        raise SystemExit(1)

    security = create_trust_metadata(
        glyph="^",
        integrity="local_model",
        capability="propose",
        taint=["model"],
        inputs=[str(failure.get("event_id") or failure.get("id") or "")],
        input_records=[failure],
        fresh_human=True,
    )
    event = append_event(
        {
            "type": "fix_generated",
            "failure": failure,
            "commands": candidates,
            **security,
        }
    )
    write_json(
        "last-fix.json",
        {
            "prompt": str(failure["command"]),
            "failure": failure,
            "commands": candidates,
            "event_id": event["id"],
            **security,
        },
    )
    return str(failure["command"]), candidates, security


def previous_fix() -> tuple[str, list[dict[str, str]], dict[str, Any]]:
    """Load the previous repair candidates for `^^`."""
    data = read_json("last-fix.json")
    if not isinstance(data, dict) or not data.get("commands"):
        print(f"{LOVE}✗ no previous fix suggestions{RESET}", file=sys.stderr)
        raise SystemExit(1)
    security = inherit_security(
        glyph="^^", input_records=[normalize_trust_record(data)], capability="propose"
    )
    return str(data.get("prompt", "")), list(data["commands"]), security


def select_fix() -> str | None:
    """Generate fixes and return the user's selected repair command."""
    prompt, candidates, security = generate_fixes()
    command = select(prompt, candidates, security)
    print_selected_fix_note(command, candidates)
    return command


def select_previous_fix() -> str | None:
    """Reopen previous repair candidates and return the selected command."""
    prompt, candidates, security = previous_fix()
    continued = append_event({"type": "fix_continued", "prompt": prompt, **security})
    security = {**security, "inputs": [continued["id"]]}
    print(f"{MUTED}❯ sigil ^^ · inherited repair{RESET}", file=sys.stderr)
    command = select(prompt, candidates, security)
    print_selected_fix_note(command, candidates)
    return command


def print_selected_fix_note(
    command: str | None, candidates: list[dict[str, str]]
) -> None:
    """Show the selected rationale on stderr without changing stdout payload."""
    if not command:
        return
    for candidate in candidates:
        if candidate.get("command") == command and candidate.get("note"):
            print(f"{MUTED}why: {candidate['note']}{RESET}", file=sys.stderr)
            return
