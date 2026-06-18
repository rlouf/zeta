"""Event store protocols and filters.

Stores own durable event ordering and filtered replay. Keeping the reader
protocol separate from sinks lets timeline projection depend only on read
capability while producers depend only on append capability.
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..event import Event


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


@runtime_checkable
class EventReader(Protocol):
    """Readable event log capability for projections and inspection."""

    def list_events(self, filter: Filter) -> list[Event]:
        """List durable events matching the filter."""
