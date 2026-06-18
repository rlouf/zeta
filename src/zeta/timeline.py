"""Runtime timeline projection over durable events."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from zeta.events import Event, EventSink
from zeta.store.events import EventReader, Filter, SqliteEventStore
from zeta.store.substrate import Store, warn_trace_failure_once

if TYPE_CHECKING:
    from zeta.session import Session


def current_timeline(*, runtime_context: Session) -> list[dict[str, Any]]:
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
    return event.timestamp_micros / 1_000_000


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
