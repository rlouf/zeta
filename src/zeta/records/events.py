"""Durable events shared by Zeta runtimes.

Events are the append-only record of runtime activity. Producers submit
drafts through an event sink, stores assign durable ordering, and readers
replay filtered slices to rebuild timelines without depending on trace object
layout.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast
from uuid import uuid4

from zeta.events import DraftEvent, Event, json_native_payload
from zeta.run.events import TURN_EVENT_COMPLETED, TURN_EVENT_FAILED

EVENT_IDEMPOTENT_TYPES = frozenset(
    {
        "zeta.model_call.completed",
        "zeta.tool_call.started",
        "zeta.tool_call.completed",
        "zeta.tool_call.failed",
        "zeta.user_message",
    }
)
TURN_IDEMPOTENT_TYPES = frozenset(
    {
        "zeta.prompt.submitted",
        TURN_EVENT_COMPLETED,
        TURN_EVENT_FAILED,
    }
)
RUNTIME_DURABLE_EXCLUDED_KEYS = {
    "id",
    "type",
    "time",
    "session",
    "source",
    "caused_by",
}
REFUSED_TOOL_ERROR_CODES = {
    "direct-execution-disallowed",
    "disallowed-tool",
    "invalid-json-args",
    "invalid-tool-call",
    "schema-mismatch",
    "staging-unsupported",
    "unknown-tool",
}


@dataclass(frozen=True)
class AppendOutcome:
    """Append result that preserves idempotent producer semantics.

    Stores return the existing event on duplicate input so callers can treat
    retries as successful acknowledgements without guessing whether persistence
    happened.
    """

    event: Event
    inserted: bool


class EventSink(Protocol):
    """Accepts draft events from runtime producers."""

    def accept(self, draft: DraftEvent) -> AppendOutcome:
        """Accept one draft event and return the durable append outcome."""


def publish_event(draft: DraftEvent, *, sink: EventSink) -> AppendOutcome:
    return sink.accept(draft)


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


def ensure_runtime_event_id(event: dict[str, Any]) -> str:
    event_id = event.get("id")
    if isinstance(event_id, str) and event_id:
        return event_id
    event_id = str(uuid4())
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


def tool_result_status(result: Mapping[str, Any]) -> str:
    if result.get("ok") is True:
        return "completed"
    error = result.get("error")
    if isinstance(error, dict) and error.get("code") in REFUSED_TOOL_ERROR_CODES:
        return "refused"
    return "failed"


def normalized_tool_result(name: str, result: Mapping[str, Any]) -> dict[str, Any]:
    stored = dict(result)
    if stored.get("ok") is not False or isinstance(stored.get("error"), dict):
        return stored
    message = tool_failure_message(stored)
    if message:
        stored["error"] = {
            "code": f"{name or 'tool'}-failed",
            "message": message,
        }
    return stored


def tool_failure_message(result: Mapping[str, Any]) -> str:
    content = result.get("content")
    text = first_tool_text(content)
    if text:
        return flatten_tool_text(text)
    metadata = result.get("metadata")
    if isinstance(metadata, dict):
        status = metadata.get("status")
        if isinstance(status, int):
            return f"status {status}"
    return ""


def first_tool_text(content: object) -> str:
    if not isinstance(content, list):
        return ""
    for item in content:
        if not isinstance(item, dict):
            continue
        text = cast("dict[str, Any]", item).get("text")
        if isinstance(text, str) and text.strip():
            return text
    return ""


def flatten_tool_text(text: str) -> str:
    return " ".join(text.strip().split())


__all__ = [
    "AppendOutcome",
    "DraftEvent",
    "EVENT_IDEMPOTENT_TYPES",
    "Event",
    "EventSink",
    "TURN_IDEMPOTENT_TYPES",
    "draft_event_id",
    "draft_event_view",
    "draft_timeline_type",
    "ensure_runtime_event_id",
    "event_timeline_type",
    "event_view",
    "draft_from_boundary_event",
    "durable_model_event_payload",
    "durable_tool_event_payload",
    "json_native_payload",
    "model_call_draft",
    "normalized_tool_result",
    "publish_event",
    "draft_from_runtime_event",
    "status_update_draft",
    "stream_chunk_draft",
    "tool_call_draft",
    "tool_result_status",
    "turn_aborted_draft",
    "user_message_draft",
]
