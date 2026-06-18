"""Runtime timeline projection over durable events."""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from zeta.events import DraftEvent, Event, EventSink, publish_event
from zeta.store.events import EventReader, Filter, SqliteEventStore
from zeta.substrate.store import Store, warn_trace_failure_once

if TYPE_CHECKING:
    from zeta.session import Session

EVENT_IDEMPOTENT_TYPES = frozenset(
    {
        "zeta.model.called",
        "zeta.tool.called",
        "zeta.user_message",
        "zeta.turn_aborted",
        "zeta.model_usage",
    }
)
TURN_IDEMPOTENT_TYPES = frozenset(
    {
        "zeta.prompt.submitted",
        "zeta.turn.completed",
        "zeta.turn.failed",
        "zeta.turn.aborted",
    }
)
SUPPORTED_DURABLE_EVENT_TYPES = EVENT_IDEMPOTENT_TYPES | TURN_IDEMPOTENT_TYPES


@runtime_checkable
class EventAppender(Protocol):
    def append(self, event: Event) -> object: ...


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
        turn_id=optional_str(event.get("turn_id")),
        session_id=str(event.get("session") or session_id or timeline_session_id()),
        caused_by=optional_str(event.get("caused_by")),
        event_id=durable_event_id(event_type, event),
    )
    if draft is None:
        return
    if event_sink is None:
        return
    try:
        appender = event_sink if isinstance(event_sink, EventAppender) else None
        if appender is not None:
            appender.append(timeline_durable_event(event, draft))
        else:
            publish_event(draft, sink=event_sink)
    except Exception as exc:
        warn_trace_failure_once("record_durable_event", exc)


def timeline_durable_event(event: dict[str, Any], draft: DraftEvent) -> Event:
    return Event(
        id=str(event.get("id") or uuid.uuid4()),
        event_type=draft.event_type,
        source=draft.source,
        payload=draft.payload,
        idempotency_key=normalized_idempotency_key(draft.idempotency_key),
        caused_by=draft.caused_by,
        session_id=draft.session_id,
        turn_id=draft.turn_id,
        timestamp_micros=timestamp_micros_from_time(event.get("time")),
    )


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
    if event.event_type == "zeta.model.called":
        return "model"
    if event.event_type == "zeta.tool.called":
        return "tool_result" if "result" in event.payload else "tool_call"
    prefix = "zeta."
    if event.event_type.startswith(prefix):
        return event.event_type[len(prefix) :]
    return ""


def durable_event_draft(
    event_type: str,
    source: str,
    *,
    payload: dict[str, Any],
    turn_id: str | None,
    session_id: str,
    caused_by: str | None,
    idempotency_key: str | None,
) -> DraftEvent:
    return DraftEvent(
        event_type=event_type,
        source=source,
        payload=payload,
        idempotency_key=idempotency_key,
        caused_by=caused_by,
        session_id=session_id,
        turn_id=turn_id,
    )


