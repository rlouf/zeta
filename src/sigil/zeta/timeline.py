"""Trace-backed run timeline projection and chat-message conversion for Zeta."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any

from ..protocols import is_shell_handoff_result, is_shell_prompt_handoff
from ..state import session_id
from .trace import (
    Derivation,
    Object,
    ObjectId,
    Store,
    default_store,
    warn_trace_failure_once,
)

RUN_EVENT_KIND = "run_event"
RUN_HEAD_EVENT_TYPES = {"assistant_message", "tool_call", "tool_result"}
NON_HEAD_EVENT_TYPES = {"model_usage"}


@dataclass(frozen=True)
class ChatMessageEntry:
    """A rendered chat message plus the timeline event that produced it."""

    event_index: int
    event: dict[str, Any]
    message: dict[str, Any]


def record_event(event: dict[str, Any]) -> dict[str, Any]:
    """Record a Zeta event in the trace store and advance the run head."""
    payload = event_payload(event)
    try:
        store = default_store()
        with store.batch():
            previous_event_id = store.get_ref(event_head_ref())
            links = event_links(payload, previous_event_id)
            event_id = store.put_object(
                Object(
                    kind=RUN_EVENT_KIND,
                    schema="zeta.run_event.v1",
                    data={
                        "event": stored_event_payload(payload),
                        "previous_event_object_id": previous_event_id or "",
                    },
                    links=links,
                )
            )
            store.record_derivation(
                Derivation(
                    producer="RunEvent",
                    output_id=event_id,
                    input_ids=links,
                    params={"type": str(payload.get("type") or "")},
                )
            )
            store.set_ref(event_head_ref(), event_id)
            head_id = event_domain_object_id(payload) or event_id
            if should_update_run_head(payload):
                store.set_ref(run_head_ref(), head_id)
            elif store.get_ref(run_head_ref()) is None:
                store.set_ref(run_head_ref(), head_id)
    except Exception as exc:
        warn_trace_failure_once("record_event", exc)
    return payload


def current_timeline() -> list[dict[str, Any]]:
    try:
        store = default_store()
        events = timeline_from_ref(run_head_ref(), store=store)
        if not events:
            events = timeline_from_ref(event_head_ref(), store=store)
    except Exception as exc:
        warn_trace_failure_once("current_timeline", exc)
        return []
    return events


def last_event_time() -> float | None:
    """Return the time of the most recently recorded event, if any."""
    try:
        store = default_store()
        event_id = store.get_ref(event_head_ref())
        if event_id is None:
            return None
        obj = store.get_object(event_id)
        if obj is None:
            return None
        value = object_event(obj).get("time")
        return float(value) if isinstance(value, int | float) else None
    except Exception as exc:
        warn_trace_failure_once("last_event_time", exc)
        return None


def timeline_from_ref(
    ref_name: str,
    *,
    store: Store | None = None,
) -> list[dict[str, Any]]:
    """Project a timeline from the object named by a trace ref."""
    try:
        active_store = store or default_store()
        object_id = active_store.get_ref(ref_name)
        if object_id is None:
            return []
        return timeline_from_object(object_id, store=active_store)
    except Exception as exc:
        warn_trace_failure_once("timeline_from_ref", exc)
        return []


def timeline_from_object(
    object_id: ObjectId,
    *,
    store: Store | None = None,
) -> list[dict[str, Any]]:
    """Project the full timeline by walking backward from a trace object.

    The projection is unbounded; model-facing truncation lives in the
    prompt layer next to `from_message_boundary`.
    """
    try:
        return timeline_events_from_head(
            store or default_store(),
            object_id,
            seen=set(),
        )
    except Exception as exc:
        warn_trace_failure_once("timeline_from_object", exc)
        return []


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


def run_head_ref(run_id: str | None = None) -> str:
    """Return the mutable ref naming the current trace leaf for a run."""
    return f"run/{run_id or session_id()}/head"


def event_head_ref(run_id: str | None = None) -> str:
    """Return the event-chain fallback ref for a run."""
    return f"run/{run_id or session_id()}/event_head"


def set_run_head(object_id: ObjectId, *, store: Store | None = None) -> None:
    """Move the current run head to a trace object."""
    (store or default_store()).set_ref(run_head_ref(), object_id)


def run_head(*, store: Store | None = None) -> ObjectId | None:
    """Return the current run head object id, if any."""
    return (store or default_store()).get_ref(run_head_ref())


def event_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = dict(event)
    payload["id"] = str(payload.get("id") or uuid.uuid4())
    payload["time"] = event_time_value(payload.get("time"))
    payload["cwd"] = str(payload.get("cwd") or os.getcwd())
    payload["session"] = str(payload.get("session") or session_id())
    return payload


def event_time_value(value: Any) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return time.time()


def event_links(
    event: dict[str, Any],
    previous_event_id: ObjectId | None,
) -> tuple[ObjectId, ...]:
    """Link a run event to its predecessor and its prompt-trace objects.

    Components stay reachable through the prompt object's own links;
    linking them here again grew every event by the whole component set.
    """
    links: list[ObjectId] = []
    if previous_event_id:
        links.append(previous_event_id)
    add_event_link(links, event_domain_object_id(event))
    prompt_trace = event.get("prompt_trace")
    if isinstance(prompt_trace, dict):
        add_event_link(links, trace_object_id(prompt_trace, "prompt_object_id"))
        add_event_link(
            links,
            trace_object_id(prompt_trace, "assistant_message_object_id"),
        )
    return tuple(links)


def stored_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the event as persisted: linked graph content is not inlined.

    The component ids and the assistant message body already live in the
    prompt and assistant_message objects; storing them again in every run
    event duplicated the heaviest content once per turn. Projection
    rehydrates the assistant body from the link.
    """
    prompt_trace = payload.get("prompt_trace")
    if not isinstance(prompt_trace, dict):
        return payload
    stored = dict(payload)
    stored["prompt_trace"] = {
        key: value
        for key, value in prompt_trace.items()
        if key != "component_object_ids"
    }
    if str(stored.get("type") or "") == "assistant_message" and trace_object_id(
        stored["prompt_trace"], "assistant_message_object_id"
    ):
        stored.pop("content", None)
        stored.pop("tool_calls", None)
        stored.pop("reasoning", None)
    return stored


