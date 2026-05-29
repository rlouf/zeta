"""Confirmed one-step Pi edit runner for triple-comma autonomy."""

from __future__ import annotations

import os
import sys
import uuid
from typing import Any

from .ansi import MUTED, RESET
from .staged_command import (
    prepare_staged_commands,
    record_staged_commands,
    staged_command_extension_path,
)
from .pi_stream import pi_trust_env, run_pi_pipeline
from .security import create_trust_metadata
from .model import ensure_model_for_pi
from .state import append_event, append_jsonl, read_jsonl
from .tty import prompt_on_tty

LAST_ACT = "last-act.jsonl"
MAX_EVENT_OUTPUT_CHARS = 4000
PI_AGENT_TOOLS = "read,grep,find,ls,bash,edit,write"
PI_AGENT_TOOLS_WITHOUT_BASH = "read,grep,find,ls,edit,write"

PI_AGENT_SYSTEM_PROMPT = (
    "You are Sigil's bounded shell-native edit route. Complete at most one "
    "coherent coding step for the user's objective. Use read/search tools "
    "before editing. Use edit/write only for minimal, relevant file changes. "
    "If local inspection or focused tests would help, call the bash tool; "
    "Sigil will block execution and hand the command to the user's terminal for "
    "review. Do not install dependencies, commit, push, reset, delete unrelated "
    "files, or perform network operations. If the request is ambiguous or "
    "unsafe, stop and say what you need. End with a concise summary of changed "
    "files and the next verification command."
)


def run_act_stepper(
    *,
    objective: str,
    stdin_text: str = "",
    confirm_step: bool,
    glyph: str,
    dry_run: bool = False,
) -> int:
    """Create or resume a one-step Pi edit action."""
    prepared = prepare_act(
        objective=objective,
        stdin_text=stdin_text,
        confirm_step=confirm_step,
        glyph=glyph,
        dry_run=dry_run,
    )
    if isinstance(prepared, int):
        return prepared
    act = prepared

    print_act(act)
    step = next_pending_step(act)
    if step is None:
        act["status"] = "completed"
        record_act_update("act_completed", act)
        print("act complete")
        return 0

    print_next_step(step)
    proceed, decision_label = confirm_act_step(act, step, confirm_step)
    if not proceed:
        return 0

    decision_event = record_step_decision(act, step, decision_label)
    status = run_pi_agent_step(act, step, decision_event, glyph=glyph)
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
    dry_run: bool,
) -> dict[str, Any] | int:
    """Create, replace, or resume an act; return the act or an exit code."""
    act = active_act()
    if act is None:
        if not objective:
            print(
                "sigil act: no active Pi edit step; provide an objective",
                file=sys.stderr,
            )
            return 2
        if dry_run:
            approval = "confirmed" if confirm_step else "auto-approved"
            print(f"sigil act: would create one {approval} Pi edit step")
            return 0
        return create_act(
            objective=objective,
            stdin_text=stdin_text,
            confirm_step=confirm_step,
            glyph=glyph,
        )
    if objective and objective != str(act.get("objective", "")):
        if dry_run:
            print("sigil act: would replace active Pi edit step with a new objective")
            return 0
        return create_act(
            objective=objective,
            stdin_text=stdin_text,
            confirm_step=confirm_step,
            glyph=glyph,
        )
    if dry_run:
        print("sigil act: would resume the pending Pi edit step")
        return 0
    act["glyph"] = glyph
    act["approval"] = "confirm" if confirm_step else "auto"
    return act


def confirm_act_step(
    act: dict[str, Any],
    step: dict[str, Any],
    confirm_step: bool,
) -> tuple[bool, str]:
    """Confirm one act step; return (proceed, decision_label) and record stops."""
    if not confirm_step:
        return True, "auto_accepted"
    decision = read_step_decision()
    if decision in {"", "n", "no", "quit", "q"}:
        return False, ""
    if decision == "skip":
        step["status"] = "skipped"
        record_step_decision(act, step, "skipped")
        print(f"skipped step {step['id']}")
        return False, ""
    if decision == "edit":
        edited = prompt_on_tty("objective> ")
        if edited is None or not edited.strip():
            return False, ""
        act["objective"] = edited.strip()
        step["edited"] = True
        confirm = read_step_decision(prompt="run edited Pi step? [y/N] ")
        if confirm not in {"y", "yes"}:
            return False, ""
    elif decision not in {"y", "yes"}:
        return False, ""
    return True, "accepted"


def create_act(
    *,
    objective: str,
    stdin_text: str = "",
    confirm_step: bool,
    glyph: str,
) -> dict[str, Any]:
    """Create a one-step active Pi edit action."""
    approval = "confirm" if confirm_step else "auto"
    explanation = (
        "One confirmed read/edit/write pass, then control returns to the shell."
        if confirm_step
        else "One auto-approved read/edit/write pass within policy, then control returns to the shell."
    )
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
                "title": "Run one Pi edit step",
                "command": f"pi --tools {PI_AGENT_TOOLS}",
                "explanation": explanation,
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
    """Return the pending Pi edit step, if any."""
    steps = act.get("steps")
    if not isinstance(steps, list):
        return None
    for step in steps:
        if isinstance(step, dict) and step.get("status") == "pending":
            return step
    return None


def print_act(act: dict[str, Any]) -> None:
    """Print a compact act overview."""
    print(f"objective: {act.get('objective')}")


def print_next_step(step: dict[str, Any]) -> None:
    """Print the tools available for the next Pi step."""
    tools = tools_from_step(step)
    print(f"tools: {tools}")


def read_step_decision(prompt: str = "run? [y/N] ") -> str:
    """Read an act-step decision from the terminal."""
    answer = prompt_on_tty(prompt)
    return "" if answer is None else answer.strip().lower()


