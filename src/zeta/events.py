"""Durable events shared by Zeta runtimes.

Events are the append-only record of runtime activity. Producers submit
drafts through an event sink, stores assign durable ordering, and readers
replay filtered slices to rebuild timelines without depending on trace object
layout.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4


@dataclass(frozen=True)
class DraftEvent:
    """Producer-supplied event before store enrichment.

    Drafts keep event creation ergonomic at call sites while centralizing ID,
    idempotency, and timestamp normalization at the sink/store boundary.
    """

    event_type: str
    source: str
    payload: dict[str, Any]
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
    payload: dict[str, Any]
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
            payload=dict(draft.payload),
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


@runtime_checkable
class EventReader(Protocol):
    """Readable event log capability for projections and inspection."""

    def list_events(self, filter: Filter) -> list[Event]:
        """List durable events matching the filter."""


def publish_event(draft: DraftEvent, *, sink: EventSink) -> AppendOutcome:
    return sink.accept(draft)


__all__ = [
    "AppendOutcome",
    "DraftEvent",
    "Event",
    "EventReader",
    "EventSink",
    "Filter",
    "publish_event",
]
