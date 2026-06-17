"""Trace-backed run timeline projection and chat-message conversion for Zeta."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .context import ZetaContext
from .events import (
    DraftEvent,
    Event,
    EventReader,
    EventSink,
    Filter,
    SqliteEventStore,
    durable_event_for_type,
    publish_event,
    time_from_timestamp_micros,
    timestamp_micros_from_time,
)
from .tools.base import effect_resolution, proposed_effect
from .trace import (
    ObjectId,
    Store,
    warn_trace_failure_once,
)


@dataclass(frozen=True)
class ChatMessageEntry:
    """A rendered chat message plus the timeline event that produced it."""

    event_index: int
    event: dict[str, Any]
    message: dict[str, Any]


def record_event(
    event: dict[str, Any],
    *,
    runtime_context: ZetaContext,
) -> dict[str, Any]:
    """Record a Zeta event in the durable event log."""
    scoped_event = dict(event)
    if "session" not in scoped_event:
        scoped_event["session"] = runtime_context.session_id
    payload = event_payload(scoped_event)
    record_durable_event(
        payload,
        event_sink=runtime_context.event_sink,
        session_id=runtime_context.session_id,
    )
    return payload


def record_durable_event(
    event: dict[str, Any],
    *,
    event_sink: EventSink | None = None,
    session_id: str | None = None,
) -> None:
    event_type = str(event.get("type") or "event")
    payload = durable_event_payload(event)
    draft = durable_event_from_timeline(
        event_type,
        payload=payload,
        turn_id=optional_event_str(event.get("turn_id")),
        session_id=str(event.get("session") or session_id or timeline_session_id()),
        caused_by=optional_event_str(event.get("caused_by")),
        event_id=durable_event_id(event_type, event),
        timestamp_micros=timestamp_micros_from_time(event.get("time")),
    )
    if draft is None:
        return
    if event_sink is None:
        return
    try:
        publish_event(draft, sink=event_sink)
    except Exception as exc:
        warn_trace_failure_once("record_durable_event", exc)


def durable_event_from_timeline(
    event_type: str,
    *,
    payload: dict[str, Any],
    turn_id: str | None,
    session_id: str,
    caused_by: str | None,
    event_id: str | None,
    timestamp_micros: int | None,
) -> DraftEvent | None:
    durable_type = durable_type_from_timeline_type(event_type)
    if durable_type:
        return durable_event_for_type(
            durable_type,
            payload=payload,
            turn_id=turn_id,
            session_id=session_id,
            caused_by=caused_by,
            event_id=event_id,
            timestamp_micros=timestamp_micros,
        )
    return None


def durable_type_from_timeline_type(event_type: str) -> str:
    return {
        "model": "zeta.model.called",
        "tool_call": "zeta.tool.called",
        "tool_result": "zeta.tool.called",
        "user_message": "zeta.user_message",
        "turn_aborted": "zeta.turn_aborted",
        "model_usage": "zeta.model_usage",
    }.get(event_type, "")


def timeline_session_id() -> str:
    return os.environ.get("ZETA_SESSION_ID") or ""


def durable_event_payload(event: dict[str, Any]) -> dict[str, Any]:
    event_type = str(event.get("type") or "event")
    payload = {
        key: value
        for key, value in event.items()
        if key
        not in {
            "id",
            "type",
            "time",
            "session",
            "source",
            "caused_by",
            "prompt_trace",
            "tool_call_object_id",
            "tool_call_object_ids",
            "tool_result_object_id",
        }
    }
    payload["_timeline_type"] = event_type
    if isinstance(event.get("time"), int | float) and not isinstance(
        event.get("time"), bool
    ):
        payload["_time"] = float(event["time"])
    used_objects, returned_objects = durable_event_object_links(event)
    if used_objects:
        payload["used_objects"] = used_objects
    if returned_objects:
        payload["returned_objects"] = returned_objects
    return payload


def durable_event_object_links(
    event: dict[str, Any],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    event_type = str(event.get("type") or "")
    used_objects: list[dict[str, str]] = []
    returned_objects: list[dict[str, str]] = []
    if event_type == "model":
        prompt_trace = event.get("prompt_trace")
        if isinstance(prompt_trace, dict):
            add_object_link(
                used_objects,
                "prompt",
                trace_object_id(prompt_trace, "prompt_object_id"),
            )
            add_object_link(
                returned_objects,
                "assistant_message",
                trace_object_id(prompt_trace, "assistant_message_object_id"),
            )
        add_object_links(
            returned_objects,
            "tool_call",
            event.get("tool_call_object_ids"),
        )
        add_object_link(
            returned_objects,
            "tool_call",
            trace_object_id(event, "tool_call_object_id"),
        )
    if event_type == "tool_result":
        add_object_link(
            used_objects,
            "tool_call",
            trace_object_id(event, "tool_call_object_id"),
        )
        add_object_link(
            returned_objects,
            "tool_result",
            trace_object_id(event, "tool_result_object_id"),
        )
    return used_objects, returned_objects


def add_object_links(
    links: list[dict[str, str]],
    kind: str,
    object_ids: Any,
) -> None:
    if not isinstance(object_ids, (list, tuple)):
        return
    for object_id in object_ids:
        add_object_link(links, kind, object_id if isinstance(object_id, str) else None)


def add_object_link(
    links: list[dict[str, str]],
    kind: str,
    object_id: str | None,
) -> None:
    if not object_id:
        return
    link = {"kind": kind, "id": object_id}
    if link not in links:
        links.append(link)


def durable_event_id(event_type: str, event: dict[str, Any]) -> str | None:
    event_id = event.get("id")
    return event_id if isinstance(event_id, str) and event_id else None


def optional_event_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def current_timeline(*, runtime_context: ZetaContext) -> list[dict[str, Any]]:
    try:
        return timeline_from_event_reader(
            event_reader(runtime_context.event_sink),
            session_id=runtime_context.session_id,
        )
    except Exception as exc:
        warn_trace_failure_once("current_timeline", exc)
        return []


def last_event_time(*, store: Store, run_id: str | None = None) -> float | None:
    """Return the time of the most recently recorded event, if any."""
    try:
        reader = event_reader_from_trace_store(store)
        if reader is not None:
            event_time = latest_zeta_event_time(reader, session_id=run_id)
            if event_time is not None:
                return event_time
        return None
    except Exception as exc:
        warn_trace_failure_once("last_event_time", exc)
        return None


def latest_zeta_event_time(
    reader: EventReader,
    *,
    session_id: str | None,
) -> float | None:
    events = reader.list_events(Filter(session_id=session_id))
    zeta_events = [event for event in events if event.event_type.startswith("zeta.")]
    if not zeta_events:
        return None
    return exact_event_time(zeta_events[-1])


def exact_event_time(event: Event) -> float:
    exact_time = event.payload.get("_time")
    if isinstance(exact_time, int | float) and not isinstance(exact_time, bool):
        return float(exact_time)
    return time_from_timestamp_micros(event.timestamp_micros)


def event_reader(sink: EventSink) -> EventReader | None:
    if isinstance(sink, EventReader):
        return sink
    return None


def event_reader_from_trace_store(store: Store) -> EventReader | None:
    path = getattr(store, "path", None)
    if path is None:
        return None
    return SqliteEventStore(path)


def timeline_from_event_reader(
    reader: EventReader | None,
    *,
    session_id: str,
) -> list[dict[str, Any]]:
    if reader is None:
        return []
    return timeline_from_events(
        reader.list_events(
            Filter(session_id=session_id, event_type_prefix="zeta."),
        )
    )


def timeline_from_events(events: list[Event]) -> list[dict[str, Any]]:
    timeline = []
    for event in events:
        projected = timeline_event_from_durable_event(event)
        if projected:
            timeline.append(projected)
    return timeline


def timeline_event_from_durable_event(event: Event) -> dict[str, Any]:
    timeline_type = durable_timeline_type(event)
    if not timeline_type:
        return {}
    payload = {
        key: value
        for key, value in event.payload.items()
        if key not in {"_timeline_type", "_time"}
    }
    projected: dict[str, Any] = {
        "type": timeline_type,
        "id": event.id,
        "time": exact_event_time(event),
    }
    if event.session_id is not None:
        projected["session"] = event.session_id
    if event.turn_id is not None:
        projected["turn_id"] = event.turn_id
    if event.caused_by is not None:
        projected["caused_by"] = event.caused_by
    projected.update(payload)
    add_durable_object_refs(projected)
    return projected


def durable_timeline_type(event: Event) -> str:
    timeline_type = event.payload.get("_timeline_type")
    if isinstance(timeline_type, str) and timeline_type:
        return timeline_type
    if event.event_type == "zeta.model.called":
        return "model"
    if event.event_type == "zeta.tool.called":
        return "tool_result" if "result" in event.payload else "tool_call"
    prefix = "zeta."
    if event.event_type.startswith(prefix):
        return event.event_type[len(prefix) :]
    return ""


def add_durable_object_refs(event: dict[str, Any]) -> None:
    for link in event.get("used_objects") or []:
        add_durable_object_ref(event, link, returned=False)
    for link in event.get("returned_objects") or []:
        add_durable_object_ref(event, link, returned=True)


def add_durable_object_ref(
    event: dict[str, Any],
    link: Any,
    *,
    returned: bool,
) -> None:
    ref = durable_object_ref(link)
    if ref is None:
        return
    kind, object_id = ref
    if kind == "tool_call":
        event.setdefault("tool_call_object_id", object_id)
        return
    durable_ref_handlers(returned).get(kind, ignore_durable_ref)(event, object_id)


def durable_object_ref(link: Any) -> tuple[str, str] | None:
    if not isinstance(link, dict):
        return None
    kind = link.get("kind")
    object_id = link.get("id")
    if isinstance(kind, str) and isinstance(object_id, str):
        return kind, object_id
    return None


def durable_ref_handlers(
    returned: bool,
) -> dict[str, Any]:
    if returned:
        return {
            "tool_result": set_tool_result_ref,
            "assistant_message": set_assistant_message_ref,
        }
    return {"prompt": set_prompt_ref}


def ignore_durable_ref(event: dict[str, Any], object_id: str) -> None:
    del event, object_id


def set_tool_result_ref(event: dict[str, Any], object_id: str) -> None:
    event.setdefault("tool_result_object_id", object_id)


def set_prompt_ref(event: dict[str, Any], object_id: str) -> None:
    durable_prompt_trace(event).setdefault("prompt_object_id", object_id)


def set_assistant_message_ref(event: dict[str, Any], object_id: str) -> None:
    durable_prompt_trace(event).setdefault(
        "assistant_message_object_id",
        object_id,
    )


def durable_prompt_trace(event: dict[str, Any]) -> dict[str, Any]:
    prompt_trace = event.setdefault("prompt_trace", {})
    if isinstance(prompt_trace, dict):
        return prompt_trace
    prompt_trace = {}
    event["prompt_trace"] = prompt_trace
    return prompt_trace


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


def event_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = dict(event)
    payload["id"] = str(payload.get("id") or uuid.uuid4())
    payload["time"] = event_time_value(payload.get("time"))
    payload["cwd"] = str(payload.get("cwd") or os.getcwd())
    payload["session"] = str(payload.get("session") or timeline_session_id())
    return payload


def event_time_value(value: Any) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return time.time()


def trace_object_id(event: dict[str, Any], field: str) -> ObjectId | None:
    value = event.get(field)
    if isinstance(value, str) and value.startswith("sha256:"):
        return value
    return None


def add_event_link(links: list[ObjectId], object_id: ObjectId | None) -> None:
    if object_id and object_id not in links:
        links.append(object_id)


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
