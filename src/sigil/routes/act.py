"""Confirmed one-step Zeta edit runner for triple-comma autonomy."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
import uuid
from typing import Any

from ..state import append_event, append_jsonl, read_jsonl
from ..display import (
    render_act_objective_line,
    render_act_tools_line,
    render_zeta_status,
)
from ..tty import clear_lines_on_tty, open_tty_fd, prompt_on_tty
from .zeta_step import run_agent_step

LAST_ACT = "last-act.jsonl"
MAX_EVENT_OUTPUT_CHARS = 4000
ZETA_AGENT_TOOLS = "read,grep,bash,edit,write"

ZETA_AGENT_SYSTEM_PROMPT = (
    "You are Sigil's bounded confirmed edit route. Complete the user's "
    "objective by running tool calls until no more tool calls are needed. "
    "Read/search tools run immediately for local context. Bash commands "
    "execute automatically, and edit/write tools apply directly, so only use "
    "them when they are minimal, relevant, and safe for this local workspace. "
    "Do not install dependencies, commit, push, reset, delete unrelated files, "
    "or perform network operations. If the request is ambiguous or unsafe, "
    "stop and say what you need. End with a concise summary of changed files "
    "and verification performed or the next verification command."
)

ZETA_AGENT_AUTO_SYSTEM_PROMPT = (
    "You are Sigil's bounded auto edit route. Complete the user's objective "
    "by running tool calls until no more tool calls are needed. Read/search "
    "tools run immediately for local context. Bash commands execute "
    "automatically, and edit/write tools apply directly, so only use them when "
    "they are minimal, relevant, and safe for this local workspace. Do not "
    "install dependencies, commit, push, reset, delete unrelated files, or "
    "perform network operations. If the request is ambiguous or unsafe, stop "
    "and say what you need. End with a concise summary of changed files and "
    "verification performed or the next verification command."
)


def run_act_stepper(
    *,
    objective: str,
    stdin_text: str = "",
    confirm_step: bool,
    glyph: str,
) -> int:
    """Create or resume a one-step Zeta edit action."""
    prepared = prepare_act(
        objective=objective,
        stdin_text=stdin_text,
        confirm_step=confirm_step,
        glyph=glyph,
    )
    if isinstance(prepared, int):
        return prepared
    act = prepared

    step = next_pending_step(act)
    if step is None:
        act["status"] = "completed"
        record_act_update("act_completed", act)
        print("act complete")
        return 0

    print_next_step(step)
    proceed, decision_label = confirm_act_step(
        act, step, confirm_step, preamble_lines=2
    )
    if not proceed:
        return 0

    record_step_decision(act, step, decision_label)
    status = run_zeta_agent_step(act, glyph=glyph, tools=tools_from_step(step))
    step["status"] = "done" if status == 0 else "failed"
    step["exit_code"] = status
    record_step_executed(act, step, status)
    if status == 0:
        act["status"] = "completed"
        record_act_update("act_completed", act)
    return status


def prepare_act(
    *,
    objective: str,
    stdin_text: str,
    confirm_step: bool,
    glyph: str,
) -> dict[str, Any] | int:
    """Create, replace, or resume an act; return the act or an exit code."""
    act = active_act()
    if act is None:
        if not objective:
            print(
                "sigil act: no active Zeta edit step; provide an objective",
                file=sys.stderr,
            )
            return 2
        return create_act(
            objective=objective,
            stdin_text=stdin_text,
            confirm_step=confirm_step,
            glyph=glyph,
        )
    if objective and next_pending_step(act) is None:
        return create_act(
            objective=objective,
            stdin_text=stdin_text,
            confirm_step=confirm_step,
            glyph=glyph,
        )
    if objective and objective != str(act.get("objective", "")):
        return create_act(
            objective=objective,
            stdin_text=stdin_text,
            confirm_step=confirm_step,
            glyph=glyph,
        )
    act["glyph"] = glyph
    act["approval"] = "confirm" if confirm_step else "auto"
    return act


def confirm_act_step(
    act: dict[str, Any],
    step: dict[str, Any],
    confirm_step: bool,
    preamble_lines: int = 0,
) -> tuple[bool, str]:
    """Confirm one act step; return (proceed, decision_label) and record stops."""
    if not confirm_step:
        return True, "auto_accepted"
    shown = preamble_lines
    decision = read_step_decision()
    shown += 1
    if decision in {"", "n", "no", "quit", "q"}:
        clear_lines_on_tty(shown)
        return False, ""
    if decision == "skip":
        step["status"] = "skipped"
        record_step_decision(act, step, "skipped")
        clear_lines_on_tty(shown)
        print(f"skipped step {step['id']}")
        return False, ""
    if decision in {"e", "edit"}:
        edited_tools = edit_step_tools(step)
        if edited_tools is None:
            return False, ""
        set_step_tools(step, edited_tools)
        step["edited_tools"] = True
        confirm = read_step_decision(prompt="run edited Zeta step? [y/N] ")
        shown += 1
        if confirm not in {"y", "yes"}:
            clear_lines_on_tty(shown)
            return False, ""
    elif decision not in {"y", "yes"}:
        clear_lines_on_tty(shown)
        return False, ""
    clear_lines_on_tty(shown)
    return True, "accepted"


def create_act(
    *,
    objective: str,
    stdin_text: str = "",
    confirm_step: bool,
    glyph: str,
) -> dict[str, Any]:
    """Create a one-step active Zeta edit action."""
    approval = "confirm" if confirm_step else "auto"
    act = {
        "act_id": str(uuid.uuid4()),
        "glyph": glyph,
        "approval": approval,
        "objective": objective,
        "stdin": stdin_text,
        "status": "active",
        "steps": [
            {
                "id": "1",
                "title": "Run one Zeta edit step",
                "command": f"zeta --tools {ZETA_AGENT_TOOLS}",
                "status": "pending",
            }
        ],
    }
    record_act_update("act_created", act)
    return act


def active_act() -> dict[str, Any] | None:
    """Return the latest active act snapshot for this session."""
    for event in reversed(read_act_events()):
        act = event_act(event)
        if isinstance(act, dict):
            status = act.get("status")
            if status == "active":
                return act
            if status in {"aborted", "completed"}:
                return None
    return None


def last_act() -> dict[str, Any] | None:
    """Return the latest act snapshot for this session."""
    for event in reversed(read_act_events()):
        act = event_act(event)
        if isinstance(act, dict):
            return act
    return None


def abort_active_act() -> dict[str, Any] | None:
    """Mark the active act aborted."""
    act = active_act()
    if act is None:
        return None
    act["status"] = "aborted"
    record_act_update("act_aborted", act)
    return act


def read_act_events() -> list[dict[str, Any]]:
    """Read current act state events."""
    return read_jsonl(LAST_ACT)


def event_act(event: dict[str, Any]) -> dict[str, Any] | None:
    """Return the act payload from an event, if present."""
    act = event.get("act")
    if isinstance(act, dict):
        return act
    return None


def next_pending_step(act: dict[str, Any]) -> dict[str, Any] | None:
    """Return the pending Zeta edit step, if any."""
    steps = act.get("steps")
    if not isinstance(steps, list):
        return None
    for step in steps:
        if isinstance(step, dict) and step.get("status") == "pending":
            return step
    return None


def print_act(act: dict[str, Any]) -> None:
    """Print a compact act overview."""
    print(render_act_objective_line(act))


def print_next_step(step: dict[str, Any]) -> None:
    """Print the tools available for the next Zeta step."""
    print(render_act_tools_line(tools_from_step(step)))


def read_step_decision(prompt: str = "run? [y/N/e] ") -> str:
    """Read an act-step decision from the terminal."""
    answer = prompt_on_tty(prompt)
    return "" if answer is None else answer.strip().lower()


def edit_step_tools(step: dict[str, Any]) -> list[str] | None:
    """Open $EDITOR on the step tool list and return the edited tools."""
    tools = tool_names_from_step(step)
    edited = edit_tools(tools)
    if edited is None:
        return None
    normalized = normalize_tool_names(edited)
    known_tools = set(tools)
    unknown_tools = [tool for tool in normalized if tool not in known_tools]
    if unknown_tools:
        print(
            f"sigil: unknown tool(s): {', '.join(unknown_tools)}",
            file=sys.stderr,
        )
        return None
    return normalized


def edit_tools(tools: list[str]) -> list[str] | None:
    """Let the user edit tool names, one per line, in their editor."""
    editor = editor_command()
    initial_text = "\n".join(tools)
    if initial_text:
        initial_text += "\n"
    with tempfile.NamedTemporaryFile(
        "w+",
        encoding="utf-8",
        prefix="sigil-tools-",
        suffix=".txt",
    ) as file:
        file.write(initial_text)
        file.flush()
        try:
            status = run_editor(editor, file.name)
        except OSError as exc:
            print(f"sigil: could not open editor {editor!r}: {exc}", file=sys.stderr)
            return None
        if status != 0:
            print(f"sigil: editor exited with status {status}", file=sys.stderr)
            return None
        file.seek(0)
        return parse_tool_lines(file.read())


def editor_command() -> list[str]:
    """Return the configured editor command."""
    raw = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    try:
        return shlex.split(raw) or ["vi"]
    except ValueError:
        return [raw]


def run_editor(editor: list[str], path: str) -> int:
    """Run an editor attached to the controlling terminal when available."""
    command = [*editor, path]
    fd = open_tty_fd()
    if fd is None:
        return subprocess.run(command, check=False).returncode
    try:
        return subprocess.run(
            command,
            stdin=fd,
            stdout=fd,
            stderr=fd,
            check=False,
        ).returncode
    finally:
        os.close(fd)


def parse_tool_lines(text: str) -> list[str]:
    """Parse edited tool lines, allowing blank lines and comments."""
    tools = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        tools.append(line)
    return tools


def normalize_tool_names(tools: list[str]) -> list[str]:
    """Deduplicate edited tool names while preserving order."""
    normalized = []
    seen = set()
    for tool in tools:
        name = tool.strip()
        if not name or name in seen:
            continue
        normalized.append(name)
        seen.add(name)
    return normalized


def tool_names_from_step(step: dict[str, Any]) -> list[str]:
    """Return the ordered tool names from a pending step."""
    return normalize_tool_names(tools_from_step(step).split(","))


def set_step_tools(step: dict[str, Any], tools: list[str]) -> None:
    """Persist an edited tool list on a pending step."""
    serialized = ",".join(tools)
    step["tools"] = tools
    step["command"] = f"zeta --tools {serialized}"


def tools_from_step(step: dict[str, Any]) -> str:
    """Return the tool list from a step command, if present."""
    raw_tools = step.get("tools")
    if isinstance(raw_tools, list):
        return ",".join(str(tool).strip() for tool in raw_tools if str(tool).strip())
    command = str(step.get("command") or "")
    marker = "--tools "
    if marker not in command:
        return command
    return command.split(marker, 1)[1].split(maxsplit=1)[0]


def run_zeta_agent_step(
    act: dict[str, Any],
    *,
    glyph: str | None = None,
    tools: str | list[str] | None = None,
) -> int:
    """Run one non-interactive Zeta edit step."""
    route_glyph = glyph or str(act.get("glyph") or ",,,")
    enabled_tools = effective_zeta_tools(tools)
    step_label = "auto tool loop" if route_glyph == ",,," else "confirmed tool loop"
    render_zeta_status(
        route_glyph,
        enabled_tools,
        step_label,
        output=sys.stderr,
        color_enabled=True,
    )
    exit_code = run_agent_step(
        str(act.get("objective") or ""),
        glyph=route_glyph,
        system=system_prompt_for_glyph(route_glyph),
        stdin_text=str(act.get("stdin") or ""),
        allowed_tools=enabled_tools,
    )
    print()
    return exit_code


def system_prompt_for_glyph(glyph: str) -> str:
    """Return the route-specific Zeta system prompt."""
    if glyph == ",,,":
        return ZETA_AGENT_AUTO_SYSTEM_PROMPT
    return ZETA_AGENT_SYSTEM_PROMPT


def effective_zeta_tools(
    tools: str | list[str] | None,
) -> list[str]:
    """Return the tool list to pass to Zeta for a step."""
    if tools is None:
        tools = ZETA_AGENT_TOOLS
    if isinstance(tools, str):
        names = tools.split(",")
    else:
        names = tools
    enabled = normalize_tool_names([str(name) for name in names])
    return enabled


def record_act_update(event_type: str, act: dict[str, Any]) -> dict[str, Any]:
    """Record an act snapshot in session and global state."""
    payload = {
        "type": event_type,
        "act_id": act.get("act_id"),
        "objective": act.get("objective"),
        "act": act,
        "glyph": str(act.get("glyph") or ",,,"),
    }
    global_event = append_event(payload)
    if event_type == "act_created":
        act["event_id"] = global_event["id"]
    act["last_event_id"] = global_event["id"]
    payload["act"] = act
    session_event = append_jsonl(LAST_ACT, payload)
    return session_event


def record_step_decision(
    act: dict[str, Any],
    step: dict[str, Any],
    decision: str,
) -> dict[str, Any]:
    """Record a user decision for one Zeta edit step."""
    step["decision"] = decision
    payload = {
        "type": "act_step_decision",
        "act_id": act.get("act_id"),
        "step_id": step.get("id"),
        "decision": decision,
        "command": step.get("command"),
        "act": act,
        "glyph": str(act.get("glyph") or ",,,"),
    }
    global_event = append_event(payload)
    step["decision_event_id"] = global_event["id"]
    act["last_event_id"] = global_event["id"]
    payload["decision_event_id"] = global_event["id"]
    payload["act"] = act
    session_event = append_jsonl(LAST_ACT, payload)
    return session_event


def record_step_executed(
    act: dict[str, Any],
    step: dict[str, Any],
    status: int,
) -> dict[str, Any]:
    """Record completion of one Zeta edit step."""
    payload = {
        "type": "act_step_executed",
        "act_id": act.get("act_id"),
        "step_id": step.get("id"),
        "command": step.get("command"),
        "status": status,
        "act": act,
        "glyph": str(act.get("glyph") or ",,,"),
    }
    global_event = append_event(payload)
    step["execution_event_id"] = global_event["id"]
    act["last_event_id"] = global_event["id"]
    payload["execution_event_id"] = global_event["id"]
    payload["act"] = act
    session_event = append_jsonl(LAST_ACT, payload)
    return session_event
