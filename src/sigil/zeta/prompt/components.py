"""Prompt component construction for Zeta."""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from ..skills import Skill, available_skills, expand_skill_directive
from ..timeline import (
    ChatMessageEntry,
    _chat_message_entries,
    add_event_link,
    from_message_boundary,
    trace_object_id,
)
from ..tools.base import content_hash
from ..trace import Object, ObjectId
from .system import (
    can_read_skill_files,
    enabled_tool_names,
    skill_prompt_items,
    system_prompt,
)

Representation = Literal["full", "summary", "stub"]

# How much of the (unbounded) timeline projection the prompt carries.
TIMELINE_TAIL_LIMIT = 50


@dataclass(frozen=True)
class PromptComponent:
    """A first-class prompt component that can become a trace object."""

    kind: str
    data: dict[str, Any] = field(default_factory=dict)
    message: dict[str, Any] | None = None
    representation: Representation = "full"
    source_object_id: ObjectId | None = None
    links: tuple[ObjectId, ...] = ()
    object_id: ObjectId | None = None


def zeta_context_message(
    objective: str,
    *,
    context: str = "",
) -> str:
    objective = expand_skill_directive(objective)
    sections = [
        objective,
        f"cwd:\n{os.getcwd()}",
    ]
    if context.strip():
        sections.append(context.strip())
    return "\n\n".join(sections)


def prompt_components(
    objective: str,
    timeline: list[dict[str, Any]],
    *,
    system: str | None = None,
    allowed_tools: Iterable[str] | None = None,
    context: str = "",
    current_events: Iterable[dict[str, Any]] = (),
    tools: list[dict[str, Any]] | None = None,
    include_non_message_components: bool = True,
    skills: list[Skill] | None = None,
) -> list[PromptComponent]:
    """Return prompt components in stable prefix-cache-friendly order.

    Public ordering contract: system_prompt, tool descriptors, project context,
    then volatile timeline/objective/current-turn components.
    """
    enabled_tools = enabled_tool_names(allowed_tools)
    if skills is None:
        skills = available_skills() if can_read_skill_files(enabled_tools) else []
    system_content = system_prompt(system, allowed_tools=enabled_tools, skills=skills)
    components = [
        PromptComponent(
            kind="system_prompt",
            data={
                "content": system_content,
                "base_prompt": system,
                "allowed_tools": list(enabled_tools),
            },
            message={"role": "system", "content": system_content},
        )
    ]
    if include_non_message_components:
        components.extend(
            non_message_components(
                objective,
                context=context,
                tools=tools,
                enabled_tools=enabled_tools,
                skills=skills,
            )
        )
    components.extend(
        timeline_message_components(
            from_message_boundary(timeline[-TIMELINE_TAIL_LIMIT:]),
            historical=True,
        )
    )
    objective_message = zeta_context_message(objective, context=context)
    components.append(
        PromptComponent(
            kind="user_message",
            data={
                "objective": objective,
                "expanded_objective": expand_skill_directive(objective),
                "context": context,
                "message": {"role": "user", "content": objective_message},
            },
            message={"role": "user", "content": objective_message},
        )
    )
    components.extend(
        timeline_message_components(
            list(current_events),
            historical=False,
        )
    )
    return components


def timeline_message_components(
    events: list[dict[str, Any]],
    *,
    historical: bool,
) -> list[PromptComponent]:
    entries = _chat_message_entries(events)
    components = []
    tool_call_names: dict[str, str] = {}
    for message_index, entry in enumerate(entries):
        tool_name = (
            tool_call_names.get(str(entry.event.get("tool_call_id") or "")) or ""
        )
        components.append(
            PromptComponent(
                kind=message_component_kind(entry.message),
                data=timeline_message_component_data(
                    message_index,
                    entry,
                    tool_name=tool_name,
                    historical=historical,
                ),
                message=entry.message,
                links=timeline_message_component_links(entry.event),
            )
        )
        record_tool_call_names(entry.message, tool_call_names)
    return components


def timeline_message_component_data(
    message_index: int,
    entry: ChatMessageEntry,
    *,
    tool_name: str = "",
    historical: bool = False,
) -> dict[str, Any]:
    data = {
        "index": message_index,
        "event_index": entry.event_index,
        "message": entry.message,
        "source_event_type": str(entry.event.get("type") or ""),
        "source_event_role": str(entry.event.get("role") or ""),
    }
    if historical:
        data["historical"] = True
    if tool_name:
        data["source_tool_name"] = tool_name
    source_event_value = structured_source_event(entry.event, tool_name=tool_name)
    if source_event_value:
        data["source_event"] = source_event_value
    return data


