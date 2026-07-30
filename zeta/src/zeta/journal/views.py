"""Timeline views projected from durable events.

The prompt builder and the inspection commands read events as flat view
objects rather than as records. These helpers derive that view, including the
timeline type that names an event in a conversation.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from zeta.events import DraftEvent, Event


def event_view(event: Event) -> dict[str, Any]:
    view_type = durable_view_type(event)
    payload = {
        key: value
        for key, value in event.payload.items()
        if key not in {"_timeline_type", "_time"}
    }
    projected: dict[str, Any] = {
        "type": view_type or event.event_type,
        "id": event.id,
        "time": exact_event_time(event),
    }
    if not view_type:
        projected["source"] = event.source
    if event.session_id is not None:
        projected["session"] = event.session_id
    if event.run_id is not None:
        projected["run_id"] = event.run_id
    if event.turn_id is not None:
        projected["turn_id"] = event.turn_id
    if event.caused_by is not None:
        projected["caused_by"] = event.caused_by
    projected.update(payload)
    if event.cursor is not None:
        projected["cursor"] = str(event.cursor)
    return projected


def draft_event_view(draft: DraftEvent) -> dict[str, Any]:
    event = Event(
        id=draft_event_id(draft) or f"evt_{uuid4().hex}",
        event_type=draft.event_type,
        source=draft.source,
        payload=dict(draft.payload),
        idempotency_key=draft.idempotency_key,
        caused_by=draft.caused_by,
        session_id=draft.session_id,
        run_id=draft.run_id,
        turn_id=draft.turn_id,
        timestamp_ms=time.time_ns() // 1_000_000,
    )
    return event_view(event)


def exact_event_time(event: Event) -> float:
    exact_time = event.payload.get("_time")
    if isinstance(exact_time, int | float) and not isinstance(exact_time, bool):
        return float(exact_time)
    return event.timestamp_ms / 1_000


def event_timeline_type(event: Event) -> str:
    return payload_timeline_type(
        event.payload,
        event.event_type,
        fallback=event.event_type,
    )


def draft_timeline_type(draft: DraftEvent) -> str:
    return payload_timeline_type(
        draft.payload,
        draft.event_type,
        fallback=draft.event_type,
    )


def durable_view_type(event: Event) -> str:
    return payload_timeline_type(event.payload, event.event_type, fallback="")


def payload_timeline_type(
    payload: Mapping[str, Any],
    event_type: str,
    *,
    fallback: str,
) -> str:
    view_type = payload.get("_timeline_type")
    if isinstance(view_type, str) and view_type:
        return view_type
    prefix = "zeta."
    if event_type.startswith(prefix):
        return event_type[len(prefix) :]
    return fallback


def draft_event_id(draft: DraftEvent) -> str | None:
    key = draft.idempotency_key
    prefix = f"{draft.event_type}:"
    if key is None or not key.startswith(prefix):
        return None
    event_id = key[len(prefix) :].strip()
    return event_id or None
