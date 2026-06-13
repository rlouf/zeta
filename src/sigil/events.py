"""Durable event ontology and SQLite store for Sigil."""

from __future__ import annotations

import atexit
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

EVENT_STORE_NAME = "events.sqlite3"


@dataclass(frozen=True)
class EventCursor:
    """Opaque replay position over the event ordering key."""

    timestamp_micros: int
    id: str

    @classmethod
    def from_event(cls, event: Event) -> EventCursor:
        return cls(timestamp_micros=event.timestamp_micros, id=event.id)

    def encode(self) -> str:
        return f"{self.timestamp_micros}:{self.id}"

    @classmethod
    def decode(cls, value: str) -> EventCursor | None:
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
    timestamp_micros: int


@dataclass(frozen=True)
class AppendOutcome:
    """Result of appending an event."""

    event: Event
    inserted: bool


@dataclass(frozen=True)
class Filter:
    """Event listing filter."""

    event_type: str | None = None
    event_type_prefix: str | None = None
    session_id: str | None = None
    caused_by: str | None = None
    after: EventCursor | None = None
    limit: int | None = None


class SqliteEventStore:
    """SQLite-backed event store."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if self.path != Path(":memory:"):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path), timeout=5.0)
        self.connection.row_factory = sqlite3.Row
        execute_with_retry(self.connection, "PRAGMA busy_timeout=5000")
        execute_with_retry(self.connection, "PRAGMA case_sensitive_like=ON")
        if self.path != Path(":memory:"):
            execute_with_retry(self.connection, "PRAGMA journal_mode=WAL")
            execute_with_retry(self.connection, "PRAGMA synchronous=NORMAL")
        self._init_schema()

    @classmethod
    def in_memory(cls) -> SqliteEventStore:
        return cls(Path(":memory:"))

    def close(self) -> None:
        self.connection.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def _init_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
              id TEXT PRIMARY KEY,
              type TEXT NOT NULL,
              source TEXT NOT NULL,
              payload TEXT NOT NULL,
              idempotency_key TEXT,
              caused_by TEXT,
              session_id TEXT,
              timestamp INTEGER NOT NULL
            ) STRICT;
            CREATE TABLE IF NOT EXISTS event_streams (
              stream_key TEXT NOT NULL,
              version INTEGER NOT NULL,
              event_id TEXT NOT NULL,
              PRIMARY KEY (stream_key, version),
              UNIQUE (stream_key, event_id),
              FOREIGN KEY (event_id) REFERENCES events(id)
            ) STRICT;
            CREATE INDEX IF NOT EXISTS idx_events_type_ts
              ON events(type, timestamp);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_events_idempotency_key
              ON events(idempotency_key)
              WHERE idempotency_key IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_events_session_ts
              ON events(session_id, timestamp)
              WHERE session_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_events_caused_by_ts
              ON events(caused_by, timestamp)
              WHERE caused_by IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_event_streams_event_id
              ON event_streams(event_id);
            """
        )
        self.connection.commit()

    def accept(self, draft: DraftEvent) -> AppendOutcome:
        return self.append(draft.enrich())

    def append(self, event: Event) -> AppendOutcome:
        payload = json.dumps(event.payload, ensure_ascii=False, separators=(",", ":"))
        cursor = self.connection.execute(
            """
            INSERT INTO events
              (id, type, source, payload, idempotency_key, caused_by, session_id,
               timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                event.id,
                event.event_type,
                event.source,
                payload,
                event.idempotency_key,
                event.caused_by,
                event.session_id,
                event.timestamp_micros,
            ),
        )
        self.connection.commit()
        if cursor.rowcount == 1:
            return AppendOutcome(event=event, inserted=True)
        return AppendOutcome(event=self._duplicate_for(event), inserted=False)

    def append_if_stream_version(
        self,
        stream_key: str,
        expected_version: int,
        event: Event,
    ) -> AppendOutcome:
        with self.connection:
            current_version = self.stream_version(stream_key)
            if current_version != expected_version:
                raise ValueError(
                    f"stream {stream_key} expected version {expected_version}, "
                    f"found {current_version}"
                )
            outcome = self._append_without_commit(event)
            if not outcome.inserted:
                return outcome
            self.connection.execute(
                "INSERT INTO event_streams (stream_key, version, event_id) "
                "VALUES (?, ?, ?)",
                (stream_key, expected_version + 1, event.id),
            )
        return outcome

    def _append_without_commit(self, event: Event) -> AppendOutcome:
        payload = json.dumps(event.payload, ensure_ascii=False, separators=(",", ":"))
        cursor = self.connection.execute(
            """
            INSERT INTO events
              (id, type, source, payload, idempotency_key, caused_by, session_id,
               timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                event.id,
                event.event_type,
                event.source,
                payload,
                event.idempotency_key,
                event.caused_by,
                event.session_id,
                event.timestamp_micros,
            ),
        )
        if cursor.rowcount == 1:
            return AppendOutcome(event=event, inserted=True)
        return AppendOutcome(event=self._duplicate_for(event), inserted=False)

    def stream_version(self, stream_key: str) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(version), 0) AS version "
            "FROM event_streams WHERE stream_key = ?",
            (stream_key,),
        ).fetchone()
        return int(row["version"])

    def get(self, event_id: str) -> Event | None:
        row = self.connection.execute(
            """
            SELECT id, type, source, payload, idempotency_key, caused_by,
                   session_id, timestamp
            FROM events
            WHERE id = ?
            """,
            (event_id,),
        ).fetchone()
        return row_to_event(row) if row is not None else None

    def list_events(self, filter: Filter) -> list[Event]:
        clauses: list[str] = []
        params: list[Any] = []
        if filter.event_type is not None:
            clauses.append("type = ?")
            params.append(filter.event_type)
        if filter.event_type_prefix is not None:
            clauses.append("type LIKE ? ESCAPE '\\'")
            params.append(like_prefix(filter.event_type_prefix))
        if filter.session_id is not None:
            clauses.append("session_id = ?")
            params.append(filter.session_id)
        if filter.caused_by is not None:
            clauses.append("caused_by = ?")
            params.append(filter.caused_by)
        if filter.after is not None:
            clauses.append("(timestamp > ? OR (timestamp = ? AND id > ?))")
            params.extend(
                [
                    filter.after.timestamp_micros,
                    filter.after.timestamp_micros,
                    filter.after.id,
                ]
            )
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit = ""
        if filter.limit is not None:
            limit = "LIMIT ?"
            params.append(filter.limit)
        rows = self.connection.execute(
            f"""
            SELECT id, type, source, payload, idempotency_key, caused_by,
                   session_id, timestamp
            FROM events
            {where}
            ORDER BY timestamp ASC, id ASC
            {limit}
            """,
            params,
        ).fetchall()
        return [row_to_event(row) for row in rows]

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
        return [
            event
            for event in self.list_events(Filter())
            if event.payload.get("turn_id") == turn_id
        ]

    def _duplicate_for(self, event: Event) -> Event:
        if event.idempotency_key is not None:
            row = self.connection.execute(
                """
                SELECT id, type, source, payload, idempotency_key, caused_by,
                       session_id, timestamp
                FROM events
                WHERE id = ? OR idempotency_key = ?
                ORDER BY id = ? DESC
                LIMIT 1
                """,
                (event.id, event.idempotency_key, event.id),
            ).fetchone()
        else:
            row = self.connection.execute(
                """
                SELECT id, type, source, payload, idempotency_key, caused_by,
                       session_id, timestamp
                FROM events
                WHERE id = ?
                """,
                (event.id,),
            ).fetchone()
        if row is None:
            raise sqlite3.IntegrityError(f"append conflict for event {event.id}")
        return row_to_event(row)


