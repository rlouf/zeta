"""Event domain shapes."""

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

__all__ = ["DraftEvent", "Event"]


@dataclass(frozen=True)
class DraftEvent:
    """An event fact proposed by a producer before it enters the log.

    Drafts are created by runtime code, tools, dispatchers, and host
    boundaries. Stores turn them into durable `Event` values by assigning an id,
    timestamp, and cursor while preserving causality and idempotency fields.
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
    """A durable fact in the append-only runtime event log.

    Events are the replayable history of a session or turn. They are created
    from `DraftEvent` values by an event store and projected into timelines,
    trace objects, RPC notifications, and history views.
    """

    id: str
    event_type: str
    source: str
    payload: Mapping[str, Any]
    idempotency_key: str | None
    caused_by: str | None
    session_id: str | None
    turn_id: str | None
    timestamp_ms: int
    cursor: int | None = None

    @classmethod
    def from_draft(cls, draft: DraftEvent) -> "Event":
        idempotency_key = (
            draft.idempotency_key.strip() or None
            if draft.idempotency_key is not None
            else None
        )
        return cls(
            id=f"evt_{uuid4().hex}",
            event_type=draft.event_type,
            source=draft.source,
            payload=dict(draft.payload),
            idempotency_key=idempotency_key,
            caused_by=draft.caused_by,
            session_id=draft.session_id,
            turn_id=draft.turn_id,
            timestamp_ms=time.time_ns() // 1_000_000,
        )
