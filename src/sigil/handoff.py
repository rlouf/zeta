"""Sigil-owned shell handoff capture and reconciliation."""

from __future__ import annotations

import os
from typing import Any

from .session import event_time, recent_turns, record_turn
from .protocols import (
    SHELL_HANDOFF_OUTCOME_CANCELLED,
    SHELL_HANDOFF_OUTCOME_EXECUTED,
    SHELL_HANDOFF_OUTCOME_NO_PENDING,
    SHELL_HANDOFF_CANCEL_EXPECTED_NOT_EXECUTED,
    SHELL_HANDOFF_CANCEL_NO_TURNS,
    SHELL_HANDOFF_RESULT_SCHEMA,
    SHELL_HANDOFF_RESULT_TYPE,
    is_shell_handoff_result,
    is_shell_prompt_handoff,
)
from .zeta import runtime as zeta_runtime


def append_shell_turn(
    command: str,
    status: int,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Record one user shell command for the current Zeta continuation."""
    turn_cwd = cwd or os.getcwd()
    record_turn(command, status, turn_cwd)
    return {
        "ok": True,
        "type": "shell_turn_recorded",
        "command": command,
        "status": status,
        "cwd": turn_cwd,
    }


def append_shell_result() -> dict[str, Any]:
    """Append a synthetic tool result for commands run after a shell handoff."""
    return zeta_runtime.record_event(
        shell_result_event(zeta_runtime.current_timeline())
    )


def shell_result_event(timeline: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the timeline event that resumes a user-run shell handoff."""
    handoff = latest_unresolved_shell_handoff(timeline)
    if not handoff:
        return {
            "type": "shell_resume",
            "result": no_pending_handoff_result(recent_turns(limit=10)),
        }
    result = shell_handoff_result(handoff, recent_turns(limit=10))
    return {
        "type": "tool_result",
        "tool_call_id": handoff.get("tool_call_id") or "",
        "name": handoff.get("name") or "bash",
        "result": result,
    }


def shell_handoff_result(
    handoff: dict[str, Any],
    turns: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return an execution or cancellation result for a shell handoff."""
    expected = str(handoff.get("command") or "")
    handoff_time = event_time(handoff)
    turns_after_handoff = [
        normalize_shell_turn(turn) for turn in turns if event_time(turn) > handoff_time
    ]
    matching_turn = first_matching_turn(expected, turns_after_handoff)
    if matching_turn is None:
        first_turn = turns_after_handoff[0] if turns_after_handoff else {}
        actual = str(first_turn.get("command") or "")
        return cancelled_shell_result(
            handoff,
            actual,
            turns_after_handoff,
        )
    return executed_shell_result(handoff, matching_turn, turns_after_handoff)


def executed_shell_result(
    handoff: dict[str, Any],
    turn: dict[str, Any],
    turns: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return a tool result for the command the model staged, edited or not."""
    command = str(turn.get("command") or "")
    expected = str(handoff.get("command") or command)
    edited = normalize_command(command) != normalize_command(expected)
    status = turn.get("status")
    lines = [f"{command} (exit {status})"]
    stderr = turn.get("stderr_snippet")
    stdout = turn.get("stdout_snippet")
    if isinstance(stderr, str) and stderr:
        lines.append(f"stderr: {stderr}")
    if isinstance(stdout, str) and stdout:
        lines.append(f"stdout: {stdout}")
    if edited:
        lines.insert(
            0,
            f"The user edited the staged command before running it (staged: {expected}).",
        )
    preceding_count = turns.index(turn)
    if preceding_count:
        lines.insert(
            0,
            f"{preceding_count} user shell turn(s) occurred before the command.",
        )
    return {
        "ok": True,
        "schema": SHELL_HANDOFF_RESULT_SCHEMA,
        "type": SHELL_HANDOFF_RESULT_TYPE,
        "outcome": SHELL_HANDOFF_OUTCOME_EXECUTED,
        "edited": edited,
        "handoff": shell_handoff_summary(handoff),
        "expected_command": expected,
        "executed_command": command,
        "command": command,
        "status": status,
        "cwd": turn.get("cwd"),
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "shell_turns": turns,
        "turns": turns,
    }


def cancelled_shell_result(
    handoff: dict[str, Any],
    actual: str,
    turns: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return a tool result that says the staged shell call was not executed."""
    expected = str(handoff.get("command") or "")
    reason_code = SHELL_HANDOFF_CANCEL_NO_TURNS
    reason = "No shell command was recorded after the handoff."
    if actual:
        reason_code = SHELL_HANDOFF_CANCEL_EXPECTED_NOT_EXECUTED
        reason = (
            "The user did not run the proposed command. "
            f"First command after handoff: {actual}"
        )
    return {
        "ok": False,
        "schema": SHELL_HANDOFF_RESULT_SCHEMA,
        "type": SHELL_HANDOFF_RESULT_TYPE,
        "outcome": SHELL_HANDOFF_OUTCOME_CANCELLED,
        "cancelled": True,
        "cancellation_reason": reason_code,
        "handoff": shell_handoff_summary(handoff),
        "expected_command": expected,
        "actual_command": actual,
        "executed_command": "",
        "content": [{"type": "text", "text": reason}],
        "shell_turns": turns,
        "turns": turns,
    }


def no_pending_handoff_result(turns: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a deterministic resume event when no shell handoff is pending."""
    shell_turns = [normalize_shell_turn(turn) for turn in turns]
    return {
        "ok": True,
        "schema": SHELL_HANDOFF_RESULT_SCHEMA,
        "type": SHELL_HANDOFF_RESULT_TYPE,
        "outcome": SHELL_HANDOFF_OUTCOME_NO_PENDING,
        "content": [
            {
                "type": "text",
                "text": "No unresolved shell handoff was pending.",
            }
        ],
        "shell_turns": shell_turns,
        "turns": shell_turns,
    }


def first_matching_turn(
    expected: str,
    turns: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return the first shell turn that ran the handoff command, possibly edited.

    Whitespace differences never count as an edit, and a command the user
    extended with extra arguments still counts as executed rather than
    cancelled.
    """
    normalized_expected = normalize_command(expected)
    if not normalized_expected:
        return None
    for turn in turns:
        command = normalize_command(str(turn.get("command") or ""))
        if command == normalized_expected:
            return turn
        if command.startswith(f"{normalized_expected} "):
            return turn
    return None


def normalize_command(text: str) -> str:
    """Collapse whitespace runs so formatting edits do not change matching."""
    return " ".join(text.split())


def latest_unresolved_shell_handoff(
    timeline: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return metadata for the latest bash handoff in a timeline."""
    resolved_call_ids: set[str] = set()
    for event in reversed(timeline):
        result = event.get("result")
        if not isinstance(result, dict):
            continue
        tool_call_id = str(event.get("tool_call_id") or "")
        if is_shell_handoff_result(result):
            if tool_call_id:
                resolved_call_ids.add(tool_call_id)
            continue
        handoff = result.get("handoff")
        if not is_shell_prompt_handoff(handoff):
            continue
        if tool_call_id and tool_call_id in resolved_call_ids:
            continue
        return {
            "tool_call_id": tool_call_id,
            "name": str(event.get("name") or "bash"),
            "command": str(handoff.get("command") or ""),
            "reason": str(handoff.get("reason") or ""),
            "artifact": str(handoff.get("artifact") or ""),
            "time": event.get("time"),
        }
    return {}


def shell_handoff_summary(handoff: dict[str, Any]) -> dict[str, Any]:
    """Return the stable subset of handoff metadata for timeline context."""
    summary = {
        "tool_call_id": str(handoff.get("tool_call_id") or ""),
        "tool": str(handoff.get("name") or "bash"),
        "command": str(handoff.get("command") or ""),
        "reason": str(handoff.get("reason") or ""),
        "time": handoff.get("time"),
    }
    artifact = str(handoff.get("artifact") or "")
    if artifact:
        summary["artifact"] = artifact
    return summary


def normalize_shell_turn(turn: dict[str, Any]) -> dict[str, Any]:
    """Return a stable representation of a recorded user shell command."""
    normalized = {
        "id": str(turn.get("id") or ""),
        "time": event_time(turn),
        "command": str(turn.get("command") or ""),
        "status": turn.get("status"),
        "cwd": turn.get("turn_cwd") or turn.get("cwd") or "",
    }
    stdout = turn.get("stdout_snippet")
    stderr = turn.get("stderr_snippet")
    if isinstance(stdout, str) and stdout:
        normalized["stdout_snippet"] = stdout
    if isinstance(stderr, str) and stderr:
        normalized["stderr_snippet"] = stderr
    return normalized
