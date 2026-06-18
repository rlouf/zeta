"""Prompt component construction for Zeta."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from ..capabilities import content_hash, effect_resolution, proposed_effect
from ..skills import Skill, available_skills, expand_skill_directive
from ..substrate import (
    Object,
    ObjectId,
    add_event_link,
    trace_object_id,
)
from .system import (
    can_read_skill_files,
    enabled_capability_ids,
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

    def message_payload(self) -> dict[str, Any] | None:
        return self.message

    def object_data(self) -> dict[str, Any]:
        data = dict(self.data)
        if self.message is not None and "message" not in data:
            data["message"] = self.message
        data["representation"] = self.representation
        if self.source_object_id is not None:
            data["source_object_id"] = self.source_object_id
        return data

    def object_links(self) -> tuple[ObjectId, ...]:
        return self.links


@dataclass(frozen=True)
class ChatMessageEntry:
    """A rendered chat message plus the timeline event that produced it."""

    event_index: int
    event: dict[str, Any]
    message: dict[str, Any]


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


def from_message_boundary(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop leading tool results whose calls fell outside a timeline window.

    The event-count tail can open mid tool exchange; an orphaned result
    renders as a raw JSON dump in the prompt. Reconciliation reads the raw
    timeline, so this trim belongs to the model-facing conversion only.
    """
    start = 0
    while start < len(events):
        if str(events[start].get("type") or "") != "tool_result":
            break
        start += 1
    return events[start:]


def tool_result_event_from_message(message: dict[str, Any]) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "tool_result",
        "tool_call_id": str(message.get("tool_call_id") or ""),
    }
    content = message.get("content")
    if isinstance(content, str):
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            result = {"content": content}
        if isinstance(result, dict):
            event["result"] = result
    return event


def chat_messages(
    timeline: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [entry.message for entry in _chat_message_entries(timeline)]


def _chat_message_entries(
    timeline: list[dict[str, Any]],
) -> list[ChatMessageEntry]:
    entries: list[ChatMessageEntry] = []
    tool_call_ids: set[str] = set()
    resolved_effects = resolved_effect_call_ids(timeline)
    for index, event in enumerate(timeline):
        message = timeline_chat_message(
            event,
            index=index,
            tool_call_ids=tool_call_ids,
            resolved_effects=resolved_effects,
        )
        if message is not None:
            entries.append(ChatMessageEntry(index, event, message))
            record_tool_call_ids(message, tool_call_ids)
    return entries


def timeline_chat_message(
    event: dict[str, Any],
    *,
    index: int,
    tool_call_ids: set[str],
    resolved_effects: set[str],
) -> dict[str, Any] | None:
    message = role_or_event_chat_message(event)
    if message is not None:
        return message
    event_type = str(event.get("type") or "")
    if event_type == "tool_call":
        tool_call_id = str(event.get("id") or event.get("tool_call_id") or "")
        if tool_call_id and tool_call_id in tool_call_ids:
            return None
        return tool_call_message(event, fallback_id=f"call-{index}")
    if event_type == "tool_result":
        if is_resolved_proposed_effect(event, resolved_effects):
            return None
        return tool_result_message(event, tool_call_ids)
    return None


def resolved_effect_call_ids(timeline: list[dict[str, Any]]) -> set[str]:
    """Return tool call ids that have a proposed-effect resolution."""
    resolved: set[str] = set()
    for event in timeline:
        if str(event.get("type") or "") != "tool_result":
            continue
        result = event.get("result")
        if not isinstance(result, dict) or effect_resolution(result) is None:
            continue
        tool_call_id = str(event.get("tool_call_id") or "")
        if tool_call_id:
            resolved.add(tool_call_id)
    return resolved


def is_resolved_proposed_effect(
    event: dict[str, Any],
    resolved_effects: set[str],
) -> bool:
    """Return whether this proposal was superseded by a resolution result."""
    tool_call_id = str(event.get("tool_call_id") or "")
    if not tool_call_id or tool_call_id not in resolved_effects:
        return False
    result = event.get("result")
    if not isinstance(result, dict):
        return False
    effect = proposed_effect(result)
    return effect is not None


def role_or_event_chat_message(event: dict[str, Any]) -> dict[str, Any] | None:
    role = str(event.get("role") or "")
    if role not in {"user", "assistant"}:
        role = {
            "user_message": "user",
            "model": "assistant",
            "turn_aborted": "assistant",
        }.get(str(event.get("type") or ""), "")
    if not role:
        return None
    content = str(event.get("content") or "")
    tool_calls = event.get("tool_calls")
    if isinstance(tool_calls, list) and role == "assistant":
        return {
            "role": "assistant",
            "content": content or None,
            "tool_calls": [renderable_tool_call(call) for call in tool_calls],
        }
    if not content:
        return None
    return {"role": role, "content": content}


def renderable_tool_call(call: Any) -> Any:
    """Repair tool call arguments that are not valid JSON.

    A recorded tool call can carry truncated arguments; chat templates refuse to
    render them, which would fail every later prompt in the session.
    """
    if not isinstance(call, dict):
        return call
    function = call.get("function")
    if not isinstance(function, dict):
        return call
    arguments = function.get("arguments")
    if not isinstance(arguments, str):
        return call
    try:
        json.loads(arguments)
    except json.JSONDecodeError:
        repaired = json.dumps(
            {"truncated_arguments": arguments[:200]},
            ensure_ascii=False,
        )
        return {**call, "function": {**function, "arguments": repaired}}
    return call


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


RENDERED_EVENT_PRIVATE_FIELDS = frozenset(
    {
        "prompt_trace",
        "tool_call_object_id",
        "tool_result_object_id",
        "prompt_component_object_id",
        "source_object_id",
        "model_telemetry",
    }
)


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
    rendered = {
        key: value
        for key, value in event.items()
        if key not in RENDERED_EVENT_PRIVATE_FIELDS
    }
    return {
        "role": "user",
        "content": "Tool result JSON:\n"
        + json.dumps(rendered, ensure_ascii=False, separators=(",", ":")),
    }


def prompt_components(
    objective: str,
    timeline: list[dict[str, Any]],
    *,
    system: str | None = None,
    allowed_capabilities: Iterable[str] | None = None,
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
    enabled_capabilities = enabled_capability_ids(allowed_capabilities)
    if skills is None:
        skills = (
            available_skills() if can_read_skill_files(enabled_capabilities) else []
        )
    system_content = system_prompt(
        system,
        allowed_capabilities=enabled_capabilities,
        skills=skills,
    )
    components = [
        PromptComponent(
            kind="system_prompt",
            data={
                "content": system_content,
                "base_prompt": system,
                "allowed_tools": list(enabled_capabilities),
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
                enabled_capabilities=enabled_capabilities,
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
    if event_type == "model" and isinstance(event.get("tool_calls"), list):
        return structured_model_event(event)
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


def structured_model_event(event: dict[str, Any]) -> dict[str, Any]:
    data = {
        "type": "model",
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
    enabled_capabilities: tuple[str, ...],
    skills: list[Skill],
) -> list[PromptComponent]:
    components: list[PromptComponent] = []
    if tools is not None:
        components.append(
            PromptComponent(
                kind="tool_descriptor_set",
                data={
                    "allowed_tools": list(enabled_capabilities),
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
        message
        for component in components
        if (message := component.message_payload()) is not None
    ]


def prompt_component_object(component: PromptComponent) -> Object:
    return Object(
        kind=component.kind,
        schema="zeta.prompt_component.v1",
        data=component.object_data(),
        links=component.object_links(),
    )
