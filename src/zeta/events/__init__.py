"""Durable events shared by Zeta runtimes.

Events are the append-only record of runtime activity. Producers submit
drafts through an event sink, stores assign durable ordering, and readers
replay filtered slices to rebuild timelines without depending on trace object
layout.
"""

from .event import (
    DraftEvent,
    Event,
)
from .sink import EventSink, publish_event
from .store import (
    EVENT_STORE_NAME,
    ZETA_STORE_NAME,
    AppendOutcome,
    EventReader,
    Filter,
    MemoryEventStore,
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

__all__ = [
    "AppendOutcome",
    "DraftEvent",
    "EVENT_STORE_NAME",
    "Event",
    "EventReader",
    "EventSink",
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
    "publish_event",
    "publish_event_to_log",
    "read_event_log",
]
