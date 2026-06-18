"""Global paths and durable events for Sigil.

Session-local continuity lives in `sigil.sessions`; this module owns the shared
state directory and the frontend event journal.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Any

from zeta.events import Event
from zeta.store.events import (
    EVENT_STORE_NAME,
    Filter,
    append_event_to_log,
    event_log_causal_chain,
    event_log_children,
    event_log_turn_events,
    read_event_log,
)

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
TIMELINE_DURABLE_TYPES = {
    "user_message": "zeta.user_message",
    "model_usage": "zeta.model_call.completed",
}


def state_dir() -> Path:
    """Return the global Sigil state directory."""
    base = os.environ.get("SIGIL_STATE_DIR")
    if base:
        return Path(base)
    return Path.home() / ".sigil"


def event_store_path() -> Path:
    """Return Sigil's frontend event journal path."""
    return state_dir() / EVENT_STORE_NAME


def read_events() -> list[Event]:
    """Read Sigil's frontend event journal."""
    return read_event_log(event_store_path(), Filter())


def history_view(events: list[Event] | None = None) -> Any:
    """Return a Zeta history view over Sigil's durable events."""
    from zeta.history import HistoryView

    if events is not None:
        return HistoryView(events)
    return HistoryView.from_store(event_store_path())


def event_children(event_id: str, *, limit: int | None = None) -> list[Event]:
    return event_log_children(event_store_path(), event_id, limit=limit)


def causal_chain(event_id: str) -> list[Event]:
    return event_log_causal_chain(event_store_path(), event_id)


def events_for_turn(turn_id: str) -> list[Event]:
    return event_log_turn_events(event_store_path(), turn_id)


def append_event(event: dict[str, Any]) -> Event:
    """Append a global audit/debug event with session metadata."""
    from sigil.sessions import session_id

    payload = {"source": "sigil", **event}
    return append_event_to_log(
        event_store_path(), durable_log_event(payload, session_id=session_id())
    )


def append_prompt_submitted_event(event: dict[str, Any]) -> Event:
    prompt_event = dict(event)
    prompt_event["type"] = "zeta.prompt.submitted"
    return append_event(prompt_event)


def durable_log_event(event: dict[str, Any], *, session_id: str) -> Event:
    payload = {"cwd": os.getcwd(), **event}
    source = str(payload.get("source") or "zeta")
    event_type = str(payload.get("type") or "event")
    durable_type = TIMELINE_DURABLE_TYPES.get(event_type, event_type)
    event_id = optional_string(payload.get("id")) or f"evt_{uuid.uuid4().hex}"
    turn_id = optional_string(payload.get("turn_id"))
    event_session_id = str(payload.get("session") or session_id)
    caused_by = optional_string(payload.get("caused_by"))
    domain_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"id", "type", "time", "session", "source", "caused_by"}
    }
    if event_type == "model_usage":
        domain_payload["_timeline_type"] = "model_usage"
    return Event(
        id=event_id,
        event_type=durable_type,
        source="zeta" if is_zeta_durable_event(durable_type) else source,
        payload=domain_payload,
        idempotency_key=durable_idempotency_key(durable_type, event_id, turn_id),
        caused_by=caused_by,
        session_id=event_session_id,
        turn_id=turn_id,
        timestamp_micros=timestamp_micros(payload.get("time")),
    )


def is_zeta_durable_event(event_type: str) -> bool:
    return event_type in EVENT_IDEMPOTENT_TYPES or event_type in TURN_IDEMPOTENT_TYPES


def durable_idempotency_key(
    event_type: str,
    event_id: str,
    turn_id: str | None,
) -> str | None:
    if event_type in EVENT_IDEMPOTENT_TYPES:
        return f"{event_type}:{event_id}"
    if event_type in TURN_IDEMPOTENT_TYPES and turn_id is not None:
        return f"{event_type}:{turn_id}"
    return None


def timestamp_micros(value: Any) -> int:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return int(float(value) * 1_000_000)
    return time.time_ns() // 1_000


def optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
