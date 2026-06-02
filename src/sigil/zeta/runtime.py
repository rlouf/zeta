"""Zeta v1 runtime services used by the shell loop."""

from __future__ import annotations

import json
import os
from typing import Any, Iterable, TextIO, cast

from ..model import chat_json, ensure_server
from ..session import recent_turns, recent_turns_context
from ..state import append_jsonl, read_jsonl
from . import tools as tool_registry
from .prompt import system_prompt

TRANSCRIPT = "zeta-transcript.jsonl"
DEFAULT_TAIL_LIMIT = 50
TOOL_SPECS = tool_registry.TOOL_SPECS


def tool_metadata(name: str) -> dict[str, Any]:
    return tool_registry.tool_metadata(name)


def allowed_tool_names(allowed_tools: Iterable[str] | None = None) -> list[str]:
    return tool_registry.allowed_tool_names(allowed_tools)


def tools_list(allowed_tools: Iterable[str] | None = None) -> dict[str, Any]:
    return tool_registry.tools_list(allowed_tools)


def model_tool_descriptors(
    allowed_tools: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    return tool_registry.model_tool_descriptors(allowed_tools)


def analyze_tool(name: str, params: dict[str, Any]) -> dict[str, Any]:
    return tool_registry.analyze_tool(name, params)


def run_tool(name: str, params: dict[str, Any]) -> dict[str, Any]:
    return tool_registry.run_tool(name, params)


def model_action_schema(allowed_tools: Iterable[str] | None = None) -> dict[str, Any]:
    names = allowed_tool_names(allowed_tools)
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["type"],
        "oneOf": [
            {
                "required": ["type", "content"],
                "properties": {
                    "type": {"type": "string", "enum": ["final"]},
                    "content": {"type": "string", "minLength": 1},
                },
            },
            {
                "required": ["type", "name", "input"],
                "properties": {
                    "type": {"type": "string", "enum": ["tool_call"]},
                    "name": {"type": "string", "enum": names},
                    "input": {"type": "object", "additionalProperties": True},
                },
            },
        ],
        "properties": {
            "type": {
                "type": "string",
                "enum": ["tool_call", "final"],
            },
            "name": {
                "type": "string",
                "enum": names,
            },
            "input": {
                "type": "object",
                "additionalProperties": True,
            },
            "content": {"type": "string"},
        },
    }


def append_transcript(event: dict[str, Any]) -> dict[str, Any]:
    return append_jsonl(TRANSCRIPT, event)


def append_shell_result() -> dict[str, Any]:
    """Append a synthetic tool result for commands run after a shell handoff."""
    return append_transcript(shell_result_event(transcript_tail()))


