"""Confirmed one-step Pi edit runner for triple-comma autonomy."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from typing import Any

from .ansi import MUTED, RESET
from .handoff import (
    bash_handoff_extension_path,
    prepare_bash_handoff,
    record_bash_handoffs,
)
from .question import renderer_command
from .security import create_trust_metadata
from .model import ensure_model_for_pi
from .state import append_event, append_jsonl, read_jsonl
from .tty import prompt_on_tty

LAST_ACT = "last-act.jsonl"
LEGACY_LAST_PLAN = "last-plan.jsonl"
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
    dry_run: bool = False,
    verbose: bool = False,
) -> int:
    """Create or resume a confirmed one-step Pi edit action."""
    act = active_act()
    if act is None:
        if not objective:
            print(
                "sigil act: no active Pi edit step; provide an objective",
                file=sys.stderr,
            )
            return 2
        if dry_run:
            print("sigil act: would create one confirmed Pi edit step")
            return 0
        act = create_act(objective=objective, stdin_text=stdin_text)
    elif objective and objective != str(act.get("objective", "")):
        if dry_run:
            print("sigil act: would replace active Pi edit step with a new objective")
            return 0
        act = create_act(objective=objective, stdin_text=stdin_text)
    elif dry_run:
        print("sigil act: would resume the pending Pi edit step")
        return 0

    print_act(act)
    step = next_pending_step(act)
    if step is None:
        act["status"] = "completed"
        record_act_update("act_completed", act)
        print("act complete")
        return 0

    print_next_step(step)
    decision = read_step_decision()
    if decision in {"", "n", "no", "quit", "q"}:
        return 0
    if decision == "skip":
        step["status"] = "skipped"
        record_step_decision(act, step, "skipped")
        print(f"skipped step {step['id']}")
        return 0
    if decision == "edit":
        edited = prompt_on_tty("objective> ")
        if edited is None or not edited.strip():
            return 0
        act["objective"] = edited.strip()
        step["edited"] = True
        confirm = read_step_decision(prompt="run edited Pi step? [y/N] ")
        if confirm not in {"y", "yes"}:
            return 0
    elif decision not in {"y", "yes"}:
        return 0

    decision_event = record_step_decision(act, step, "accepted")
    status = run_pi_agent_step(act, step, decision_event, verbose=verbose)
    step["status"] = "done" if status == 0 else "failed"
    step["exit_code"] = status
    record_step_executed(act, step, status)
    if status == 0:
        act["status"] = "completed"
        record_act_update("act_completed", act)
    return status


def create_act(*, objective: str, stdin_text: str = "") -> dict[str, Any]:
    """Create a one-step active Pi edit action."""
    act = {
        "act_id": str(uuid.uuid4()),
        "objective": objective,
        "stdin": stdin_text,
        "status": "active",
        "steps": [
            {
                "id": "1",
                "title": "Run one Pi edit step",
                "command": f"pi --tools {PI_AGENT_TOOLS}",
                "explanation": "One confirmed read/edit/write pass, then control returns to the shell.",
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
    """Read current act state, accepting the previous plan file as fallback."""
    events = read_jsonl(LAST_ACT)
    if events:
        return events
    return read_jsonl(LEGACY_LAST_PLAN)


def event_act(event: dict[str, Any]) -> dict[str, Any] | None:
    """Return the act payload, normalizing legacy plan snapshots."""
    act = event.get("act")
    if isinstance(act, dict):
        return act
    legacy_plan = event.get("plan")
    if isinstance(legacy_plan, dict):
        legacy_plan.setdefault("act_id", legacy_plan.get("plan_id"))
        return legacy_plan
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
    steps = [step for step in act.get("steps", []) if isinstance(step, dict)]
    print(f"sigil act ({act.get('status', 'active')}):")
    print(f"  objective: {act.get('objective')}")
    for step in steps:
        status = str(step.get("status") or "pending")
        print(f"  {step.get('id')}. [{status}] {step.get('title')}")


def print_next_step(step: dict[str, Any]) -> None:
    """Print the next proposed Pi step."""
    print("")
    print("next:")
    print(str(step.get("command") or ""))
    explanation = str(step.get("explanation") or "")
    if explanation:
        print(explanation)
    print("")


def read_step_decision(prompt: str = "proceed? [y/N/skip/edit/quit] ") -> str:
    """Read an act-step decision from the terminal."""
    answer = prompt_on_tty(prompt)
    return "" if answer is None else answer.strip().lower()


def run_pi_agent_step(
    act: dict[str, Any],
    step: dict[str, Any],
    decision_event: dict[str, Any],
    *,
    verbose: bool = False,
) -> int:
    """Run one non-interactive Pi edit step and stream tool events."""
    if not ensure_model_for_pi():
        return 1

    decision_event_id = str(decision_event.get("id") or "")
    security = create_trust_metadata(
        glyph=",,,",
        integrity="local_model",
        capability="exec_boxed",
        taint=["model"],
        inputs=[decision_event_id] if decision_event_id else [],
        input_records=[decision_event] if decision_event else [],
        fresh_human=True,
    )
    handoff_path = prepare_bash_handoff()
    extension_path = bash_handoff_extension_path()
    tools = (
        PI_AGENT_TOOLS if extension_path is not None else PI_AGENT_TOOLS_WITHOUT_BASH
    )
    tool_label = "read+bash-handoff+edit+write" if extension_path else "read+edit+write"
    print(
        f"{MUTED}❯ pi ,,,   · {tool_label} · one confirmed step{RESET}",
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
    filter_cmd = [sys.argv[0], "render-pi-stream"]
    if not verbose:
        filter_cmd.append("--compact")
    filter_env = {
        **os.environ,
        "SIGIL_CAPTURE_ANSWER": "1",
        "SIGIL_CAPTURE_TRACE": "1",
        "SIGIL_SECURITY_GLYPH": str(security["glyph"]),
        "SIGIL_SECURITY_INTEGRITY": str(security["integrity"]),
        "SIGIL_SECURITY_CAPABILITY": str(security["capability"]),
        "SIGIL_SECURITY_TAINT": ",".join(security["taint"]),
        "SIGIL_SECURITY_PROVISIONAL": "1" if security["provisional"] else "0",
        "SIGIL_SECURITY_INPUTS": ",".join(security["inputs"]),
        "SIGIL_QUESTION": str(act.get("objective") or ""),
        "SIGIL_PROMPT": pi_agent_prompt(act),
        "SIGIL_FOLLOW_UP": "0",
        "SIGIL_BASH_HANDOFF_PATH": str(handoff_path),
    }

    pi_proc = subprocess.Popen(pi_cmd, stdout=subprocess.PIPE)
    filter_proc = subprocess.Popen(
        filter_cmd,
        stdin=pi_proc.stdout,
        stdout=subprocess.PIPE,
        env=filter_env,
    )
    assert pi_proc.stdout is not None
    pi_proc.stdout.close()
    renderer_proc = subprocess.Popen(renderer_command(), stdin=filter_proc.stdout)
    assert filter_proc.stdout is not None
    filter_proc.stdout.close()

    renderer_code = renderer_proc.wait()
    filter_code = filter_proc.wait()
    pi_code = pi_proc.wait()
    handoffs = record_bash_handoffs(
        source_event=decision_event,
        source_security=security,
    )
    if handoffs:
        latest = str(handoffs[-1].get("command") or "")
        print(
            f"{MUTED}❯ bash handoff  {latest}{RESET}",
            file=sys.stderr,
        )
    print()
    if pi_code:
        return pi_code
    if filter_code:
        return filter_code
    return renderer_code


def pi_agent_prompt(act: dict[str, Any]) -> str:
    """Build the prompt for one Pi edit step."""
    sections = [
        "Run one bounded Sigil edit step.",
        f"Working directory: {os.getcwd()}",
        f"Objective: {act.get('objective')}",
    ]
    stdin_text = str(act.get("stdin") or "")
    if stdin_text:
        sections.append(f"Confirmed piped input:\n{stdin_text}")
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
        glyph=",,,",
        integrity="human"
        if event_type in {"act_created", "act_aborted"}
        else "local_model",
        capability="propose",
        taint=[] if event_type in {"act_created", "act_aborted"} else ["model"],
        inputs=inputs,
        fresh_human=True,
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
        glyph=",,,",
        integrity="human",
        capability="none",
        taint=[],
        inputs=inputs,
        fresh_human=True,
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
        glyph=",,,",
        integrity="local_model",
        capability="exec_boxed",
        taint=["model"],
        inputs=inputs,
        fresh_human=True,
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
