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
from types import MappingProxyType
from typing import Any, Protocol, cast
from uuid import uuid4

from zeta.substrate import trace_object_id

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
        "zeta.turn.completed",
        "zeta.turn.failed",
    }
)
RUNTIME_DURABLE_EXCLUDED_KEYS = {
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
class DraftEvent:
    """Producer-supplied event before store enrichment.

    Drafts keep event creation ergonomic at call sites while centralizing ID,
    idempotency, and timestamp normalization at the sink/store boundary.
    """

    event_type: str
    source: str
    payload: Mapping[str, Any]
    idempotency_key: str | None = None
    caused_by: str | None = None
    session_id: str | None = None
    turn_id: str | None = None


@dataclass(frozen=True)
class Event:
    """Immutable fact recorded in the event log.

    Events carry both domain payload and bookkeeping fields so replay,
    causality traversal, and session filtering do not need to inspect payload
    schemas.
    """

    id: str
    event_type: str
    source: str
    payload: Mapping[str, Any]
    idempotency_key: str | None
    caused_by: str | None
    session_id: str | None
    turn_id: str | None
    timestamp_micros: int
    seq: int = 0

    @classmethod
    def from_draft(cls, draft: DraftEvent) -> Event:
        idempotency_key = (
            draft.idempotency_key.strip() if draft.idempotency_key is not None else None
        )
        idempotency_key = idempotency_key or None
        return cls(
            id=f"evt_{uuid4().hex}",
            event_type=draft.event_type,
            source=draft.source,
            payload=immutable_payload(draft.payload),
            idempotency_key=idempotency_key,
            caused_by=draft.caused_by,
            session_id=draft.session_id,
            turn_id=draft.turn_id,
            timestamp_micros=time.time_ns() // 1_000,
        )


@dataclass(frozen=True)
class AppendOutcome:
    """Append result that preserves idempotent producer semantics.

    Stores return the existing event on duplicate input so callers can treat
    retries as successful acknowledgements without guessing whether persistence
    happened.
    """

    event: Event
    inserted: bool


@dataclass(frozen=True)
class Filter:
    """Selection criteria for replaying a slice of the event log."""

    event_type: str | None = None
    event_type_prefix: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    caused_by: str | None = None
    after_seq: int | None = None
    limit: int | None = None


class EventSink(Protocol):
    """Accepts draft events from runtime producers."""

    def accept(self, draft: DraftEvent) -> AppendOutcome:
        """Accept one draft event and return the durable append outcome."""


def publish_event(draft: DraftEvent, *, sink: EventSink) -> AppendOutcome:
    return sink.accept(draft)


def immutable_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(payload))


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
    if event.turn_id is not None:
        projected["turn_id"] = event.turn_id
    if event.caused_by is not None:
        projected["caused_by"] = event.caused_by
    projected.update(payload)
    add_durable_object_refs(projected)
    if event.seq:
        projected["cursor"] = str(event.seq)
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
        turn_id=draft.turn_id,
        timestamp_micros=time.time_ns() // 1_000,
    )
    return event_view(event)


def event_views(events: list[Event]) -> list[dict[str, Any]]:
    return [view for event in events if (view := event_view(event))]


def exact_event_time(event: Event) -> float:
    exact_time = event.payload.get("_time")
    if isinstance(exact_time, int | float) and not isinstance(exact_time, bool):
        return float(exact_time)
    return event.timestamp_micros / 1_000_000


def durable_view_type(event: Event) -> str:
    view_type = event.payload.get("_timeline_type")
    if isinstance(view_type, str) and view_type:
        return view_type
    prefix = "zeta."
    if event.event_type.startswith(prefix):
        return event.event_type[len(prefix) :]
    return ""


def draft_event_id(draft: DraftEvent) -> str | None:
    key = draft.idempotency_key
    prefix = f"{draft.event_type}:"
    if key is None or not key.startswith(prefix):
        return None
    event_id = key[len(prefix) :].strip()
    return event_id or None


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
        if returned and event.get("type") == "model":
            event.setdefault("tool_call_object_ids", []).append(object_id)
        else:
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