def event_store_path(root: Path | None = None) -> Path:
    if root is not None:
        return root / EVENT_STORE_NAME
    from .state import state_dir

    return state_dir() / EVENT_STORE_NAME


_DEFAULT_STORES: dict[Path, SqliteEventStore] = {}


def event_store() -> SqliteEventStore:
    path = event_store_path()
    store = _DEFAULT_STORES.get(path)
    if store is None:
        store = SqliteEventStore(path)
        _DEFAULT_STORES[path] = store
    return store


def close_event_stores() -> None:
    while _DEFAULT_STORES:
        _, store = _DEFAULT_STORES.popitem()
        store.close()


def publish_event(draft: DraftEvent) -> AppendOutcome:
    return event_store().accept(draft)


def event_children(event_id: str, *, limit: int | None = None) -> list[Event]:
    return event_store().children(event_id, limit=limit)


def causal_chain(event_id: str) -> list[Event]:
    return event_store().causal_chain(event_id)


def events_for_turn(turn_id: str) -> list[Event]:
    return event_store().events_for_turn(turn_id)


def row_to_event(row: sqlite3.Row) -> Event:
    payload = json.loads(str(row["payload"]))
    if not isinstance(payload, dict):
        payload = {"value": payload}
    return Event(
        id=str(row["id"]),
        event_type=str(row["type"]),
        source=str(row["source"]),
        payload=payload,
        idempotency_key=optional_str(row["idempotency_key"]),
        caused_by=optional_str(row["caused_by"]),
        session_id=optional_str(row["session_id"]),
        timestamp_micros=int(row["timestamp"]),
    )


def optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


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


def like_prefix(prefix: str) -> str:
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}%"


def execute_with_retry(connection: sqlite3.Connection, sql: str) -> None:
    deadline = time.monotonic() + 5
    while True:
        try:
            connection.execute(sql)
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


atexit.register(close_event_stores)
