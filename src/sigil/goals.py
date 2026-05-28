"""Durable goal loop runner for @ and @@ routes."""

from __future__ import annotations

import os
import sys
import uuid
from typing import Any, Literal

from .acts import PI_AGENT_TOOLS, print_next_step, run_pi_agent_step
from .security import create_trust_metadata
from .state import append_event, append_jsonl, read_jsonl
from .tty import prompt_on_tty

LAST_GOAL = "last-goal.jsonl"
DEFAULT_MAX_GOAL_STEPS = 5
StepStatus = Literal["continue", "complete", "blocked"]


def run_goal_loop(
    *,
    objective: str,
    stdin_text: str = "",
    confirm_steps: bool,
    glyph: str,
    dry_run: bool = False,
) -> int:
    """Create or resume a bounded goal loop."""
    prepared = prepare_goal(
        objective=objective,
        stdin_text=stdin_text,
        confirm_steps=confirm_steps,
        glyph=glyph,
        dry_run=dry_run,
    )
    if isinstance(prepared, int):
        return prepared
    goal = prepared

    max_steps = goal_step_budget(goal)
    while int(goal.get("steps_run", 0) or 0) < max_steps:
        if str(goal.get("status")) != "active":
            record_goal_update("goal_stopped", goal)
            return 0

        print_goal(goal)
        step = create_goal_step(goal)
        print_next_step(step)
        proceed, decision_label = approve_goal_step(goal, confirm_steps)
        if not proceed:
            return 0
        outcome = execute_goal_step(goal, step, decision_label, glyph=glyph)
        if outcome is not None:
            return outcome

    goal["status"] = "budget_hit"
    record_goal_update("goal_budget_hit", goal)
    print("goal budget hit")
    return 0


def prepare_goal(
    *,
    objective: str,
    stdin_text: str,
    confirm_steps: bool,
    glyph: str,
    dry_run: bool,
) -> dict[str, Any] | int:
    """Create, replace, or resume a goal; return the goal or an exit code."""
    goal = active_goal()
    if goal is None:
        if not objective:
            print("sigil goal: no active goal; provide an objective", file=sys.stderr)
            return 2
        if dry_run:
            approval = "confirmed" if confirm_steps else "auto-approved"
            print(f"sigil goal: would create {approval} goal loop")
            return 0
        return create_goal_state(
            objective=objective,
            stdin_text=stdin_text,
            confirm_steps=confirm_steps,
            glyph=glyph,
        )
    if objective and objective != str(goal.get("objective", "")):
        if dry_run:
            print("sigil goal: would replace active goal with a new objective")
            return 0
        return create_goal_state(
            objective=objective,
            stdin_text=stdin_text,
            confirm_steps=confirm_steps,
            glyph=glyph,
        )
    if dry_run:
        print("sigil goal: would resume active goal")
        return 0
    goal["glyph"] = glyph
    goal["approval"] = "confirm" if confirm_steps else "auto"
    return goal


def approve_goal_step(goal: dict[str, Any], confirm_steps: bool) -> tuple[bool, str]:
    """Confirm one step; return (proceed, decision_label) and record stops."""
    if not confirm_steps:
        return True, "auto_accepted"
    decision = read_goal_decision()
    if decision == "abort":
        goal["status"] = "aborted"
        record_goal_update("goal_aborted", goal)
        print("goal aborted")
        return False, ""
    if decision not in {"y", "yes"}:
        record_goal_update("goal_checkpoint", goal)
        return False, ""
    return True, "accepted"


