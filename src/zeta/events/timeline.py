"""Timeline projections over durable events."""

from __future__ import annotations

import os
import time
import uuid
from typing import TYPE_CHECKING, Any

from ..substrate import Store, warn_trace_failure_once
from .event import Event, time_from_timestamp_micros
from .payloads import (
    durable_event_from_timeline,
    durable_event_id,
    durable_event_payload,
    optional_event_str,
)
from .sink import EventSink, publish_event
from .store import EventReader, Filter, SqliteEventStore

if TYPE_CHECKING:
    from ..session import Session


def record_event(
    event: dict[str, Any],
    *,
    runtime_context: Session,
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


def current_timeline(*, runtime_context: Session) -> list[dict[str, Any]]:
    try:
        return timeline_from_event_reader(
            event_reader(runtime_context.event_sink),
            session_id=runtime_context.session_id,
        )
    except Exception as exc:
        warn_trace_failure_once("current_timeline", exc)
        return []


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
        timestamp_micros=timestamp_micros_from_event_time(event.get("time")),
    )
    if draft is None:
        return
    if event_sink is None:
        return
    try:
        publish_event(draft, sink=event_sink)
    except Exception as exc:
        warn_trace_failure_once("record_durable_event", exc)


def timestamp_micros_from_event_time(value: object) -> int | None:
    from .event import timestamp_micros_from_time

    return timestamp_micros_from_time(value)


def timeline_session_id() -> str:
    return os.environ.get("ZETA_SESSION_ID") or ""


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
