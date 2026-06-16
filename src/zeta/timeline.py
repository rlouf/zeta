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
    durable_event_draft,
    model_called_event,
    publish_event,
    time_from_timestamp_micros,
    timestamp_micros_from_time,
    tool_called_event,
)
from .tools.base import effect_resolution, proposed_effect
from .trace import (
    Derivation,
    Object,
    ObjectId,
    Store,
    warn_trace_failure_once,
)

RUN_EVENT_KIND = "run_event"
RUN_HEAD_EVENT_TYPES = {"model", "tool_call", "tool_result"}
NON_HEAD_EVENT_TYPES = {"model_usage"}


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
    """Record a Zeta event in the trace store and advance the run head."""
    scoped_event = dict(event)
    if "session" not in scoped_event:
        scoped_event["session"] = runtime_context.session_id
    payload = event_payload(scoped_event)
    record_durable_event(
        payload,
        event_sink=runtime_context.event_sink,
        session_id=runtime_context.session_id,
    )
    try:
        store = runtime_context.trace_store
        run_id = runtime_context.session_id
        with store.batch():
            previous_event_id = store.get_ref(event_head_ref(run_id))
            previous_run_head_id = store.get_ref(run_head_ref(run_id))
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
            store.set_ref(event_head_ref(run_id), event_id, expected=previous_event_id)
            head_id = event_domain_object_id(payload) or event_id
            if should_update_run_head(payload):
                store.set_ref(
                    run_head_ref(run_id),
                    head_id,
                    expected=previous_run_head_id,
                )
            elif previous_run_head_id is None:
                store.set_ref(run_head_ref(run_id), head_id, expected=None)
    except Exception as exc:
        warn_trace_failure_once("record_event", exc)
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
    if event_type == "model":
        return model_called_event(
            payload=payload,
            turn_id=turn_id,
            session_id=session_id,
            caused_by=caused_by,
            event_id=event_id,
            timestamp_micros=timestamp_micros,
        )
    if event_type == "tool_result":
        return tool_called_event(
            payload=payload,
            turn_id=turn_id,
            session_id=session_id,
            caused_by=caused_by,
            event_id=event_id,
            timestamp_micros=timestamp_micros,
        )
    if event_type == "tool_call":
        return tool_called_event(
            payload=payload,
            turn_id=turn_id,
            session_id=session_id,
            caused_by=caused_by,
            event_id=event_id,
            timestamp_micros=timestamp_micros,
        )
    if event_type in {"user_message", "turn_aborted", "model_usage"}:
        durable_type = f"zeta.{event_type}"
        return durable_event_draft(
            durable_type,
            "zeta",
            payload=payload,
            turn_id=turn_id,
            session_id=session_id,
            caused_by=caused_by,
            event_id=event_id,
            idempotency_key=(
                f"{durable_type}:{event_id}" if event_id is not None else None
            ),
            timestamp_micros=timestamp_micros,
        )
    return None


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
        events = timeline_from_event_reader(
            event_reader(runtime_context.event_sink),
            session_id=runtime_context.session_id,
        )
        if events:
            return events
        store = runtime_context.trace_store
        run_id = runtime_context.session_id
        events = timeline_from_ref(run_head_ref(run_id), store=store)
        if not events:
            events = timeline_from_ref(event_head_ref(run_id), store=store)
    except Exception as exc:
        warn_trace_failure_once("current_timeline", exc)
        return []
    return events


def last_event_time(*, store: Store, run_id: str | None = None) -> float | None:
    """Return the time of the most recently recorded event, if any."""
    try:
        reader = event_reader_from_trace_store(store)
        if reader is not None:
            event_time = latest_zeta_event_time(reader, session_id=run_id)
            if event_time is not None:
                return event_time
        event_id = store.get_ref(event_head_ref(run_id))
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


def timeline_from_ref(
    ref_name: str,
    *,
    store: Store,
) -> list[dict[str, Any]]:
    """Project a timeline from the object named by a trace ref."""
    try:
        object_id = store.get_ref(ref_name)
        if object_id is None:
            return []
        return timeline_from_object(object_id, store=store)
    except Exception as exc:
        warn_trace_failure_once("timeline_from_ref", exc)
        return []


def timeline_from_object(
    object_id: ObjectId,
    *,
    store: Store,
) -> list[dict[str, Any]]:
    """Project the full timeline by walking backward from a trace object.

    The projection is unbounded; model-facing truncation lives in the
    prompt layer next to `from_message_boundary`.
    """
    try:
        return timeline_events_from_head(
            store,
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
    return f"run/{run_id or timeline_session_id()}/head"


def event_head_ref(run_id: str | None = None) -> str:
    """Return the event-chain fallback ref for a run."""
    return f"run/{run_id or timeline_session_id()}/event_head"


def set_run_head(
    object_id: ObjectId, *, store: Store, run_id: str | None = None
) -> None:
    """Move the current run head to a trace object."""
    store.set_ref(run_head_ref(run_id), object_id)


def run_head(*, store: Store, run_id: str | None = None) -> ObjectId | None:
    """Return the current run head object id, if any."""
    return store.get_ref(run_head_ref(run_id))


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
    if str(stored.get("type") or "") == "model" and trace_object_id(
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
    if event_type == "model" and isinstance(prompt_trace, dict):
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
            event = rehydrated_model_event(store, event)
        return previous_id or None, [event] if event else []
    if obj.kind == "assistant_message":
        prompt_id = obj.links[0] if obj.links else ""
        events = prompt_component_events(store, prompt_id) if prompt_id else []
        event = model_event_from_object(object_id, obj, prompt_id)
        if event:
            events.append(event)
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


def rehydrated_model_event(
    store: Store,
    event: dict[str, Any],
) -> dict[str, Any]:
    """Merge a linked assistant message body back into a projected event."""
    if str(event.get("type") or "") != "model" or "content" in event:
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
        event: dict[str, Any] = {"type": "model"}
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


def model_event_from_object(
    object_id: ObjectId,
    obj: Object,
    prompt_id: ObjectId,
) -> dict[str, Any]:
    message = obj.data.get("message")
    if not isinstance(message, dict):
        return {}
    event = chat_message_event({"role": "assistant", **message})
    event["type"] = "model"
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
    resolved_effects = resolved_effect_call_ids(timeline)
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
            if is_resolved_proposed_effect(event, resolved_effects):
                continue
            entries.append(
                ChatMessageEntry(
                    index, event, tool_result_message(event, tool_call_ids)
                )
            )
    return entries


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
        "model": "assistant",
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
