"""Event store implementations."""

from typing import Protocol, runtime_checkable

from zeta.events import AppendOutcome, Event, Filter
from zeta.store.events.memory import MemoryEventStore
from zeta.store.events.sqlite import (
    EVENT_STORE_NAME,
    ZETA_STORE_NAME,
    SqliteEventStore,
    append_event_to_log,
    append_event_to_log_outcome,
    event_log_causal_chain,
    event_log_children,
    event_log_turn_events,
    event_store_path,
    publish_event_to_log,
    read_event_log,
)


@runtime_checkable
class EventReader(Protocol):
    """Readable event store capability for projections and inspection."""

    def list_events(self, filter: Filter) -> list[Event]:
        """List durable events matching the filter."""


__all__ = [
    "AppendOutcome",
    "EVENT_STORE_NAME",
    "EventReader",
    "Filter",
    "MemoryEventStore",
    "SqliteEventStore",
    "ZETA_STORE_NAME",
    "append_event_to_log",
    "append_event_to_log_outcome",
    "event_log_causal_chain",
    "event_log_children",
    "event_log_turn_events",
    "event_store_path",
    "publish_event_to_log",
    "read_event_log",
]