def durable_ref_handlers(returned: bool) -> dict[str, Any]:
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


def runtime_event_draft(
    event: Mapping[str, Any],
    *,
    session_id: str | None,
    turn_id: str | None,
) -> DraftEvent:
    event_type = str(event.get("type") or "")
    caused_by = (
        event.get("caused_by") if isinstance(event.get("caused_by"), str) else None
    )
    event_id = event.get("id") if isinstance(event.get("id"), str) else None
    event_dict = dict(event)
    if event_type == "model":
        return model_call_draft(
            payload=model_durable_payload(event_dict),
            turn_id=turn_id,
            session_id=session_id,
            caused_by=caused_by,
            event_id=event_id,
        )
    if event_type in {"tool_call", "tool_result"}:
        return tool_call_draft(
            payload=tool_durable_payload(event_dict),
            turn_id=turn_id,
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
            caused_by=caused_by,
        )
    return DraftEvent(
        event_type=event_type,
        source="zeta",
        payload=durable_payload(event_dict),
        idempotency_key=None,
        caused_by=caused_by,
        session_id=session_id,
        turn_id=turn_id,
    )


def boundary_event_draft(
    event: Mapping[str, Any],
    *,
    session_id: str,
) -> DraftEvent:
    payload = dict(event)
    event_type = str(payload.get("type") or "event")
    event_session_id = str(payload.get("session") or session_id)
    if event_type in {"model", "tool_call", "tool_result", "turn_aborted"}:
        turn_id = optional_event_string(payload.get("turn_id"))
        return runtime_event_draft(
            payload,
            session_id=event_session_id,
            turn_id=turn_id,
        )
    event_id = optional_event_string(payload.get("id"))
    turn_id = optional_event_string(payload.get("turn_id"))
    caused_by = optional_event_string(payload.get("caused_by"))
    domain_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"id", "type", "time", "session", "source", "caused_by"}
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


def optional_event_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def model_call_draft(
    *,
    payload: dict[str, Any],
    turn_id: str | None,
    session_id: str | None,
    caused_by: str | None = None,
    event_id: str | None = None,
) -> DraftEvent:
    return durable_event_draft(
        "zeta.model_call.completed",
        payload=payload,
        turn_id=turn_id,
        session_id=session_id,
        caused_by=caused_by,
        event_id=event_id,
    )


def tool_call_draft(
    *,
    payload: dict[str, Any],
    turn_id: str | None,
    session_id: str | None,
    caused_by: str | None = None,
    event_id: str | None = None,
) -> DraftEvent:
    return durable_event_draft(
        tool_call_event_type(payload),
        payload=payload,
        turn_id=turn_id,
        session_id=session_id,
        caused_by=caused_by,
        event_id=event_id,
    )


def turn_aborted_draft(
    *,
    reason: str,
    session_id: str | None,
    turn_id: str | None,
    caused_by: str | None = None,
    content: str | None = None,
) -> DraftEvent:
    payload = {
        "_timeline_type": "turn_aborted",
        "reason": reason,
        "content": content or f"(turn aborted: {reason.replace('_', ' ')})",
    }
    return DraftEvent(
        event_type="zeta.turn.failed",
        source="zeta",
        payload=payload,
        idempotency_key=None,
        caused_by=caused_by,
        session_id=session_id,
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
    caused_by: str | None = None,
) -> DraftEvent:
    return DraftEvent(
        event_type="zeta.user_message",
        source="zeta",
        payload={**payload, "_timeline_type": "user_message"},
        idempotency_key=None,
        caused_by=caused_by,
        session_id=session_id,
        turn_id=turn_id,
    )


