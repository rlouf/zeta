"""Durable event payload constructors."""

import os
import time
import uuid
from pathlib import Path
from typing import Any

from .event import DraftEvent, Event

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
    event_id = payload.get("id") if isinstance(payload.get("id"), str) else None
    event_type = str(payload.get("type") or "event")
    turn_id = (
        payload.get("turn_id") if isinstance(payload.get("turn_id"), str) else None
    )
    event_session_id = str(payload.get("session") or session_id)
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
    from .store import append_event_to_log

    payload = {"cwd": cwd or os.getcwd(), **event}
    draft = event_payload_draft(payload, session_id=session_id, cwd=cwd)
    event_id = payload.get("id") if isinstance(payload.get("id"), str) else None
    event_time = payload.get("time")
    timestamp_micros = (
        int(float(event_time) * 1_000_000)
        if isinstance(event_time, int | float) and not isinstance(event_time, bool)
        else time.time_ns() // 1_000
    )
    idempotency_key = (
        draft.idempotency_key.strip() if draft.idempotency_key is not None else None
    )
    return append_event_to_log(
        path,
        Event(
            id=event_id or f"evt_{uuid.uuid4().hex}",
            event_type=draft.event_type,
            source=draft.source,
            payload=draft.payload,
            idempotency_key=idempotency_key or None,
            caused_by=draft.caused_by,
            session_id=draft.session_id,
            turn_id=draft.turn_id,
            timestamp_micros=timestamp_micros,
        ),
    )