def event_domain_object_id(event: dict[str, Any]) -> ObjectId | None:
    event_type = str(event.get("type") or "")
    if event_type == "tool_result":
        return trace_object_id(event, "tool_result_object_id")
    if event_type == "tool_call":
        return trace_object_id(event, "tool_call_object_id")
    prompt_trace = event.get("prompt_trace")
    if event_type == "assistant_message" and isinstance(prompt_trace, dict):
        return trace_object_id(prompt_trace, "assistant_message_object_id")
    return None


def should_update_run_head(event: dict[str, Any]) -> bool:
    event_type = str(event.get("type") or "")
    if event_type in NON_HEAD_EVENT_TYPES:
        return False
    if event_type in RUN_HEAD_EVENT_TYPES:
        return True
    return event_domain_object_id(event) is None


def add_event_link(links: list[ObjectId], object_id: ObjectId | None) -> None:
    if object_id and object_id not in links:
        links.append(object_id)


def trace_object_id(event: dict[str, Any], field: str) -> ObjectId | None:
    value = event.get(field)
    if isinstance(value, str) and value.startswith("sha256:"):
        return value
    return None


def timeline_events_from_head(
    store: Store,
    object_id: ObjectId,
    *,
    seen: set[ObjectId],
) -> list[dict[str, Any]]:
    """Walk predecessor links iteratively; chains exceed the recursion limit."""
    chunks: list[list[dict[str, Any]]] = []
    current: ObjectId | None = object_id
    while current and current not in seen:
        seen.add(current)
        obj = store.get_object(current)
        if obj is None:
            break
        predecessor, events = timeline_node(store, current, obj)
        chunks.append(events)
        current = predecessor
    events = []
    for chunk in reversed(chunks):
        events.extend(chunk)
    return events


