"""Durable event envelope types.

The event layer keeps runtime facts separate from the substrate object graph.
Drafts are convenient producer inputs; events are the immutable records that
stores can deduplicate, order, and replay.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
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