def execute_goal_step(
    goal: dict[str, Any],
    step: dict[str, Any],
    decision_label: str,
    *,
    glyph: str,
) -> int | None:
    """Run one approved step; return an exit code to stop, or None to continue."""
    decision_event = record_goal_step_decision(goal, step, decision_label)
    status = run_pi_agent_step(
        goal_as_act(goal),
        step,
        decision_event,
        glyph=glyph,
    )
    step["exit_code"] = status
    goal["steps_run"] = int(goal.get("steps_run", 0) or 0) + 1
    if status != 0:
        step["status"] = "failed"
        goal["status"] = "blocked"
        goal["last_status"] = "blocked"
        goal["last_next"] = f"Pi step exited with status {status}."
        record_goal_step_executed(goal, step, status)
        record_goal_update("goal_blocked", goal)
        return status

    step_status, next_note = latest_step_status()
    step["status"] = "done"
    step["reported_status"] = step_status
    goal["last_status"] = step_status
    goal["last_next"] = next_note
    record_goal_step_executed(goal, step, status)
    if step_status == "complete":
        goal["status"] = "completed"
        record_goal_update("goal_completed", goal)
        print("goal complete")
        return 0
    if step_status == "blocked":
        goal["status"] = "blocked"
        record_goal_update("goal_blocked", goal)
        print("goal blocked")
        return 0
    return None


def create_goal_state(
    *,
    objective: str,
    stdin_text: str,
    confirm_steps: bool,
    glyph: str,
) -> dict[str, Any]:
    """Create an active goal state record."""
    goal = {
        "goal_id": str(uuid.uuid4()),
        "glyph": glyph,
        "objective": objective,
        "stdin": stdin_text,
        "status": "active",
        "approval": "confirm" if confirm_steps else "auto",
        "steps": [],
        "steps_run": 0,
        "budgets": {"max_steps": DEFAULT_MAX_GOAL_STEPS},
    }
    record_goal_update("goal_created", goal)
    return goal


def active_goal() -> dict[str, Any] | None:
    """Return the latest active goal snapshot for this session."""
    for event in reversed(read_jsonl(LAST_GOAL)):
        goal = event.get("goal")
        if not isinstance(goal, dict):
            continue
        status = goal.get("status")
        if status == "active":
            return goal
        if status in {"completed", "blocked", "budget_hit", "aborted"}:
            return None
    return None


def goal_step_budget(goal: dict[str, Any]) -> int:
    """Return the maximum steps allowed for this goal invocation."""
    raw_env = os.environ.get("SIGIL_GOAL_MAX_STEPS")
    if raw_env:
        try:
            return max(1, int(raw_env))
        except ValueError:
            pass
    budgets = goal.get("budgets")
    if isinstance(budgets, dict):
        raw_budget = budgets.get("max_steps")
        if isinstance(raw_budget, int) and raw_budget > 0:
            return raw_budget
    return DEFAULT_MAX_GOAL_STEPS


def create_goal_step(goal: dict[str, Any]) -> dict[str, Any]:
    """Append and return the next pending goal step."""
    steps = goal.setdefault("steps", [])
    if not isinstance(steps, list):
        steps = []
        goal["steps"] = steps
    step = {
        "id": str(len(steps) + 1),
        "title": "Run one Pi goal step",
        "command": f"pi --tools {PI_AGENT_TOOLS}",
        "explanation": "One bounded goal step, then report continue, complete, or blocked.",
        "status": "pending",
    }
    steps.append(step)
    record_goal_update("goal_step_created", goal)
    return step


def goal_as_act(goal: dict[str, Any]) -> dict[str, Any]:
    """Return an act-shaped view so the shared Pi step runner can execute it."""
    return {
        "kind": "goal",
        "goal_id": goal.get("goal_id"),
        "act_id": goal.get("goal_id"),
        "glyph": goal.get("glyph"),
        "approval": goal.get("approval"),
        "objective": goal.get("objective"),
        "stdin": goal.get("stdin"),
    }


def print_goal(goal: dict[str, Any]) -> None:
    """Print a compact goal overview."""
    print(f"sigil goal ({goal.get('status', 'active')}):")
    print(f"  objective: {goal.get('objective')}")
    print(f"  approval: {goal.get('approval')}")
    last_next = str(goal.get("last_next") or "")
    if last_next:
        print(f"  next: {last_next}")


