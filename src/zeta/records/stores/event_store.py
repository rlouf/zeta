"""Event store contracts and query shapes."""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from zeta.records.events import AppendOutcome, DraftEvent, Event


@dataclass(frozen=True)
class Filter:
    """Criteria for selecting events from the append-only log."""

    event_type: str | None = None
    event_type_prefix: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    turn_id: str | None = None
    caused_by: str | None = None
    after_cursor: int | None = None
    limit: int | None = None


@runtime_checkable
class EventReader(Protocol):
    """Readable event store capability for projections and inspection."""

    def list_events(self, filter: Filter) -> list[Event]:
        """List durable events matching the filter."""


@runtime_checkable
class EventWriter(Protocol):
    """Appendable event store capability for runtime producers."""

    def accept(self, draft: DraftEvent) -> AppendOutcome:
        """Accept one draft event and return the durable append outcome."""


@runtime_checkable
class EventStoreProtocol(EventReader, EventWriter, Protocol):
    """Full event ledger API shared by memory and SQLite implementations."""

    def append(self, event: Event) -> AppendOutcome:
        """Append one already durable event."""

    def get(self, event_id: str) -> Event | None:
        """Return one event by id."""

    def children(self, event_id: str, *, limit: int | None = None) -> list[Event]:
        """Return direct children caused by an event."""

    def causal_chain(self, event_id: str) -> list[Event]:
        """Return the root-to-event causal chain."""

    def events_for_turn(self, turn_id: str) -> list[Event]:
        """Return events associated with a turn."""

    def events_for_run(self, run_id: str) -> list[Event]:
        """Return events associated with a run."""

    def clear_session_events(self, session_id: str, *, event_type_prefix: str) -> int:
        """Delete events in one session matching a type prefix."""

    def close(self) -> None:
        """Release store resources."""
