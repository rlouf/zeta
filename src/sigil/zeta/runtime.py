"""Zeta v1 runtime services used by Sigil step runners."""

from __future__ import annotations

import json
import os
from typing import Any, Iterable, TextIO

from . import tools as tool_registry
from .context import load_project_context
from .prompt import system_prompt
from .skills import (
    available_skills,
    discover_skills,
    expand_skill_directive,
)
from .transcript import (
    DEFAULT_TAIL_LIMIT,
    TRANSCRIPT,
    append_transcript,
    event_chat_message,
    record_tool_call_ids,
    role_chat_message,
    tool_call_message,
    tool_result_message,
    transcript_chat_messages,
    transcript_tail,
)

TOOL_SPECS = tool_registry.TOOL_SPECS

__all__ = [
    "DEFAULT_TAIL_LIMIT",
    "TOOL_SPECS",
    "TRANSCRIPT",
    "allowed_tool_names",
    "analyze_tool",
    "append_transcript",
    "available_skills",
    "discover_skills",
    "event_chat_message",
    "expand_skill_directive",
    "load_project_context",
    "model_tool_descriptors",
    "read_json_stdin",
    "record_tool_call_ids",
    "role_chat_message",
    "run_tool",
    "tool_call_message",
    "tool_metadata",
    "tool_result_message",
    "tools_list",
    "transcript_chat_messages",
    "transcript_tail",
    "zeta_chat_messages",
    "zeta_context_message",
    "zeta_system_prompt",
]


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


def run_tool(
    name: str,
    params: dict[str, Any],
    *,
    edit_mode: str = "review_patch",
    execution_mode: tool_registry.ExecutionMode = "handoff",
) -> dict[str, Any]:
    return tool_registry.run_tool(
        name,
        params,
        edit_mode=edit_mode,
        execution_mode=execution_mode,
    )


def zeta_system_prompt(
    route_prompt: str | None = None,
    *,
    allowed_tools: Iterable[str] | None = None,
) -> str:
    enabled_tools = allowed_tool_names(allowed_tools)
    skills = available_skills() if "read" in enabled_tools else []
    return system_prompt(route_prompt, allowed_tools=enabled_tools, skills=skills)


def zeta_context_message(
    objective: str,
    *,
    context: str = "",
) -> str:
    objective = expand_skill_directive(objective)
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


def read_json_stdin(stdin: TextIO) -> dict[str, Any]:
    raw = stdin.read()
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("expected JSON object")
    return data
