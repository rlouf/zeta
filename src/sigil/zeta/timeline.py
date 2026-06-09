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

DEFAULT_TAIL_LIMIT = 50
RUN_EVENT_KIND = "run_event"
RUN_HEAD_EVENT_TYPES = {"assistant_message", "tool_call", "tool_result"}
NON_HEAD_EVENT_TYPES = {"model_usage", "tool_analysis"}


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
        previous_event_id = store.get_ref(event_head_ref())
        links = event_links(payload, previous_event_id)
        event_id = store.put_object(
            Object(
                kind=RUN_EVENT_KIND,
                schema="zeta.run_event.v1",
                data={
                    "event": payload,
                    "previous_event_object_id": previous_event_id or "",
                },
                links=links,
            )
        )
        store.record_derivation(
            Derivation(
                producer="SigilRunEvent:v1",
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


def current_timeline(limit: int = DEFAULT_TAIL_LIMIT) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    try:
        store = default_store()
        events = timeline_from_ref(run_head_ref(), store=store, limit=limit)
        if not events:
            events = timeline_from_ref(
                event_head_ref(),
                store=store,
                limit=limit,
            )
    except Exception as exc:
        warn_trace_failure_once("current_timeline", exc)
        return []
    return events


def timeline_from_ref(
    ref_name: str,
    *,
    store: Store | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Project a timeline from the object named by a trace ref."""
    if limit is not None and limit <= 0:
        return []
    try:
        active_store = store or default_store()
        object_id = active_store.get_ref(ref_name)
        if object_id is None:
            return []
        return timeline_from_object(object_id, store=active_store, limit=limit)
    except Exception as exc:
        warn_trace_failure_once("timeline_from_ref", exc)
        return []


def timeline_from_object(
    object_id: ObjectId,
    *,
    store: Store | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Project a timeline by walking backward from a trace object."""
    if limit is not None and limit <= 0:
        return []
    try:
        events = timeline_events_from_head(
            store or default_store(),
            object_id,
            seen=set(),
        )
    except Exception as exc:
        warn_trace_failure_once("timeline_from_object", exc)
        return []
    if limit is None:
        return events
    return events[-limit:]


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
    if not str(payload.get("id") or ""):
        payload["id"] = str(uuid.uuid4())
    return payload


def event_time_value(value: Any) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return time.time()


def event_links(
    event: dict[str, Any],
    previous_event_id: ObjectId | None,
) -> tuple[ObjectId, ...]:
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
        component_ids = prompt_trace.get("component_object_ids")
        if isinstance(component_ids, list):
            for component_id in component_ids:
                if isinstance(component_id, str):
                    add_event_link(links, component_id)
    return tuple(links)


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


def timeline_from_current_head(store: Store) -> list[dict[str, Any]]:
    head_id = store.get_ref(run_head_ref())
    if head_id is None:
        return []
    return timeline_events_from_head(store, head_id, seen=set())


def timeline_from_event_ref(store: Store) -> list[dict[str, Any]]:
    event_id = store.get_ref(event_head_ref())
    if event_id is None:
        return []
    return timeline_events_from_head(store, event_id, seen=set())


def timeline_events_from_head(
    store: Store,
    object_id: ObjectId,
    *,
    seen: set[ObjectId],
) -> list[dict[str, Any]]:
    if object_id in seen:
        return []
    seen.add(object_id)
    obj = store.get_object(object_id)
    if obj is None:
        return []
    if obj.kind == RUN_EVENT_KIND:
        return timeline_events_from_event_object(store, obj, seen=seen)
    if obj.kind == "assistant_message":
        return timeline_events_from_assistant_object(
            store,
            object_id,
            obj,
            seen=seen,
        )
    if obj.kind == "tool_call":
        return timeline_events_from_tool_call_object(
            store,
            object_id,
            obj,
            seen=seen,
        )
    if obj.kind == "tool_result":
        return timeline_events_from_tool_result_object(
            store,
            object_id,
            obj,
            seen=seen,
        )
    event = object_event(obj)
    return [event] if event else []


def timeline_events_from_event_object(
    store: Store,
    obj: Object,
    *,
    seen: set[ObjectId],
) -> list[dict[str, Any]]:
    previous_id = str(obj.data.get("previous_event_object_id") or "")
    events = (
        timeline_events_from_head(store, previous_id, seen=seen) if previous_id else []
    )
    event = object_event(obj)
    if event:
        events.append(event)
    return events


def timeline_events_from_assistant_object(
    store: Store,
    object_id: ObjectId,
    obj: Object,
    *,
    seen: set[ObjectId],
) -> list[dict[str, Any]]:
    prompt_id = obj.links[0] if obj.links else ""
    events = prompt_component_events(store, prompt_id) if prompt_id else []
    assistant_event = assistant_event_from_object(object_id, obj, prompt_id, store)
    if assistant_event:
        events.append(assistant_event)
    return events


def timeline_events_from_tool_call_object(
    store: Store,
    object_id: ObjectId,
    obj: Object,
    *,
    seen: set[ObjectId],
) -> list[dict[str, Any]]:
    assistant_id = obj.links[0] if obj.links else ""
    events = (
        timeline_events_from_head(store, assistant_id, seen=seen)
        if assistant_id
        else []
    )
    events.append(tool_call_event_from_object(object_id, obj))
    return events


def timeline_events_from_tool_result_object(
    store: Store,
    object_id: ObjectId,
    obj: Object,
    *,
    seen: set[ObjectId],
) -> list[dict[str, Any]]:
    event = object_event(obj)
    if event:
        previous_id = obj.links[0] if obj.links else ""
        events = (
            timeline_events_from_head(store, previous_id, seen=seen)
            if previous_id
            else []
        )
        events.append(event)
        return events
    call_id = obj.links[0] if obj.links else ""
    events = timeline_events_from_head(store, call_id, seen=seen) if call_id else []
    events.append(tool_result_event_from_object(object_id, obj))
    return events


def object_event(obj: Object) -> dict[str, Any]:
    event = obj.data.get("event")
    return dict(event) if isinstance(event, dict) else {}


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
    if component.kind == "user_objective":
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
    if not event.get("type"):
        event["type"] = "transcript_message"


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
        return event
    if role == "tool":
        return tool_result_event_from_message(message)
    if role in {"user", "system"}:
        return {"role": role, "content": str(message.get("content") or "")}
    return {"role": role, "content": str(message.get("content") or "")}


def assistant_event_from_object(
    object_id: ObjectId,
    obj: Object,
    prompt_id: ObjectId,
    store: Store,
) -> dict[str, Any]:
    message = obj.data.get("message")
    if not isinstance(message, dict):
        return {}
    event = chat_message_event({"role": "assistant", **message})
    event["type"] = "assistant_message"
    if prompt_id:
        prompt = store.get_object(prompt_id)
        event["prompt_trace"] = {
            "prompt_object_id": prompt_id,
            "assistant_message_object_id": object_id,
            "component_object_ids": list(prompt.links if prompt is not None else ()),
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
            entries.append(chat_message_entry(index, event, message))
            continue
        event_type = str(event.get("type") or "")
        message = event_chat_message(event_type, event)
        if message is not None:
            entries.append(chat_message_entry(index, event, message))
            record_tool_call_ids(message, tool_call_ids)
            continue
        if event_type == "tool_call":
            tool_call_id = str(event.get("id") or event.get("tool_call_id") or "")
            if tool_call_id and tool_call_id in tool_call_ids:
                continue
            message = tool_call_message(event, fallback_id=f"call-{index}")
            entries.append(chat_message_entry(index, event, message))
            record_tool_call_ids(message, tool_call_ids)
            continue
        if event_type == "tool_result":
            if is_resolved_shell_prompt_handoff(event, resolved_shell_handoffs):
                continue
            entries.append(
                chat_message_entry(
                    index,
                    event,
                    tool_result_message(event, tool_call_ids),
                )
            )
    return entries


def chat_message_entry(
    event_index: int,
    event: dict[str, Any],
    message: dict[str, Any],
) -> ChatMessageEntry:
    return ChatMessageEntry(
        event_index=event_index,
        event=event,
        message=message,
    )


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
            "tool_calls": tool_calls,
        }
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
