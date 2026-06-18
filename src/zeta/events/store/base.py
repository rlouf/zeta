"""Event store protocols and filters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..event import Event, EventCursor


@runtime_checkable
class EventReader(Protocol):
    """Readable event log capability."""

    def list_events(self, filter: Filter) -> list[Event]:
        """List durable events matching the filter."""


@dataclass(frozen=True)
class Filter:
    """Event listing filter."""

    event_type: str | None = None
    event_type_prefix: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    caused_by: str | None = None
    after: EventCursor | None = None
    limit: int | None = None
