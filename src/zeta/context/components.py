"""Prompt component construction for Zeta."""

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from zeta.capabilities.execution import content_hash, effect_resolution, proposed_effect
from zeta.context.system import (
    enabled_capability_ids,
    system_prompt,
)
from zeta.records.objects import Object, ObjectId

Representation = Literal["full", "summary", "stub"]

# How much of the (unbounded) timeline projection the prompt carries.
TIMELINE_TAIL_LIMIT = 50


@dataclass(frozen=True)
class PromptTrace:
    """Trace ids for one prompt request and its assistant response.

    Component ids ride on the prompt object's links, not here: carrying
    them in every event payload grew the store quadratically with turns.
    """

    prompt_object_id: ObjectId
    assistant_message_object_id: ObjectId | None = None


def prompt_trace_payload(trace: PromptTrace) -> dict[str, Any]:
    """Return JSON metadata for a prompt trace."""
    payload: dict[str, Any] = {"prompt_object_id": trace.prompt_object_id}
    if trace.assistant_message_object_id is not None:
        payload["assistant_message_object_id"] = trace.assistant_message_object_id
    return payload


def latest_prompt_trace_fields(prompt_traces: Iterable[Any]) -> dict[str, Any]:
    """Return event fields for the most recent valid prompt trace."""
    traces = list(prompt_traces)
    if not traces:
        return {}
    trace = traces[-1]
    if not isinstance(trace, PromptTrace):
        return {}
    return {"prompt_trace": prompt_trace_payload(trace)}


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
    return complete_tool_exchange_messages(
        [entry.message for entry in _chat_message_entries(timeline)]
    )


def _chat_message_entries(
    timeline: list[dict[str, Any]],
) -> list[ChatMessageEntry]:
    entries: list[ChatMessageEntry] = []
    tool_call_ids: set[str] = set()
    resolved_effects = resolved_effect_call_ids(timeline)
    for index, event in enumerate(timeline):
        message = _project_one_chat_message(
            event,
            index=index,
            tool_call_ids=tool_call_ids,
            resolved_effects=resolved_effects,
        )
        if message is not None:
            entries.append(ChatMessageEntry(index, event, message))
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list):
                for call in tool_calls:
                    if isinstance(call, dict):
                        tool_call_ids.add(str(call.get("id") or ""))
    return entries


def _project_one_chat_message(
    event: dict[str, Any],
    *,
    index: int,
    tool_call_ids: set[str],
    resolved_effects: set[str],
) -> dict[str, Any] | None:
    message = _chat_message_from_role_or_event(event)
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


def _chat_message_from_role_or_event(event: dict[str, Any]) -> dict[str, Any] | None:
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


def complete_tool_exchange_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    answered_call_ids = {
        str(message.get("tool_call_id") or "")
        for message in messages
        if message.get("role") == "tool"
    }
    return [
        message
        for message in messages
        if not has_unanswered_tool_call(message, answered_call_ids)
    ]


def has_unanswered_tool_call(
    message: dict[str, Any],
    answered_call_ids: set[str],
) -> bool:
    if message.get("role") != "assistant":
        return False
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return False
    call_ids = [
        str(call.get("id") or "")
        for call in tool_calls
        if isinstance(call, dict) and call.get("id")
    ]
    return any(call_id not in answered_call_ids for call_id in call_ids)


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
) -> list[PromptComponent]:
    """Return prompt components in stable prefix-cache-friendly order.

    Public ordering contract: system_prompt, tool descriptors, project context,
    then volatile timeline/objective/current-turn components.
    """
    enabled_capabilities = enabled_capability_ids(allowed_capabilities)
    system_content = system_prompt(
        system,
        allowed_capabilities=enabled_capabilities,
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
            )
        )
    components.extend(
        project_timeline_message_components(
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
                "expanded_objective": objective,
                "context": context,
                "message": {"role": "user", "content": objective_message},
            },
            message={"role": "user", "content": objective_message},
        )
    )
    components.extend(
        project_timeline_message_components(
            list(current_events),
            historical=False,
        )
    )
    return components


def project_timeline_message_components(
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
        kind = "user_message"
        if entry.message.get("role") == "tool":
            kind = "tool_result"
        elif entry.message.get("role") == "assistant":
            kind = "assistant_message"
        components.append(
            PromptComponent(
                kind=kind,
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
        tool_calls = entry.message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            call_id = str(call.get("id") or "")
            function = call.get("function")
            name = function.get("name") if isinstance(function, dict) else None
            if call_id and isinstance(name, str) and name:
                tool_call_names[call_id] = name
    return components


def timeline_message_component_data(
    message_index: int,
    entry: ChatMessageEntry,
    *,
    tool_name: str = "",
    historical: bool = False,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "index": message_index,
        "event_index": entry.event_index,
        "message": entry.message,
        "source_event_type": str(entry.event.get("type") or ""),
        "source_event_role": str(entry.event.get("role") or ""),
    }
    if historical:
        data["historical"] = True
    data.update(timeline_message_source_fields(entry.event, tool_name=tool_name))
    return data


def timeline_message_source_fields(
    event: dict[str, Any],
    *,
    tool_name: str = "",
) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if tool_name:
        data["source_tool_name"] = tool_name
    event_type = str(event.get("type") or "")
    if event_type == "tool_result":
        data["source_tool_call_id"] = str(event.get("tool_call_id") or "")
        result = event.get("result")
        if isinstance(result, dict):
            data["source_tool_result"] = result
        data.update(
            {
                f"source_{field_name}": object_id
                for field_name in ("tool_result_object_id", "tool_call_object_id")
                if (object_id := event.get(field_name)) is not None
            }
        )
    elif event_type == "tool_call":
        data["source_tool_call_id"] = str(
            event.get("tool_call_id") or event.get("id") or ""
        )
        data["source_tool_name"] = str(event.get("name") or "")
        tool_input = event.get("input")
        data["source_tool_input"] = tool_input if isinstance(tool_input, dict) else {}
        object_id = event.get("tool_call_object_id")
        if object_id is not None:
            data["source_tool_call_object_id"] = object_id
    elif event_type == "model" and isinstance(event.get("tool_calls"), list):
        data["source_model_tool_calls"] = event.get("tool_calls") or []
        prompt_trace = event.get("prompt_trace")
        if isinstance(prompt_trace, dict):
            object_id = prompt_trace.get("assistant_message_object_id")
            if object_id is not None:
                data["source_assistant_message_object_id"] = object_id
    return data


def timeline_message_component_links(event: dict[str, Any]) -> tuple[ObjectId, ...]:
    links: list[ObjectId] = []
    prompt_trace = event.get("prompt_trace")
    if isinstance(prompt_trace, dict):
        object_id = prompt_trace.get("assistant_message_object_id")
        if object_id:
            links.append(object_id)
    for trace_field in ("tool_result_object_id", "tool_call_object_id"):
        object_id = event.get(trace_field)
        if object_id and object_id not in links:
            links.append(object_id)
    return tuple(links)


def non_message_components(
    objective: str,
    *,
    context: str,
    tools: list[dict[str, Any]] | None,
    enabled_capabilities: tuple[str, ...],
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


def component_messages(components: list[PromptComponent]) -> list[dict[str, Any]]:
    return [
        message
        for component in components
        if (message := component.message) is not None
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