def durable_event_from_timeline(
    event_type: str,
    *,
    payload: dict[str, Any],
    turn_id: str | None,
    session_id: str,
    caused_by: str | None,
    event_id: str | None,
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


def trace_object_id(event: dict[str, Any], field: str) -> str | None:
    value = event.get(field)
    if isinstance(value, str) and value.startswith("sha256:"):
        return value
    return None


def durable_event_id(event_type: str, event: dict[str, Any]) -> str | None:
    del event_type
    event_id = event.get("id")
    return event_id if isinstance(event_id, str) and event_id else None


def optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def timestamp_micros_from_time(value: object) -> int:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return int(float(value) * 1_000_000)
    return time.time_ns() // 1_000


def normalized_idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip() or None


def event_idempotency_key(event_type: str, event_id: str | None) -> str | None:
    if not event_id:
        return None
    return f"{event_type}:{event_id}"


def turn_idempotency_key(event_type: str, turn_id: str | None) -> str | None:
    if not turn_id:
        return None
    return f"{event_type}:{turn_id}"


def durable_event_for_type(
    event_type: str,
    *,
    payload: dict[str, Any],
    turn_id: str | None,
    session_id: str,
    caused_by: str | None = None,
    event_id: str | None = None,
) -> DraftEvent:
    return durable_event_draft(
        event_type,
        "zeta",
        payload=payload,
        turn_id=turn_id,
        session_id=session_id,
        caused_by=caused_by,
        idempotency_key=durable_event_idempotency_key(
            event_type,
            event_id=event_id,
            turn_id=turn_id,
        ),
    )


def durable_event_idempotency_key(
    event_type: str,
    *,
    event_id: str | None,
    turn_id: str | None,
) -> str | None:
    if event_type in EVENT_IDEMPOTENT_TYPES:
        return event_idempotency_key(event_type, event_id)
    if event_type in TURN_IDEMPOTENT_TYPES:
        return turn_idempotency_key(event_type, turn_id)
    return None


def model_called_event(
    *,
    payload: dict[str, Any],
    turn_id: str | None,
    session_id: str,
    caused_by: str | None = None,
    event_id: str | None = None,
) -> DraftEvent:
    return durable_event_for_type(
        "zeta.model.called",
        payload=payload,
        turn_id=turn_id,
        session_id=session_id,
        caused_by=caused_by,
        event_id=event_id,
    )


def tool_called_event(
    *,
    payload: dict[str, Any],
    turn_id: str | None,
    session_id: str,
    caused_by: str | None = None,
    event_id: str | None = None,
) -> DraftEvent:
    return durable_event_for_type(
        "zeta.tool.called",
        payload=payload,
        turn_id=turn_id,
        session_id=session_id,
        caused_by=caused_by,
        event_id=event_id,
    )


class DurableEventConstructors:
    """Factories for durable events with stable metadata."""

    def prompt_submitted(
        self,
        *,
        payload: dict[str, Any],
        turn_id: str | None,
        session_id: str,
        caused_by: str | None = None,
        event_id: str | None = None,
    ) -> DraftEvent:
        return durable_event_for_type(
            "zeta.prompt.submitted",
            payload=payload,
            turn_id=turn_id,
            session_id=session_id,
            caused_by=caused_by,
            event_id=event_id,
        )

    def turn_completed(
        self,
        *,
        payload: dict[str, Any],
        turn_id: str | None,
        session_id: str,
        caused_by: str | None = None,
        event_id: str | None = None,
    ) -> DraftEvent:
        return self._turn_event(
            "zeta.turn.completed",
            payload=payload,
            turn_id=turn_id,
            session_id=session_id,
            caused_by=caused_by,
            event_id=event_id,
        )

    def turn_failed(
        self,
        *,
        payload: dict[str, Any],
        turn_id: str | None,
        session_id: str,
        caused_by: str | None = None,
        event_id: str | None = None,
    ) -> DraftEvent:
        return self._turn_event(
            "zeta.turn.failed",
            payload=payload,
            turn_id=turn_id,
            session_id=session_id,
            caused_by=caused_by,
            event_id=event_id,
        )

    def turn_aborted(
        self,
        *,
        payload: dict[str, Any],
        turn_id: str | None,
        session_id: str,
        caused_by: str | None = None,
        event_id: str | None = None,
    ) -> DraftEvent:
        return self._turn_event(
            "zeta.turn.aborted",
            payload=payload,
            turn_id=turn_id,
            session_id=session_id,
            caused_by=caused_by,
            event_id=event_id,
        )

    def _turn_event(
        self,
        event_type: str,
        *,
        payload: dict[str, Any],
        turn_id: str | None,
        session_id: str,
        caused_by: str | None,
        event_id: str | None,
    ) -> DraftEvent:
        return durable_event_for_type(
            event_type,
            payload=payload,
            turn_id=turn_id,
            session_id=session_id,
            caused_by=caused_by,
            event_id=event_id,
        )


durable_event = DurableEventConstructors()


def durable_draft_from_payload(
    *,
    event_type: str,
    payload: dict[str, Any],
    turn_id: str | None,
    session_id: str,
    caused_by: str | None,
    event_id: str | None,
) -> DraftEvent | None:
    if event_type in SUPPORTED_DURABLE_EVENT_TYPES:
        return durable_event_for_type(
            event_type,
            payload=payload,
            turn_id=turn_id,
            session_id=session_id,
            caused_by=caused_by,
            event_id=event_id,
        )
    return None


def event_payload_draft(
    event: dict[str, Any],
    *,
    session_id: str,
    cwd: str | None = None,
) -> DraftEvent:
    payload = {"cwd": cwd or os.getcwd(), **event}
    event_id = optional_str(payload.get("id"))
    event_type = str(payload.get("type") or "event")
    turn_id = optional_str(payload.get("turn_id"))
    event_session_id = str(payload.get("session") or session_id)
    caused_by = optional_str(payload.get("caused_by"))
    domain_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"id", "type", "time", "session", "source", "caused_by"}
    }
    draft = durable_draft_from_payload(
        event_type=event_type,
        payload=domain_payload,
        turn_id=turn_id,
        session_id=event_session_id,
        caused_by=caused_by,
        event_id=event_id,
    )
    if draft is not None:
        return draft
    return DraftEvent(
        event_type=event_type,
        source=str(payload.get("source") or "zeta"),
        payload=domain_payload,
        caused_by=caused_by,
        session_id=event_session_id,
        turn_id=turn_id,
    )


def publish_event_payload_to_log(
    path: Path | str,
    event: dict[str, Any],
    *,
    session_id: str,
    cwd: str | None = None,
) -> Event:
    from zeta.store.events import append_event_to_log

    payload = {"cwd": cwd or os.getcwd(), **event}
    draft = event_payload_draft(payload, session_id=session_id, cwd=cwd)
    return append_event_to_log(
        path,
        Event(
            id=optional_str(payload.get("id")) or f"evt_{uuid.uuid4().hex}",
            event_type=draft.event_type,
            source=draft.source,
            payload=draft.payload,
            idempotency_key=normalized_idempotency_key(draft.idempotency_key),
            caused_by=draft.caused_by,
            session_id=draft.session_id,
            turn_id=draft.turn_id,
            timestamp_micros=timestamp_micros_from_time(payload.get("time")),
        ),
    )


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
