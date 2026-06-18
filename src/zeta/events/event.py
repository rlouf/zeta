"""Durable event envelope types."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4


@dataclass(frozen=True)
class EventCursor:
    """Opaque replay position over the event ordering key."""

    seq: int | None = None
    timestamp_micros: int | None = None
    id: str | None = None

    @classmethod
    def from_event(cls, event: Event) -> EventCursor:
        return cls(seq=event.seq)

    def encode(self) -> str:
        if self.seq is not None:
            return str(self.seq)
        return f"{self.timestamp_micros}:{self.id}"

    @classmethod
    def decode(cls, value: str) -> EventCursor | None:
        try:
            return cls(seq=int(value))
        except ValueError:
            pass
        timestamp, separator, event_id = value.partition(":")
        if not separator:
            return None
        try:
            timestamp_micros = int(timestamp)
        except ValueError:
            return None
        return cls(timestamp_micros=timestamp_micros, id=event_id)


@dataclass(frozen=True)
class DraftEvent:
    """Pre-enrichment event accepted by the event store."""

    event_type: str
    source: str
    payload: dict[str, Any]
    idempotency_key: str | None = None
    caused_by: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    timestamp_micros: int | None = None
    event_id: str | None = None

    def enrich(self) -> Event:
        idempotency_key = normalize_idempotency_key(self.idempotency_key)
        event_id = self.event_id
        if event_id is None and idempotency_key is not None:
            event_id = id_for_idempotency_key(idempotency_key)
        if event_id is None:
            event_id = f"evt_{uuid4().hex}"
        return Event(
            id=event_id,
            event_type=self.event_type,
            source=self.source,
            payload=dict(self.payload),
            idempotency_key=idempotency_key,
            caused_by=self.caused_by,
            session_id=self.session_id,
            turn_id=self.turn_id,
            timestamp_micros=self.timestamp_micros or current_timestamp_micros(),
        )


@dataclass(frozen=True)
class Event:
    """Durable event fact."""

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

    def cursor(self) -> EventCursor:
        return EventCursor.from_event(self)


@dataclass(frozen=True)
class AppendOutcome:
    """Result of appending an event."""

    event: Event
    inserted: bool


def current_timestamp_micros() -> int:
    return time.time_ns() // 1_000


def timestamp_micros_from_time(value: object) -> int | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return int(float(value) * 1_000_000)
    return None


def time_from_timestamp_micros(value: int) -> float:
    return value / 1_000_000


def id_for_idempotency_key(key: str) -> str:
    return "evt_" + key.encode("utf-8").hex()


def normalize_idempotency_key(key: str | None) -> str | None:
    if key is None:
        return None
    normalized = key.strip()
    return normalized or None
