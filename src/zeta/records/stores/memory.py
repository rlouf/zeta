"""In-memory event store.

The memory store mirrors SQLite append semantics for tests and ephemeral
runtimes, including idempotency and sequence ordering, without creating files.
"""

from __future__ import annotations

from zeta.records.events import AppendOutcome, DraftEvent, Event, json_native_payload
from zeta.records.stores._object_memory import InMemoryStore
from zeta.records.stores.event_store import Filter

__all__ = ["InMemoryStore", "MemoryEventStore"]


class MemoryEventStore:
    """Append-only event store backed by process memory."""

    def __init__(self) -> None:
        self._events: list[Event] = []
        self._by_id: dict[str, Event] = {}
        self._by_idempotency_key: dict[str, Event] = {}
        self._next_seq = 1

    def accept(self, draft: DraftEvent) -> AppendOutcome:
        return self.append(Event.from_draft(draft))

    def append(self, event: Event) -> AppendOutcome:
        duplicate = self._duplicate_for(event)
        if duplicate is not None:
            return AppendOutcome(event=duplicate, inserted=False)
        inserted = Event(
            id=event.id,
            event_type=event.event_type,
            source=event.source,
            payload=json_native_payload(event.payload),
            idempotency_key=event.idempotency_key,
            caused_by=event.caused_by,
            session_id=event.session_id,
            run_id=event.run_id,
            turn_id=event.turn_id,
            timestamp_ms=event.timestamp_ms,
            cursor=self._next_seq,
        )
        self._next_seq += 1
        self._events.append(inserted)
        self._by_id[inserted.id] = inserted
        if inserted.idempotency_key is not None:
            self._by_idempotency_key[inserted.idempotency_key] = inserted
        return AppendOutcome(event=inserted, inserted=True)

    def get(self, event_id: str) -> Event | None:
        return self._by_id.get(event_id)

    def list_events(self, filter: Filter) -> list[Event]:
        events = [event for event in self._events if matches_filter(event, filter)]
        if filter.limit is not None:
            return events[: filter.limit]
        return events

    def children(self, event_id: str, *, limit: int | None = None) -> list[Event]:
        return self.list_events(Filter(caused_by=event_id, limit=limit))

    def causal_chain(self, event_id: str) -> list[Event]:
        chain: list[Event] = []
        seen: set[str] = set()
        current = self.get(event_id)
        while current is not None and current.id not in seen:
            seen.add(current.id)
            chain.append(current)
            if current.caused_by is None:
                break
            current = self.get(current.caused_by)
        chain.reverse()
        return chain

    def events_for_turn(self, turn_id: str) -> list[Event]:
        return self.list_events(Filter(turn_id=turn_id))

    def events_for_run(self, run_id: str) -> list[Event]:
        return self.list_events(Filter(run_id=run_id))

    def clear_session_events(self, session_id: str, *, event_type_prefix: str) -> int:
        original_count = len(self._events)
        self._events = [
            event
            for event in self._events
            if not (
                event.session_id == session_id
                and event.event_type.startswith(event_type_prefix)
            )
        ]
        self._rebuild_indexes()
        return original_count - len(self._events)

    def close(self) -> None:
        return None

    def _duplicate_for(self, event: Event) -> Event | None:
        duplicate = self._by_id.get(event.id)
        if duplicate is not None:
            return duplicate
        if event.idempotency_key is None:
            return None
        return self._by_idempotency_key.get(event.idempotency_key)

    def _rebuild_indexes(self) -> None:
        self._by_id = {event.id: event for event in self._events}
        self._by_idempotency_key = {
            event.idempotency_key: event
            for event in self._events
            if event.idempotency_key is not None
        }


def matches_filter(event: Event, filter: Filter) -> bool:
    if filter.event_type is not None and event.event_type != filter.event_type:
        return False
    if filter.event_type_prefix is not None and not event.event_type.startswith(
        filter.event_type_prefix
    ):
        return False
    if filter.session_id is not None and event.session_id != filter.session_id:
        return False
    if filter.run_id is not None and event.run_id != filter.run_id:
        return False
    if filter.turn_id is not None and event.turn_id != filter.turn_id:
        return False
    if filter.caused_by is not None and event.caused_by != filter.caused_by:
        return False
    if filter.after_cursor is None:
        return True
    return event.cursor is not None and event.cursor > filter.after_cursor