def read_goal_decision(prompt: str = "run next goal step? [y/N/abort] ") -> str:
    """Read a goal checkpoint decision from the terminal."""
    answer = prompt_on_tty(prompt)
    return "" if answer is None else answer.strip().lower()


def latest_step_status() -> tuple[StepStatus, str]:
    """Parse the latest captured Pi answer for goal loop status."""
    for turn in reversed(read_jsonl("last-question.jsonl")):
        if turn.get("role") != "assistant":
            continue
        content = str(turn.get("content") or "")
        status = parse_step_status(content)
        if status is not None:
            return status
        return "blocked", "Goal step did not report SIGIL_STATUS."
    return "blocked", "Goal step did not produce an answer."


def parse_step_status(content: str) -> tuple[StepStatus, str] | None:
    """Parse SIGIL_STATUS and SIGIL_NEXT lines from a goal step answer."""
    status: StepStatus | None = None
    next_note = ""
    for raw_line in content.splitlines():
        line = raw_line.strip()
        key, sep, value = line.partition(":")
        if not sep:
            continue
        normalized_key = key.strip().upper()
        normalized_value = value.strip()
        if normalized_key == "SIGIL_STATUS":
            if normalized_value == "continue":
                status = "continue"
            elif normalized_value == "complete":
                status = "complete"
            elif normalized_value == "blocked":
                status = "blocked"
        elif normalized_key == "SIGIL_NEXT":
            next_note = normalized_value
    if status is None:
        return None
    return status, next_note


def record_goal_update(event_type: str, goal: dict[str, Any]) -> dict[str, Any]:
    """Record a goal snapshot in session and global state."""
    security = create_trust_metadata(
        glyph=str(goal.get("glyph") or "@"),
        mode="execute-write",
    )
    payload = {
        "type": event_type,
        "goal_id": goal.get("goal_id"),
        "objective": goal.get("objective"),
        "goal": goal,
        **security,
    }
    global_event = append_event(payload)
    if event_type == "goal_created":
        goal["event_id"] = global_event["id"]
    goal["last_event_id"] = global_event["id"]
    payload["goal"] = goal
    return append_jsonl(LAST_GOAL, payload)


def record_goal_step_decision(
    goal: dict[str, Any],
    step: dict[str, Any],
    decision: str,
) -> dict[str, Any]:
    """Record a goal step approval decision."""
    step["decision"] = decision
    security = create_trust_metadata(
        glyph=str(goal.get("glyph") or "@"),
        mode="propose",
        inputs=[str(goal.get("last_event_id"))] if goal.get("last_event_id") else [],
    )
    payload = {
        "type": "goal_step_decision",
        "goal_id": goal.get("goal_id"),
        "step_id": step.get("id"),
        "decision": decision,
        "goal": goal,
        **security,
    }
    global_event = append_event(payload)
    step["decision_event_id"] = global_event["id"]
    goal["last_event_id"] = global_event["id"]
    payload["goal"] = goal
    return append_jsonl(LAST_GOAL, payload)


def record_goal_step_executed(
    goal: dict[str, Any],
    step: dict[str, Any],
    status: int,
) -> dict[str, Any]:
    """Record completion of one goal step."""
    security = create_trust_metadata(
        glyph=str(goal.get("glyph") or "@"),
        mode="execute-write",
        inputs=[str(step.get("decision_event_id"))]
        if step.get("decision_event_id")
        else [],
    )
    payload = {
        "type": "goal_step_executed",
        "goal_id": goal.get("goal_id"),
        "step_id": step.get("id"),
        "status": status,
        "goal": goal,
        **security,
    }
    global_event = append_event(payload)
    step["execution_event_id"] = global_event["id"]
    goal["last_event_id"] = global_event["id"]
    payload["goal"] = goal
    return append_jsonl(LAST_GOAL, payload)