def shell_result_event(transcript: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the transcript event that resumes a user-run shell handoff."""
    handoff = latest_shell_handoff(transcript)
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
    turns_after_handoff = [turn for turn in turns if event_time(turn) > handoff_time]
    first_turn = turns_after_handoff[0] if turns_after_handoff else None
    if first_turn is None:
        return cancelled_shell_result(expected, "", turns_after_handoff)
    actual = str(first_turn.get("command") or "")
    if actual != expected:
        return cancelled_shell_result(expected, actual, turns_after_handoff)
    return executed_shell_result(first_turn, turns_after_handoff)


def executed_shell_result(
    turn: dict[str, Any],
    turns: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return a tool result for the exact command the model staged."""
    command = str(turn.get("command") or "")
    status = turn.get("status")
    lines = [f"{command} (exit {status})"]
    stderr = turn.get("stderr_snippet")
    stdout = turn.get("stdout_snippet")
    if isinstance(stderr, str) and stderr:
        lines.append(f"stderr: {stderr}")
    if isinstance(stdout, str) and stdout:
        lines.append(f"stdout: {stdout}")
    return {
        "ok": True,
        "type": "shell_command_result",
        "command": command,
        "status": status,
        "cwd": turn.get("turn_cwd") or turn.get("cwd"),
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "turns": turns,
    }


def cancelled_shell_result(
    expected: str,
    actual: str,
    turns: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return a tool result that says the staged shell call was not executed."""
    reason = "No shell command was recorded after the handoff."
    if actual:
        reason = f"User ran a different command instead: {actual}"
    return {
        "ok": False,
        "type": "shell_call_cancelled",
        "cancelled": True,
        "expected_command": expected,
        "actual_command": actual,
        "content": [{"type": "text", "text": reason}],
        "turns": turns,
    }


def latest_shell_handoff(transcript: list[dict[str, Any]]) -> dict[str, Any]:
    """Return metadata for the latest bash handoff in a transcript."""
    for event in reversed(transcript):
        result = event.get("result")
        if not isinstance(result, dict):
            continue
        handoff = result.get("handoff")
        if not isinstance(handoff, dict):
            continue
        if str(handoff.get("type") or "") != "shell_prompt":
            continue
        return {
            "tool_call_id": str(event.get("tool_call_id") or ""),
            "name": str(event.get("name") or "bash"),
            "command": str(handoff.get("command") or ""),
            "time": event.get("time"),
        }
    return {}


def event_time(event: dict[str, Any]) -> float:
    value = event.get("time")
    return value if isinstance(value, int | float) else 0.0


def transcript_tail(limit: int = DEFAULT_TAIL_LIMIT) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    return read_jsonl(TRANSCRIPT)[-limit:]


def zeta_system_prompt(
    route_prompt: str | None = None,
    *,
    allowed_tools: Iterable[str] | None = None,
) -> str:
    return system_prompt(route_prompt, allowed_tools=allowed_tools)


def zeta_user_prompt(
    objective: str,
    transcript: list[dict[str, Any]],
) -> str:
    sections = [
        f"Objective:\n{objective}",
        f"cwd:\n{os.getcwd()}",
    ]
    turns_context = recent_turns_context()
    if turns_context:
        sections.append(turns_context)
    sections.append(
        f"Recent transcript JSON:\n{json.dumps(transcript[-20:], ensure_ascii=False)}"
    )
    return "\n\n".join(sections)


def next_model_action(
    objective: str,
    transcript: list[dict[str, Any]],
    *,
    system: str | None = None,
    allowed_tools: Iterable[str] | None = None,
) -> dict[str, Any]:
    if not ensure_server():
        raise RuntimeError("model endpoint is not reachable")
    allowed = set(allowed_tools) if allowed_tools is not None else None
    data = chat_json(
        zeta_system_prompt(system, allowed_tools=allowed),
        zeta_user_prompt(objective, transcript),
        model_action_schema(allowed),
    )
    action_type = str(data.get("type") or "")
    if action_type == "final":
        return {"type": "final", "content": str(data.get("content") or "")}
    name = str(data.get("name") or "")
    raw_input = data.get("input")
    if (
        name not in TOOL_SPECS
        or (allowed is not None and name not in allowed)
        or not isinstance(raw_input, dict)
    ):
        return {
            "type": "final",
            "content": "I could not choose a valid Zeta tool for the next step.",
        }
    return {"type": "tool_call", "name": name, "input": cast(dict[str, Any], raw_input)}


def stream_model_events(request: dict[str, Any]) -> Iterable[dict[str, Any]]:
    objective = str(request.get("objective") or request.get("prompt") or "")
    transcript = request.get("transcript")
    if not isinstance(transcript, list):
        transcript = transcript_tail()
    action = next_model_action(objective, cast(list[dict[str, Any]], transcript))
    if action["type"] == "final":
        content = str(action.get("content") or "")
        if content:
            yield {"type": "assistant_delta", "text": content}
        yield {"type": "final"}
        return
    yield {
        "type": "tool_call",
        "name": action["name"],
        "input": action["input"],
    }


def read_json_stdin(stdin: TextIO) -> dict[str, Any]:
    raw = stdin.read()
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("expected JSON object")
    return data
