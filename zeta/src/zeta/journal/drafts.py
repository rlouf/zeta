"""Draft factories for durable runtime records.

The run loop emits in-memory runtime events. These factories turn them into
drafts the journal can append, choosing the durable event type and the
idempotency key that makes a retry a duplicate rather than a new fact.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from uuid import uuid4

from zeta.events import DraftEvent
from zeta.journal.types import (
    EVENT_IDEMPOTENT_TYPES,
    RUNTIME_DURABLE_EXCLUDED_KEYS,
    TURN_EVENT_FAILED,
    TURN_IDEMPOTENT_TYPES,
)


def ensure_runtime_event_id(
    event: dict[str, Any],
    *,
    event_id_factory: Callable[[], str] | None = None,
) -> str:
    event_id = event.get("id")
    if isinstance(event_id, str) and event_id:
        return event_id
    event_id = event_id_factory() if event_id_factory is not None else str(uuid4())
    if not event_id:
        raise ValueError("event id factory returned an empty id")
    event["id"] = event_id
    return event_id


def draft_from_runtime_event(
    event: Mapping[str, Any],
    *,
    session_id: str | None,
    turn_id: str | None,
    run_id: str | None = None,
) -> DraftEvent:
    event_type = str(event.get("type") or "")
    caused_by = (
        event.get("caused_by") if isinstance(event.get("caused_by"), str) else None
    )
    event_id = event.get("id") if isinstance(event.get("id"), str) else None
    event_dict = dict(event)
    if event_type == "model":
        return model_call_draft(
            payload=durable_model_event_payload(event_dict),
            turn_id=turn_id,
            run_id=run_id,
            session_id=session_id,
            caused_by=caused_by,
            event_id=event_id,
        )
    if event_type in {"tool_call", "tool_result"}:
        return tool_call_draft(
            payload=durable_tool_event_payload(event_dict),
            turn_id=turn_id,
            run_id=run_id,
            session_id=session_id,
            caused_by=caused_by,
            event_id=event_id,
        )
    if event_type == "turn_aborted":
        return turn_aborted_draft(
            reason=str(event.get("reason") or "aborted"),
            content=event.get("content")
            if isinstance(event.get("content"), str)
            else None,
            session_id=session_id,
            turn_id=turn_id,
            run_id=run_id,
            caused_by=caused_by,
        )
    return DraftEvent(
        event_type=event_type,
        source="zeta",
        payload=durable_payload(event_dict),
        idempotency_key=None,
        caused_by=caused_by,
        session_id=session_id,
        run_id=run_id,
        turn_id=turn_id,
    )


def draft_from_boundary_event(
    event: Mapping[str, Any],
    *,
    session_id: str,
) -> DraftEvent:
    payload = dict(event)
    event_type = str(payload.get("type") or "event")
    event_session_id = str(payload.get("session") or session_id)
    if event_type in {"model", "tool_call", "tool_result", "turn_aborted"}:
        raw_turn_id = payload.get("turn_id")
        raw_run_id = payload.get("run_id")
        turn_id = raw_turn_id if isinstance(raw_turn_id, str) and raw_turn_id else None
        run_id = raw_run_id if isinstance(raw_run_id, str) and raw_run_id else None
        return draft_from_runtime_event(
            payload,
            session_id=event_session_id,
            turn_id=turn_id,
            run_id=run_id,
        )
    raw_event_id = payload.get("id")
    raw_turn_id = payload.get("turn_id")
    raw_run_id = payload.get("run_id")
    raw_caused_by = payload.get("caused_by")
    event_id = raw_event_id if isinstance(raw_event_id, str) and raw_event_id else None
    turn_id = raw_turn_id if isinstance(raw_turn_id, str) and raw_turn_id else None
    run_id = raw_run_id if isinstance(raw_run_id, str) and raw_run_id else None
    caused_by = (
        raw_caused_by if isinstance(raw_caused_by, str) and raw_caused_by else None
    )
    domain_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"id", "type", "time", "session", "source", "caused_by", "run_id"}
    }
    if event_type == "model_usage":
        domain_payload["_timeline_type"] = "model_usage"
    durable_type = durable_event_type(event_type)
    return DraftEvent(
        durable_type,
        "zeta"
        if durable_type.startswith("zeta.")
        else str(payload.get("source") or "zeta"),
        domain_payload,
        idempotency_key=durable_event_idempotency_key(
            durable_type,
            event_id=event_id,
            turn_id=turn_id,
        ),
        caused_by=caused_by,
        session_id=event_session_id,
        run_id=run_id,
        turn_id=turn_id,
    )


def durable_event_type(event_type: str) -> str:
    return {
        "user_message": "zeta.user_message",
        "model_usage": "zeta.model_call.completed",
    }.get(event_type, event_type)


def durable_event_idempotency_key(
    event_type: str,
    *,
    event_id: str | None,
    turn_id: str | None,
) -> str | None:
    if event_type in EVENT_IDEMPOTENT_TYPES:
        return f"{event_type}:{event_id}" if event_id is not None else None
    if event_type in TURN_IDEMPOTENT_TYPES:
        return f"{event_type}:{turn_id}" if turn_id is not None else None
    return None


def model_call_draft(
    *,
    payload: dict[str, Any],
    turn_id: str | None,
    session_id: str | None,
    run_id: str | None = None,
    caused_by: str | None = None,
    event_id: str | None = None,
) -> DraftEvent:
    return durable_event_draft(
        "zeta.model_call.completed",
        payload=payload,
        turn_id=turn_id,
        run_id=run_id,
        session_id=session_id,
        caused_by=caused_by,
        event_id=event_id,
    )


def tool_call_draft(
    *,
    payload: dict[str, Any],
    turn_id: str | None,
    session_id: str | None,
    run_id: str | None = None,
    caused_by: str | None = None,
    event_id: str | None = None,
) -> DraftEvent:
    return durable_event_draft(
        tool_call_event_type(payload),
        payload=payload,
        turn_id=turn_id,
        run_id=run_id,
        session_id=session_id,
        caused_by=caused_by,
        event_id=event_id,
    )


def turn_aborted_draft(
    *,
    reason: str,
    session_id: str | None,
    turn_id: str | None,
    run_id: str | None = None,
    caused_by: str | None = None,
    content: str | None = None,
) -> DraftEvent:
    payload = {
        "_timeline_type": "turn_aborted",
        "reason": reason,
        "content": content or f"(turn aborted: {reason.replace('_', ' ')})",
    }
    return DraftEvent(
        event_type=TURN_EVENT_FAILED,
        source="zeta",
        payload=payload,
        idempotency_key=None,
        caused_by=caused_by,
        session_id=session_id,
        run_id=run_id,
        turn_id=turn_id,
    )


def stream_chunk_draft(text: str) -> DraftEvent:
    return DraftEvent(
        "runtime.stream.chunk",
        "zeta",
        {"text": text, "_timeline_type": "runtime.stream.chunk"},
    )


def status_update_draft(status: str, text: str) -> DraftEvent:
    return DraftEvent(
        "runtime.status.update",
        "zeta",
        {"status": status, "text": text, "_timeline_type": "runtime.status.update"},
    )


def user_message_draft(
    payload: Mapping[str, Any],
    *,
    session_id: str | None,
    turn_id: str | None,
    run_id: str | None = None,
    caused_by: str | None = None,
) -> DraftEvent:
    return DraftEvent(
        event_type="zeta.user_message",
        source="zeta",
        payload={**payload, "_timeline_type": "user_message"},
        idempotency_key=None,
        caused_by=caused_by,
        session_id=session_id,
        run_id=run_id,
        turn_id=turn_id,
    )


def durable_event_draft(
    event_type: str,
    *,
    payload: dict[str, Any],
    turn_id: str | None,
    session_id: str | None,
    run_id: str | None = None,
    caused_by: str | None,
    event_id: str | None,
) -> DraftEvent:
    return DraftEvent(
        event_type=event_type,
        source="zeta",
        payload=payload,
        idempotency_key=event_idempotency_key(event_type, event_id),
        caused_by=caused_by,
        session_id=session_id,
        run_id=run_id,
        turn_id=turn_id,
    )


def event_idempotency_key(event_type: str, event_id: str | None) -> str | None:
    if event_type not in EVENT_IDEMPOTENT_TYPES or not event_id:
        return None
    return f"{event_type}:{event_id}"


def tool_call_event_type(payload: Mapping[str, Any]) -> str:
    if payload.get("_timeline_type") == "tool_call":
        return "zeta.tool_call.started"
    if tool_call_failed(payload):
        return "zeta.tool_call.failed"
    return "zeta.tool_call.completed"


def tool_call_failed(payload: Mapping[str, Any]) -> bool:
    result = payload.get("result")
    return isinstance(result, dict) and result.get("ok") is False


def durable_model_event_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    event_dict = dict(event)
    payload = durable_payload(event_dict)
    payload["_timeline_type"] = "model"
    return payload


def durable_tool_event_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    event_dict = dict(event)
    payload = durable_payload(event_dict)
    event_type = str(event.get("type") or "")
    payload["_timeline_type"] = event_type
    return payload


def durable_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in event.items()
        if key not in RUNTIME_DURABLE_EXCLUDED_KEYS
    }
