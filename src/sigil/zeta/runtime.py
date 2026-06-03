"""Zeta v1 runtime services used by the shell loop."""

from __future__ import annotations

import json
import os
from typing import Any, Iterable, TextIO, cast

from ..state import append_jsonl, read_jsonl
from .model import chat_json, model_endpoint_open
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
    data = chat_json(
        zeta_system_prompt(system, allowed_tools=allowed),
        zeta_user_prompt(objective, transcript, context=context),
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