def timeline_message_component_links(event: dict[str, Any]) -> tuple[ObjectId, ...]:
    links: list[ObjectId] = []
    add_event_link(links, assistant_message_object_id(event))
    for trace_field in ("tool_result_object_id", "tool_call_object_id"):
        add_event_link(links, trace_object_id(event, trace_field))
    return tuple(links)


def assistant_message_object_id(event: dict[str, Any]) -> ObjectId | None:
    prompt_trace = event.get("prompt_trace")
    if not isinstance(prompt_trace, dict):
        return None
    return trace_object_id(prompt_trace, "assistant_message_object_id")


def record_tool_call_names(
    message: dict[str, Any],
    tool_call_names: dict[str, str],
) -> None:
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        call_id = str(call.get("id") or "")
        function = call.get("function")
        name = function.get("name") if isinstance(function, dict) else None
        if call_id and isinstance(name, str) and name:
            tool_call_names[call_id] = name


def structured_source_event(
    event: dict[str, Any],
    *,
    tool_name: str = "",
) -> dict[str, Any]:
    event_type = str(event.get("type") or "")
    if event_type == "tool_result":
        return structured_tool_result_event(event, tool_name=tool_name)
    if event_type == "tool_call":
        return structured_tool_call_event(event)
    if event_type == "assistant_message" and isinstance(event.get("tool_calls"), list):
        return structured_assistant_message_event(event)
    return {}


def structured_tool_result_event(
    event: dict[str, Any],
    *,
    tool_name: str = "",
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "type": "tool_result",
        "tool_call_id": str(event.get("tool_call_id") or ""),
    }
    add_trace_object_field(data, event, "tool_result_object_id")
    add_trace_object_field(data, event, "tool_call_object_id")
    if tool_name:
        data["tool_name"] = tool_name
    result = event.get("result")
    if isinstance(result, dict):
        data["result"] = result
    return data


def structured_tool_call_event(event: dict[str, Any]) -> dict[str, Any]:
    data = {
        "type": "tool_call",
        "id": str(event.get("id") or ""),
        "tool_call_id": str(event.get("tool_call_id") or ""),
        "name": str(event.get("name") or ""),
        "input": event.get("input") if isinstance(event.get("input"), dict) else {},
    }
    add_trace_object_field(data, event, "tool_call_object_id")
    return data


def structured_assistant_message_event(event: dict[str, Any]) -> dict[str, Any]:
    data = {
        "type": "assistant_message",
        "tool_calls": event.get("tool_calls") or [],
    }
    object_id = assistant_message_object_id(event)
    if object_id is not None:
        data["assistant_message_object_id"] = object_id
    return data


def add_trace_object_field(
    data: dict[str, Any],
    event: dict[str, Any],
    field_name: str,
) -> None:
    object_id = trace_object_id(event, field_name)
    if object_id is not None:
        data[field_name] = object_id


def non_message_components(
    objective: str,
    *,
    context: str,
    tools: list[dict[str, Any]] | None,
    enabled_tools: tuple[str, ...],
    skills: list[Skill],
) -> list[PromptComponent]:
    components: list[PromptComponent] = []
    if tools is not None:
        components.append(
            PromptComponent(
                kind="tool_descriptor_set",
                data={
                    "allowed_tools": list(enabled_tools),
                    "tools": tools,
                },
            )
        )
    if skills:
        components.append(
            PromptComponent(
                kind="skill_context",
                data={"skills": skill_prompt_items(skills)},
            )
        )
    if context.strip():
        # The context text itself ships inside the user_message message;
        # this component records provenance without double-counting it.
        content = context.strip()
        components.append(
            PromptComponent(
                kind="project_context",
                data={
                    "sha256": content_hash(content),
                    "chars": len(content),
                },
            )
        )
    return components


def message_component_kind(message: dict[str, Any]) -> str:
    if message.get("role") == "tool":
        return "tool_result"
    if message.get("role") == "assistant":
        return "assistant_message"
    return "user_message"


def component_messages(components: list[PromptComponent]) -> list[dict[str, Any]]:
    return [
        component.message for component in components if component.message is not None
    ]


def prompt_component_object(component: PromptComponent) -> Object:
    data = dict(component.data)
    if component.message is not None and "message" not in data:
        data["message"] = component.message
    data["representation"] = component.representation
    if component.source_object_id is not None:
        data["source_object_id"] = component.source_object_id
    return Object(
        kind=component.kind,
        schema="zeta.prompt_component.v1",
        data=data,
        links=component.links,
    )