def tools_from_step(step: dict[str, Any]) -> str:
    """Return the tool list from a step command, if present."""
    command = str(step.get("command") or "")
    marker = "--tools "
    if marker not in command:
        return command
    return command.split(marker, 1)[1].split(maxsplit=1)[0]


def run_pi_agent_step(
    act: dict[str, Any],
    step: dict[str, Any],
    decision_event: dict[str, Any],
    *,
    glyph: str | None = None,
) -> int:
    """Run one non-interactive Pi edit step and stream tool events."""
    if not ensure_model_for_pi():
        return 1

    decision_event_id = str(decision_event.get("id") or "")
    route_glyph = glyph or str(act.get("glyph") or ",,,")
    security = create_trust_metadata(
        glyph=route_glyph,
        mode="execute-write",
        inputs=[decision_event_id] if decision_event_id else [],
        input_records=[decision_event] if decision_event else [],
    )
    staged_command_path = prepare_staged_commands()
    extension_path = staged_command_extension_path()
    tools = (
        PI_AGENT_TOOLS if extension_path is not None else PI_AGENT_TOOLS_WITHOUT_BASH
    )
    tool_label = (
        "read+staged-command+edit+write" if extension_path else "read+edit+write"
    )
    approval = str(act.get("approval") or "confirm")
    step_label = (
        "one auto-approved step" if approval == "auto" else "one confirmed step"
    )
    print(
        f"{MUTED}❯ pi {route_glyph:<5} · {tool_label} · {step_label}{RESET}",
        file=sys.stderr,
    )

    pi_cmd = [
        "pi",
        "-p",
        "--mode",
        "json",
        "--no-session",
        "--tools",
        tools,
    ]
    if extension_path is not None:
        pi_cmd.extend(
            [
                "--extension",
                str(extension_path),
            ]
        )
    pi_cmd.extend(
        [
            "--append-system-prompt",
            PI_AGENT_SYSTEM_PROMPT,
            pi_agent_prompt(act),
        ]
    )
    filter_env = pi_trust_env(
        security,
        question=str(act.get("objective") or ""),
        prompt=pi_agent_prompt(act),
        follow_up="0",
        extra={"SIGIL_STAGED_COMMAND_PATH": str(staged_command_path)},
    )
    exit_code = run_pi_pipeline(pi_cmd, env=filter_env)
    staged = record_staged_commands(
        source_event=decision_event,
        source_security=security,
    )
    if staged:
        latest = str(staged[-1].get("command") or "")
        print(
            f"{MUTED}❯ staged command  {latest}{RESET}",
            file=sys.stderr,
        )
    print()
    return exit_code


def pi_agent_prompt(act: dict[str, Any]) -> str:
    """Build the prompt for one Pi edit step."""
    is_goal = act.get("kind") == "goal"
    sections = [
        "Run one bounded Sigil goal step."
        if is_goal
        else "Run one bounded Sigil edit step.",
        f"Working directory: {os.getcwd()}",
        f"Objective: {act.get('objective')}",
    ]
    stdin_text = str(act.get("stdin") or "")
    if stdin_text:
        sections.append(f"Confirmed piped input:\n{stdin_text}")
    if is_goal:
        sections.append(
            "After the step, stop. Do not commit. End with exactly one "
            "SIGIL_STATUS line set to continue, complete, or blocked, followed "
            "by one SIGIL_NEXT line with the next checkpoint or blocker."
        )
    else:
        sections.append(
            "After the step, stop. Do not commit. Leave review to the user in Git/lazygit."
        )
    return "\n\n".join(sections)


def record_act_update(event_type: str, act: dict[str, Any]) -> dict[str, Any]:
    """Record an act snapshot in session and global state."""
    inputs = []
    last_event_id = act.get("last_event_id")
    if event_type != "act_created" and isinstance(last_event_id, str):
        inputs.append(last_event_id)
    security = create_trust_metadata(
        glyph=str(act.get("glyph") or ",,,"),
        mode="propose",
        inputs=inputs,
    )
    payload = {
        "type": event_type,
        "act_id": act.get("act_id"),
        "objective": act.get("objective"),
        "act": act,
        **security,
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
    """Record a user decision for one Pi edit step."""
    step["decision"] = decision
    inputs = []
    act_event_id = act.get("event_id")
    if isinstance(act_event_id, str):
        inputs.append(act_event_id)
    security = create_trust_metadata(
        glyph=str(act.get("glyph") or ",,,"),
        mode="propose",
        inputs=inputs,
    )
    payload = {
        "type": "act_step_decision",
        "act_id": act.get("act_id"),
        "step_id": step.get("id"),
        "decision": decision,
        "command": step.get("command"),
        "act": act,
        **security,
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
    """Record completion of one Pi edit step."""
    inputs = []
    decision_event_id = step.get("decision_event_id")
    if isinstance(decision_event_id, str):
        inputs.append(decision_event_id)
    security = create_trust_metadata(
        glyph=str(act.get("glyph") or ",,,"),
        mode="execute-write",
        inputs=inputs,
    )
    payload = {
        "type": "act_step_executed",
        "act_id": act.get("act_id"),
        "step_id": step.get("id"),
        "command": step.get("command"),
        "status": status,
        "stdout_snippet": "",
        "stderr_snippet": "",
        "act": act,
        **security,
    }
    global_event = append_event(payload)
    step["execution_event_id"] = global_event["id"]
    act["last_event_id"] = global_event["id"]
    payload["execution_event_id"] = global_event["id"]
    payload["act"] = act
    session_event = append_jsonl(LAST_ACT, payload)
    return session_event