def timeline_node(
    store: Store,
    object_id: ObjectId,
    obj: Object,
) -> tuple[ObjectId | None, list[dict[str, Any]]]:
    """Return an object's predecessor link and its own timeline events."""
    if obj.kind == RUN_EVENT_KIND:
        previous_id = str(obj.data.get("previous_event_object_id") or "")
        event = object_event(obj)
        if event:
            event = rehydrated_assistant_event(store, event)
        return previous_id or None, [event] if event else []
    if obj.kind == "assistant_message":
        prompt_id = obj.links[0] if obj.links else ""
        events = prompt_component_events(store, prompt_id) if prompt_id else []
        assistant_event = assistant_event_from_object(object_id, obj, prompt_id)
        if assistant_event:
            events.append(assistant_event)
        return None, events
    if obj.kind == "tool_call":
        assistant_id = obj.links[0] if obj.links else ""
        return assistant_id or None, [tool_call_event_from_object(object_id, obj)]
    if obj.kind == "tool_result":
        previous_id = obj.links[0] if obj.links else ""
        event = object_event(obj) or tool_result_event_from_object(object_id, obj)
        return previous_id or None, [event]
    event = object_event(obj)
    return None, [event] if event else []


def object_event(obj: Object) -> dict[str, Any]:
    event = obj.data.get("event")
    return dict(event) if isinstance(event, dict) else {}


def rehydrated_assistant_event(
    store: Store,
    event: dict[str, Any],
) -> dict[str, Any]:
    """Merge a linked assistant message body back into a projected event."""
    if str(event.get("type") or "") != "assistant_message" or "content" in event:
        return event
    prompt_trace = event.get("prompt_trace")
    if not isinstance(prompt_trace, dict):
        return event
    assistant_id = trace_object_id(prompt_trace, "assistant_message_object_id")
    if assistant_id is None:
        return event
    assistant = store.get_object(assistant_id)
    if assistant is None:
        return event
    message = assistant.data.get("message")
    if not isinstance(message, dict):
        return event
    rehydrated = dict(event)
    content = message.get("content")
    if isinstance(content, str) and content:
        rehydrated["content"] = content
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        rehydrated["tool_calls"] = tool_calls
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        rehydrated["reasoning"] = reasoning
    return rehydrated


def prompt_component_events(store: Store, prompt_id: ObjectId) -> list[dict[str, Any]]:
    prompt = store.get_object(prompt_id)
    if prompt is None or prompt.kind != "prompt":
        return []
    events = []
    for component_id in prompt.links:
        component = store.get_object(component_id)
        if component is None:
            continue
        event = prompt_component_event(component_id, component)
        if event:
            events.append(event)
    return events


def prompt_component_event(
    component_id: ObjectId,
    component: Object,
) -> dict[str, Any]:
    if component.kind in {
        "system_prompt",
        "tool_descriptor_set",
        "skill_context",
        "project_context",
    }:
        return {}
    source_event = component.data.get("source_event")
    if isinstance(source_event, dict):
        event = dict(source_event)
        normalize_source_event(event)
        return event
    message = component.data.get("message")
    if not isinstance(message, dict):
        return {}
    event = chat_message_event(message)
    if component.kind == "user_message":
        event["type"] = "user_message"
    source_object_id = component.data.get("source_object_id")
    if isinstance(source_object_id, str) and source_object_id.startswith("sha256:"):
        event["source_object_id"] = source_object_id
    if component_id.startswith("sha256:"):
        event["prompt_component_object_id"] = component_id
    return event


def normalize_source_event(event: dict[str, Any]) -> None:
    tool_name = event.pop("tool_name", None)
    if tool_name and not event.get("name"):
        event["name"] = tool_name


def chat_message_event(message: dict[str, Any]) -> dict[str, Any]:
    role = str(message.get("role") or "")
    if role == "assistant":
        event: dict[str, Any] = {"type": "assistant_message"}
        content = message.get("content")
        if isinstance(content, str) and content:
            event["content"] = content
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            event["tool_calls"] = tool_calls
        reasoning = message.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            event["reasoning"] = reasoning
        return event
    if role == "tool":
        return tool_result_event_from_message(message)
    return {"role": role, "content": str(message.get("content") or "")}


