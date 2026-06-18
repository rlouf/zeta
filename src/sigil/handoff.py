"""Sigil-owned shell handoff capture and reconciliation."""

import uuid
from typing import Any, cast

from sigil.protocols import (
    EFFECT_KIND_HANDOFF,
    SHELL_HANDOFF_CANCEL_EXPECTED_NOT_EXECUTED,
    SHELL_HANDOFF_CANCEL_NO_TURNS,
    SHELL_HANDOFF_OUTCOME_CANCELLED,
    SHELL_HANDOFF_OUTCOME_EXECUTED,
    SHELL_HANDOFF_OUTCOME_NO_PENDING,
    SHELL_HANDOFF_RESULT_SCHEMA,
    SHELL_HANDOFF_RESULT_TYPE,
    is_shell_handoff_result,
    is_shell_prompt_handoff,
)
from sigil.sessions import event_time, recent_turns, session_id
from sigil.state import event_store_path
from zeta.capabilities.base import proposed_effect
from zeta.history import effect_record, publish_effect_record
from zeta.loop import tool_called_draft, tool_durable_payload
from zeta.timeline import (
    current_timeline,
    record_event,
    timeline_event_from_durable_event,
)


def append_shell_result() -> dict[str, Any]:
    """Append a synthetic tool result for commands run after a shell handoff."""
    from sigil import zeta_session_for_sigil

    runtime_context = zeta_session_for_sigil()
    event = shell_result_event(current_timeline(runtime_context=runtime_context))
    if event.get("type") == "tool_result":
        return record_shell_tool_result(event)
    return record_event(event, runtime_context=runtime_context)


def record_shell_tool_result(event: dict[str, Any]) -> dict[str, Any]:
    """Persist shell handoff results as tool events owned by handoff code."""
    from sigil import zeta_session_for_sigil

    runtime_context = zeta_session_for_sigil()
    outcome = runtime_context.event_sink.accept(
        tool_called_draft(
            payload=tool_durable_payload(event),
            turn_id=event.get("turn_id")
            if isinstance(event.get("turn_id"), str)
            else None,
            session_id=runtime_context.session_id,
            caused_by=event.get("caused_by")
            if isinstance(event.get("caused_by"), str)
            else None,
            event_id=event.get("id") if isinstance(event.get("id"), str) else None,
        )
    )
    return timeline_event_from_durable_event(outcome.event)


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
        result = cancelled_shell_result(
            handoff,
            actual,
            turns_after_handoff,
        )
    else:
        result = executed_shell_result(handoff, matching_turn, turns_after_handoff)
    record_handoff_effect(handoff, result)
    return result


def record_handoff_effect(
    handoff: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """Append the turn effect linking a staged handoff to what actually ran."""
    command = str(
        result.get("executed_command") or result.get("expected_command") or ""
    )
    status = result.get("status")
    publish_effect_record(
        effect_record(
            str(uuid.uuid4()),
            turn_id=str(handoff.get("turn_id") or ""),
            kind=EFFECT_KIND_HANDOFF,
            staged=True,
            command=command or None,
            exit_status=status if isinstance(status, int) else None,
            tool_call_id=str(handoff.get("tool_call_id") or "") or None,
            resolved_outcome=str(result.get("outcome") or ""),
        ),
        path=event_store_path(),
        session_id=session_id(),
    )


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
        "effect": {
            "kind": "command",
            "status": "resolved",
            "outcome": SHELL_HANDOFF_OUTCOME_EXECUTED,
            "command": command,
            "proposed_command": expected,
        },
        "handoff": shell_handoff_summary(handoff),
        "expected_command": expected,
        "executed_command": command,
        "command": command,
        "status": status,
        "cwd": turn.get("cwd"),
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "shell_turns": turns,
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
        "effect": {
            "kind": "command",
            "status": "cancelled",
            "outcome": SHELL_HANDOFF_OUTCOME_CANCELLED,
            "command": expected,
            "actual_command": actual,
        },
        "handoff": shell_handoff_summary(handoff),
        "expected_command": expected,
        "actual_command": actual,
        "executed_command": "",
        "content": [{"type": "text", "text": reason}],
        "shell_turns": turns,
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
    }


def first_matching_turn(
    expected: str,
    turns: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return the first shell turn that ran the handoff command, possibly edited."""
    for turn in turns:
        if command_matches_staged(expected, str(turn.get("command") or "")):
            return turn
    return None


def command_matches_staged(expected: str, command: str) -> bool:
    """Return whether a command counts as executing the staged one.

    Whitespace differences never count as an edit, and a command the user
    extended with extra arguments still counts as executed rather than
    cancelled.
    """
    normalized_expected = normalize_command(expected)
    if not normalized_expected:
        return False
    normalized = normalize_command(command)
    if normalized == normalized_expected:
        return True
    return normalized.startswith(f"{normalized_expected} ")


def matching_pending_handoff(
    command: str,
    timeline: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return the pending shell handoff that a just-run command resolves."""
    handoff = latest_unresolved_shell_handoff(timeline)
    if not handoff:
        return {}
    if not command_matches_staged(str(handoff.get("command") or ""), command):
        return {}
    return handoff


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
        handoff = shell_handoff_metadata(result)
        if not handoff:
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
            "turn_id": str(event.get("turn_id") or ""),
        }
    return {}


def shell_handoff_metadata(result: dict[str, Any]) -> dict[str, Any]:
    effect = proposed_effect(result)
    if effect is not None and effect.get("kind") == "command":
        return {
            "command": str(effect.get("command") or ""),
            "reason": str(effect.get("reason") or ""),
            "artifact": str(effect.get("artifact") or ""),
        }
    handoff = result.get("handoff")
    if is_shell_prompt_handoff(handoff):
        handoff = cast(dict[str, Any], handoff)
        return {
            "command": str(handoff.get("command") or ""),
            "reason": str(handoff.get("reason") or ""),
            "artifact": str(handoff.get("artifact") or ""),
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