def durable_event_draft(
    event_type: str,
    *,
    payload: dict[str, Any],
    turn_id: str | None,
    session_id: str | None,
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


def model_durable_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    event_dict = dict(event)
    payload = durable_payload(event_dict)
    payload["_timeline_type"] = "model"
    used_objects, returned_objects = model_durable_object_links(event_dict)
    add_link_payload(payload, used_objects, returned_objects)
    return payload


def tool_durable_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    event_dict = dict(event)
    payload = durable_payload(event_dict)
    payload["_timeline_type"] = str(event.get("type") or "")
    used_objects, returned_objects = tool_durable_object_links(event_dict)
    add_link_payload(payload, used_objects, returned_objects)
    return payload


def durable_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in event.items()
        if key not in RUNTIME_DURABLE_EXCLUDED_KEYS
    }


def add_link_payload(
    payload: dict[str, Any],
    used_objects: list[dict[str, str]],
    returned_objects: list[dict[str, str]],
) -> None:
    if used_objects:
        payload["used_objects"] = used_objects
    if returned_objects:
        payload["returned_objects"] = returned_objects


def model_durable_object_links(
    event: Mapping[str, Any],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    used_objects: list[dict[str, str]] = []
    returned_objects: list[dict[str, str]] = []
    prompt_trace = event.get("prompt_trace")
    if isinstance(prompt_trace, dict):
        add_durable_object_link(
            used_objects,
            "prompt",
            trace_object_id(prompt_trace, "prompt_object_id"),
        )
        add_durable_object_link(
            returned_objects,
            "assistant_message",
            trace_object_id(prompt_trace, "assistant_message_object_id"),
        )
    add_durable_object_links(
        returned_objects,
        "tool_call",
        event.get("tool_call_object_ids"),
    )
    add_durable_object_link(
        returned_objects,
        "tool_call",
        trace_object_id(dict(event), "tool_call_object_id"),
    )
    return used_objects, returned_objects


def tool_durable_object_links(
    event: Mapping[str, Any],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    event_type = str(event.get("type") or "")
    if event_type == "tool_result":
        return tool_result_durable_object_links(event)
    if event_type != "tool_call":
        return [], []
    returned_objects: list[dict[str, str]] = []
    add_durable_object_link(
        returned_objects,
        "tool_call",
        trace_object_id(dict(event), "tool_call_object_id"),
    )
    return [], returned_objects


def tool_result_durable_object_links(
    event: Mapping[str, Any],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    used_objects: list[dict[str, str]] = []
    returned_objects: list[dict[str, str]] = []
    add_durable_object_link(
        used_objects,
        "tool_call",
        trace_object_id(dict(event), "tool_call_object_id"),
    )
    add_durable_object_link(
        returned_objects,
        "tool_result",
        trace_object_id(dict(event), "tool_result_object_id"),
    )
    return used_objects, returned_objects


def add_durable_object_links(
    links: list[dict[str, str]],
    kind: str,
    object_ids: Any,
) -> None:
    if not isinstance(object_ids, (list, tuple)):
        return
    for object_id in object_ids:
        add_durable_object_link(
            links,
            kind,
            object_id if isinstance(object_id, str) else None,
        )


def add_durable_object_link(
    links: list[dict[str, str]],
    kind: str,
    object_id: str | None,
) -> None:
    if not object_id:
        return
    link = {"kind": kind, "id": object_id}
    if link not in links:
        links.append(link)


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
    "Filter",
    "TURN_IDEMPOTENT_TYPES",
    "draft_event_id",
    "draft_event_view",
    "event_view",
    "event_views",
    "boundary_event_draft",
    "model_call_draft",
    "model_durable_object_links",
    "model_durable_payload",
    "immutable_payload",
    "normalized_tool_result",
    "publish_event",
    "runtime_event_draft",
    "status_update_draft",
    "stream_chunk_draft",
    "tool_call_draft",
    "tool_durable_object_links",
    "tool_durable_payload",
    "tool_result_durable_object_links",
    "tool_result_status",
    "turn_aborted_draft",
    "user_message_draft",
]