def assistant_event_from_object(
    object_id: ObjectId,
    obj: Object,
    prompt_id: ObjectId,
) -> dict[str, Any]:
    message = obj.data.get("message")
    if not isinstance(message, dict):
        return {}
    event = chat_message_event({"role": "assistant", **message})
    event["type"] = "assistant_message"
    if prompt_id:
        event["prompt_trace"] = {
            "prompt_object_id": prompt_id,
            "assistant_message_object_id": object_id,
        }
    return event


def tool_call_event_from_object(object_id: ObjectId, obj: Object) -> dict[str, Any]:
    data = obj.data
    event = {
        "type": "tool_call",
        "id": str(data.get("tool_call_id") or ""),
        "tool_call_id": str(data.get("tool_call_id") or ""),
        "name": str(data.get("name") or ""),
        "input": data.get("input") if isinstance(data.get("input"), dict) else {},
        "tool_call_object_id": object_id,
    }
    arguments = data.get("arguments")
    if isinstance(arguments, str):
        event["arguments"] = arguments
    return event


def tool_result_event_from_object(object_id: ObjectId, obj: Object) -> dict[str, Any]:
    data = obj.data
    event: dict[str, Any] = {
        "type": "tool_result",
        "tool_call_id": str(data.get("tool_call_id") or ""),
        "name": str(data.get("name") or ""),
        "tool_result_object_id": object_id,
    }
    if obj.links:
        event["tool_call_object_id"] = obj.links[0]
    result = data.get("result")
    if isinstance(result, dict):
        event["result"] = result
    model_telemetry = data.get("model_telemetry")
    if isinstance(model_telemetry, dict):
        event["model_telemetry"] = model_telemetry
    return event


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
    resolved_shell_handoffs = resolved_shell_handoff_call_ids(timeline)
    for index, event in enumerate(timeline):
        message = role_chat_message(event)
        if message is not None:
            entries.append(ChatMessageEntry(index, event, message))
            continue
        event_type = str(event.get("type") or "")
        message = event_chat_message(event_type, event)
        if message is not None:
            entries.append(ChatMessageEntry(index, event, message))
            record_tool_call_ids(message, tool_call_ids)
            continue
        if event_type == "tool_call":
            tool_call_id = str(event.get("id") or event.get("tool_call_id") or "")
            if tool_call_id and tool_call_id in tool_call_ids:
                continue
            message = tool_call_message(event, fallback_id=f"call-{index}")
            entries.append(ChatMessageEntry(index, event, message))
            record_tool_call_ids(message, tool_call_ids)
            continue
        if event_type == "tool_result":
            if is_resolved_shell_prompt_handoff(event, resolved_shell_handoffs):
                continue
            entries.append(
                ChatMessageEntry(
                    index, event, tool_result_message(event, tool_call_ids)
                )
            )
    return entries


def resolved_shell_handoff_call_ids(timeline: list[dict[str, Any]]) -> set[str]:
    """Return tool call ids that have a real shell handoff outcome."""
    resolved: set[str] = set()
    for event in timeline:
        if str(event.get("type") or "") != "tool_result":
            continue
        result = event.get("result")
        if not is_shell_handoff_result(result):
            continue
        tool_call_id = str(event.get("tool_call_id") or "")
        if tool_call_id:
            resolved.add(tool_call_id)
    return resolved


def is_resolved_shell_prompt_handoff(
    event: dict[str, Any],
    resolved_shell_handoffs: set[str],
) -> bool:
    """Return whether this staging handoff was superseded by shell output."""
    tool_call_id = str(event.get("tool_call_id") or "")
    if not tool_call_id or tool_call_id not in resolved_shell_handoffs:
        return False
    result = event.get("result")
    if not isinstance(result, dict):
        return False
    return is_shell_prompt_handoff(result.get("handoff"))


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
        "turn_aborted": "assistant",
    }
    role = role_by_type.get(event_type)
    if role is None:
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

    A recorded tool call can carry truncated arguments (a generation cut by
    max_tokens); chat templates refuse to render them, which would fail every
    later prompt in the session.
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


# Bookkeeping the model has no use for: trace linkage and telemetry.
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
