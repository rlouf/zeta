"""Zeta v1 runtime services used by Sigil step runners."""

from __future__ import annotations

import json
import os
from typing import Any, Iterable, TextIO, cast

from ..state import append_jsonl, read_jsonl
from .model import chat_json_messages, model_endpoint_open
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
    *,
    context: str = "",
) -> str:
    sections = [
        f"Objective:\n{objective}",
        f"cwd:\n{os.getcwd()}",
    ]
    if context.strip():
        sections.append(context.strip())
    sections.append(
        f"Recent transcript JSON:\n{json.dumps(transcript[-20:], ensure_ascii=False)}"
    )
    return "\n\n".join(sections)


def zeta_context_message(
    objective: str,
    *,
    context: str = "",
) -> str:
    sections = [
        f"Objective:\n{objective}",
        f"cwd:\n{os.getcwd()}",
    ]
    if context.strip():
        sections.append(context.strip())
    return "\n\n".join(sections)


def zeta_chat_messages(
    objective: str,
    transcript: list[dict[str, Any]],
    *,
    system: str | None = None,
    allowed_tools: Iterable[str] | None = None,
    context: str = "",
) -> list[dict[str, Any]]:
    messages = [
        {
            "role": "system",
            "content": zeta_system_prompt(system, allowed_tools=allowed_tools),
        },
        {"role": "user", "content": zeta_context_message(objective, context=context)},
    ]
    messages.extend(transcript_chat_messages(transcript[-20:]))
    return messages


def transcript_chat_messages(
    transcript: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    tool_call_ids: set[str] = set()
    for index, event in enumerate(transcript):
        message = role_chat_message(event)
        if message is not None:
            messages.append(message)
            continue
        event_type = str(event.get("type") or "")
        message = event_chat_message(event_type, event)
        if message is not None:
            messages.append(message)
            continue
        if event_type == "tool_call":
            message = tool_call_message(event, fallback_id=f"call-{index}")
            messages.append(message)
            record_tool_call_ids(message, tool_call_ids)
            continue
        if event_type == "tool_result":
            messages.append(tool_result_message(event, tool_call_ids))
    return messages


def role_chat_message(event: dict[str, Any]) -> dict[str, Any] | None:
    role = str(event.get("role") or "")
    if role not in {"user", "assistant"}:
        return None
    content = str(event.get("content") or "")
    if not content:
        return None
    return {"role": role, "content": content}


def event_chat_message(
    event_type: str,
    event: dict[str, Any],
) -> dict[str, Any] | None:
    role_by_type = {
        "user_message": "user",
        "assistant_message": "assistant",
    }
    role = role_by_type.get(event_type)
    if role is None:
        return None
    content = str(event.get("content") or "")
    if not content:
        return None
    return {"role": role, "content": content}


def record_tool_call_ids(
    message: dict[str, Any],
    tool_call_ids: set[str],
) -> None:
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return
    for call in tool_calls:
        if isinstance(call, dict):
            tool_call_ids.add(str(call.get("id") or ""))


def tool_call_message(
    event: dict[str, Any],
    *,
    fallback_id: str,
) -> dict[str, Any]:
    tool_call_id = str(event.get("id") or event.get("tool_call_id") or fallback_id)
    tool_name = str(event.get("name") or "")
    tool_input = event.get("input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": tool_call_id,
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(
                        tool_input,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            }
        ],
    }


def tool_result_message(
    event: dict[str, Any],
    tool_call_ids: set[str],
) -> dict[str, Any]:
    tool_call_id = str(event.get("tool_call_id") or "")
    if tool_call_id and tool_call_id in tool_call_ids:
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": json.dumps(
                event.get("result") or {},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }
    return {
        "role": "user",
        "content": "Tool result JSON:\n"
        + json.dumps(event, ensure_ascii=False, separators=(",", ":")),
    }


def next_model_action(
    objective: str,
    transcript: list[dict[str, Any]],
    *,
    system: str | None = None,
    allowed_tools: Iterable[str] | None = None,
    context: str = "",
) -> dict[str, Any]:
    if not model_endpoint_open():
        raise RuntimeError("model endpoint is not reachable")
    allowed = set(allowed_tools) if allowed_tools is not None else None
    data = chat_json_messages(
        zeta_chat_messages(
            objective,
            transcript,
            system=system,
            allowed_tools=allowed,
            context=context,
        ),
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
    context = str(request.get("context") or "")
    transcript = request.get("transcript")
    if not isinstance(transcript, list):
        transcript = transcript_tail()
    action = next_model_action(
        objective,
        cast(list[dict[str, Any]], transcript),
        context=context,
    )
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
