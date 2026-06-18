"""Durable event payload constructors."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..substrate import durable_event_object_links
from .event import DraftEvent, Event, timestamp_micros_from_time

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


def durable_event_draft(
    event_type: str,
    source: str,
    *,
    payload: dict[str, Any],
    turn_id: str | None,
    session_id: str,
    caused_by: str | None,
    event_id: str | None,
    idempotency_key: str | None,
    timestamp_micros: int | None,
) -> DraftEvent:
    return DraftEvent(
        event_type=event_type,
        source=source,
        payload=payload,
        idempotency_key=idempotency_key,
        caused_by=caused_by,
        session_id=session_id,
        turn_id=turn_id,
        timestamp_micros=timestamp_micros,
        event_id=event_id,
    )


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


def durable_event_id(event_type: str, event: dict[str, Any]) -> str | None:
    del event_type
    event_id = event.get("id")
    return event_id if isinstance(event_id, str) and event_id else None


def optional_event_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


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
    timestamp_micros: int | None = None,
) -> DraftEvent:
    return durable_event_draft(
        event_type,
        "zeta",
        payload=payload,
        turn_id=turn_id,
        session_id=session_id,
        caused_by=caused_by,
        event_id=event_id,
        idempotency_key=durable_event_idempotency_key(
            event_type,
            event_id=event_id,
            turn_id=turn_id,
        ),
        timestamp_micros=timestamp_micros,
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
    timestamp_micros: int | None = None,
) -> DraftEvent:
    return durable_event_for_type(
        "zeta.model.called",
        payload=payload,
        turn_id=turn_id,
        session_id=session_id,
        caused_by=caused_by,
        event_id=event_id,
        timestamp_micros=timestamp_micros,
    )


def tool_called_event(
    *,
    payload: dict[str, Any],
    turn_id: str | None,
    session_id: str,
    caused_by: str | None = None,
    event_id: str | None = None,
    timestamp_micros: int | None = None,
) -> DraftEvent:
    return durable_event_for_type(
        "zeta.tool.called",
        payload=payload,
        turn_id=turn_id,
        session_id=session_id,
        caused_by=caused_by,
        event_id=event_id,
        timestamp_micros=timestamp_micros,
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
        timestamp_micros: int | None = None,
    ) -> DraftEvent:
        return durable_event_for_type(
            "zeta.prompt.submitted",
            payload=payload,
            turn_id=turn_id,
            session_id=session_id,
            caused_by=caused_by,
            event_id=event_id,
            timestamp_micros=timestamp_micros,
        )

    def turn_completed(
        self,
        *,
        payload: dict[str, Any],
        turn_id: str | None,
        session_id: str,
        caused_by: str | None = None,
        event_id: str | None = None,
        timestamp_micros: int | None = None,
    ) -> DraftEvent:
        return self._turn_event(
            "zeta.turn.completed",
            payload=payload,
            turn_id=turn_id,
            session_id=session_id,
            caused_by=caused_by,
            event_id=event_id,
            timestamp_micros=timestamp_micros,
        )

    def turn_failed(
        self,
        *,
        payload: dict[str, Any],
        turn_id: str | None,
        session_id: str,
        caused_by: str | None = None,
        event_id: str | None = None,
        timestamp_micros: int | None = None,
    ) -> DraftEvent:
        return self._turn_event(
            "zeta.turn.failed",
            payload=payload,
            turn_id=turn_id,
            session_id=session_id,
            caused_by=caused_by,
            event_id=event_id,
            timestamp_micros=timestamp_micros,
        )

    def turn_aborted(
        self,
        *,
        payload: dict[str, Any],
        turn_id: str | None,
        session_id: str,
        caused_by: str | None = None,
        event_id: str | None = None,
        timestamp_micros: int | None = None,
    ) -> DraftEvent:
        return self._turn_event(
            "zeta.turn.aborted",
            payload=payload,
            turn_id=turn_id,
            session_id=session_id,
            caused_by=caused_by,
            event_id=event_id,
            timestamp_micros=timestamp_micros,
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
        timestamp_micros: int | None,
    ) -> DraftEvent:
        return durable_event_for_type(
            event_type,
            payload=payload,
            turn_id=turn_id,
            session_id=session_id,
            caused_by=caused_by,
            event_id=event_id,
            timestamp_micros=timestamp_micros,
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
    timestamp_micros: int | None,
) -> DraftEvent | None:
    if event_type in SUPPORTED_DURABLE_EVENT_TYPES:
        return durable_event_for_type(
            event_type,
            payload=payload,
            turn_id=turn_id,
            session_id=session_id,
            caused_by=caused_by,
            event_id=event_id,
            timestamp_micros=timestamp_micros,
        )
    return None


def event_payload_draft(
    event: dict[str, Any],
    *,
    session_id: str,
    cwd: str | None = None,
) -> DraftEvent:
    payload = {"cwd": cwd or os.getcwd(), **event}
    event_id = payload.get("id") if isinstance(payload.get("id"), str) else None
    event_type = str(payload.get("type") or "event")
    turn_id = (
        payload.get("turn_id") if isinstance(payload.get("turn_id"), str) else None
    )
    event_session_id = str(payload.get("session") or session_id)
    event_timestamp = timestamp_micros_from_time(payload.get("time"))
    caused_by = (
        str(payload["caused_by"]) if isinstance(payload.get("caused_by"), str) else None
    )
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
        timestamp_micros=event_timestamp,
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
        timestamp_micros=event_timestamp,
        event_id=event_id,
    )


def publish_event_payload_to_log(
    path: Path | str,
    event: dict[str, Any],
    *,
    session_id: str,
    cwd: str | None = None,
) -> Event:
    from .sqlite import publish_event_to_log

    return publish_event_to_log(
        path,
        event_payload_draft(event, session_id=session_id, cwd=cwd),
    )
