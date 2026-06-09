"""Zeta v1 runtime services used by Sigil step runners."""

from __future__ import annotations

import json
from typing import Any, Iterable, TextIO

from . import tools as tool_registry
from .context import load_project_context
from .prompt import (
    ModelTaskStateExtractor,
    NoOpPromptTransform,
    PreparedPrompt,
    PromptBuilder,
    PromptComponent,
    PromptTransform,
    StructuralTrimPromptTransform,
    TASK_STATE_SCHEMA,
    TaskStateExtractionPromptTransform,
    TaskStateExtractor,
    can_read_skill_files,
    component_messages,
    prompt_components,
    system_prompt,
    task_state_extraction_messages,
    task_state_json,
    task_state_message,
    zeta_context_message,
)
from .skills import (
    available_skills,
    discover_skills,
    expand_skill_directive,
)
from .trace import PromptTrace
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
    "ModelTaskStateExtractor",
    "NoOpPromptTransform",
    "PreparedPrompt",
    "PromptBuilder",
    "PromptComponent",
    "PromptTransform",
    "PromptTrace",
    "StructuralTrimPromptTransform",
    "TASK_STATE_SCHEMA",
    "TaskStateExtractionPromptTransform",
    "TaskStateExtractor",
    "read_json_stdin",
    "record_tool_call_ids",
    "role_chat_message",
    "run_tool",
    "task_state_extraction_messages",
    "task_state_json",
    "task_state_message",
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
    skills = available_skills() if can_read_skill_files(enabled_tools) else []
    return system_prompt(route_prompt, allowed_tools=enabled_tools, skills=skills)


def zeta_chat_messages(
    objective: str,
    transcript: list[dict[str, Any]],
    *,
    system: str | None = None,
    allowed_tools: Iterable[str] | None = None,
    context: str = "",
    current_events: Iterable[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    return component_messages(
        prompt_components(
            objective,
            transcript,
            system=system,
            allowed_tools=allowed_tools,
            context=context,
            current_events=current_events,
            include_non_message_components=False,
        )
    )


def read_json_stdin(stdin: TextIO) -> dict[str, Any]:
    raw = stdin.read()
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("expected JSON object")
    return data
