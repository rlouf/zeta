"""Durable event ontology and SQLite store for Zeta runtimes."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

EVENT_STORE_NAME = "events.sqlite3"
ZETA_STORE_NAME = "zeta.sqlite3"


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


class EventSink(Protocol):
    """Consumer of draft events."""

    def accept(self, draft: DraftEvent) -> AppendOutcome:
        """Accept one draft event."""


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


def durable_event_draft(
    event_type: str,
    source: str,
    *,
    payload: dict[str, Any],
    turn_id: str | None,
    session_id: str,
    caused_by: str | None,
    event_id: str | None,
    idempotency_key: str | None,
    timestamp_micros: int | None,
) -> DraftEvent:
    return DraftEvent(
        event_type=event_type,
        source=source,
        payload=payload,
        idempotency_key=idempotency_key,
        caused_by=caused_by,
        session_id=session_id,
        turn_id=turn_id,
        timestamp_micros=timestamp_micros,
        event_id=event_id,
    )


def event_idempotency_key(event_type: str, event_id: str | None) -> str | None:
    if not event_id:
        return None
    return f"{event_type}:{event_id}"


def turn_idempotency_key(event_type: str, turn_id: str | None) -> str | None:
    if not turn_id:
        return None
    return f"{event_type}:{turn_id}"


def model_called_event(
    *,
    payload: dict[str, Any],
    turn_id: str | None,
    session_id: str,
    caused_by: str | None = None,
    event_id: str | None = None,
    timestamp_micros: int | None = None,
) -> DraftEvent:
    return durable_event_draft(
        "zeta.model.called",
        "zeta",
        payload=payload,
        turn_id=turn_id,
        session_id=session_id,
        caused_by=caused_by,
        event_id=event_id,
        idempotency_key=event_idempotency_key("zeta.model.called", event_id),
        timestamp_micros=timestamp_micros,
    )


def tool_called_event(
    *,
    payload: dict[str, Any],
    turn_id: str | None,
    session_id: str,
    caused_by: str | None = None,
    event_id: str | None = None,
    timestamp_micros: int | None = None,
) -> DraftEvent:
    return durable_event_draft(
        "zeta.tool.called",
        "zeta",
        payload=payload,
        turn_id=turn_id,
        session_id=session_id,
        caused_by=caused_by,
        event_id=event_id,
        idempotency_key=event_idempotency_key("zeta.tool.called", event_id),
        timestamp_micros=timestamp_micros,
    )


class DurableEventConstructors:
    """Factories for durable events with stable metadata."""

    def prompt_submitted(
        self,
        *,
        payload: dict[str, Any],
        turn_id: str | None,
        session_id: str,
        caused_by: str | None = None,
        event_id: str | None = None,
        timestamp_micros: int | None = None,
    ) -> DraftEvent:
        return durable_event_draft(
            "sigil.prompt.submitted",
            "sigil",
            payload=payload,
            turn_id=turn_id,
            session_id=session_id,
            caused_by=caused_by,
            event_id=event_id,
            idempotency_key=turn_idempotency_key("sigil.prompt.submitted", turn_id),
            timestamp_micros=timestamp_micros,
        )

    def turn_completed(
        self,
        *,
        payload: dict[str, Any],
        turn_id: str | None,
        session_id: str,
        caused_by: str | None = None,
        event_id: str | None = None,
        timestamp_micros: int | None = None,
    ) -> DraftEvent:
        return self._turn_event(
            "sigil.turn.completed",
            payload=payload,
            turn_id=turn_id,
            session_id=session_id,
            caused_by=caused_by,
            event_id=event_id,
            timestamp_micros=timestamp_micros,
        )

    def turn_failed(
        self,
        *,
        payload: dict[str, Any],
        turn_id: str | None,
        session_id: str,
        caused_by: str | None = None,
        event_id: str | None = None,
        timestamp_micros: int | None = None,
    ) -> DraftEvent:
        return self._turn_event(
            "sigil.turn.failed",
            payload=payload,
            turn_id=turn_id,
            session_id=session_id,
            caused_by=caused_by,
            event_id=event_id,
            timestamp_micros=timestamp_micros,
        )

    def turn_aborted(
        self,
        *,
        payload: dict[str, Any],
        turn_id: str | None,
        session_id: str,
        caused_by: str | None = None,
        event_id: str | None = None,
        timestamp_micros: int | None = None,
    ) -> DraftEvent:
        return self._turn_event(
            "sigil.turn.aborted",
            payload=payload,
            turn_id=turn_id,
            session_id=session_id,
            caused_by=caused_by,
            event_id=event_id,
            timestamp_micros=timestamp_micros,
        )

    def _turn_event(
        self,
        event_type: str,
        *,
        payload: dict[str, Any],
        turn_id: str | None,
        session_id: str,
        caused_by: str | None,
        event_id: str | None,
        timestamp_micros: int | None,
    ) -> DraftEvent:
        return durable_event_draft(
            event_type,
            "sigil",
            payload=payload,
            turn_id=turn_id,
            session_id=session_id,
            caused_by=caused_by,
            event_id=event_id,
            idempotency_key=turn_idempotency_key(event_type, turn_id),
            timestamp_micros=timestamp_micros,
        )


durable_event = DurableEventConstructors()


def durable_draft_from_payload(
    *,
    event_type: str,
    payload: dict[str, Any],
    turn_id: str | None,
    session_id: str,
    caused_by: str | None,
    event_id: str | None,
    timestamp_micros: int | None,
) -> DraftEvent | None:
    if event_type == "sigil.prompt.submitted":
        return durable_event.prompt_submitted(
            payload=payload,
            turn_id=turn_id,
            session_id=session_id,
            caused_by=caused_by,
            event_id=event_id,
            timestamp_micros=timestamp_micros,
        )
    if event_type == "sigil.turn.completed":
        return durable_event.turn_completed(
            payload=payload,
            turn_id=turn_id,
            session_id=session_id,
            caused_by=caused_by,
            event_id=event_id,
            timestamp_micros=timestamp_micros,
        )
    if event_type == "sigil.turn.failed":
        return durable_event.turn_failed(
            payload=payload,
            turn_id=turn_id,
            session_id=session_id,
            caused_by=caused_by,
            event_id=event_id,
            timestamp_micros=timestamp_micros,
        )
    if event_type == "sigil.turn.aborted":
        return durable_event.turn_aborted(
            payload=payload,
            turn_id=turn_id,
            session_id=session_id,
            caused_by=caused_by,
            event_id=event_id,
            timestamp_micros=timestamp_micros,
        )
    if event_type == "zeta.model.called":
        return model_called_event(
            payload=payload,
            turn_id=turn_id,
            session_id=session_id,
            caused_by=caused_by,
            event_id=event_id,
            timestamp_micros=timestamp_micros,
        )
    if event_type == "zeta.tool.called":
        return tool_called_event(
            payload=payload,
            turn_id=turn_id,
            session_id=session_id,
            caused_by=caused_by,
            event_id=event_id,
            timestamp_micros=timestamp_micros,
        )
    return None


def event_payload_draft(
    event: dict[str, Any],
    *,
    session_id: str,
    cwd: str | None = None,
) -> DraftEvent:
    payload = {"cwd": cwd or os.getcwd(), **event}
    event_id = payload.get("id") if isinstance(payload.get("id"), str) else None
    event_type = str(payload.get("type") or "event")
    turn_id = (
        payload.get("turn_id") if isinstance(payload.get("turn_id"), str) else None
    )
    event_session_id = str(payload.get("session") or session_id)
    event_timestamp = timestamp_micros_from_time(payload.get("time"))
    caused_by = (
        str(payload["caused_by"]) if isinstance(payload.get("caused_by"), str) else None
    )
    domain_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"id", "type", "time", "session", "source", "caused_by"}
    }
    draft = durable_draft_from_payload(
        event_type=event_type,
        payload=domain_payload,
        turn_id=turn_id,
        session_id=event_session_id,
        caused_by=caused_by,
        event_id=event_id,
        timestamp_micros=event_timestamp,
    )
    if draft is not None:
        return draft
    return DraftEvent(
        event_type=event_type,
        source=str(payload.get("source") or "sigil"),
        payload=domain_payload,
        caused_by=caused_by,
        session_id=event_session_id,
        turn_id=turn_id,
        timestamp_micros=event_timestamp,
        event_id=event_id,
    )


def publish_event_payload_to_log(
    path: Path | str,
    event: dict[str, Any],
    *,
    session_id: str,
    cwd: str | None = None,
) -> Event:
    return publish_event_to_log(
        path,
        event_payload_draft(event, session_id=session_id, cwd=cwd),
    )


class SqliteEventStore:
    """SQLite-backed event store."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if self.path != Path(":memory:"):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            str(self.path),
            timeout=5.0,
            check_same_thread=False,
        )
        self.connection.row_factory = sqlite3.Row
        execute_with_retry(self.connection, "PRAGMA busy_timeout=5000")
        execute_with_retry(self.connection, "PRAGMA case_sensitive_like=ON")
        if self.path != Path(":memory:"):
            execute_with_retry(self.connection, "PRAGMA journal_mode=WAL")
            execute_with_retry(self.connection, "PRAGMA synchronous=NORMAL")
        self._init_schema()

    def close(self) -> None:
        self.connection.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def _init_schema(self) -> None:
        self._ensure_events_table_has_sequence()
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
              seq INTEGER PRIMARY KEY AUTOINCREMENT,
              id TEXT UNIQUE NOT NULL,
              type TEXT NOT NULL,
              source TEXT NOT NULL,
              payload TEXT NOT NULL,
              idempotency_key TEXT,
              caused_by TEXT,
              session_id TEXT,
              turn_id TEXT,
              timestamp INTEGER NOT NULL
            ) STRICT;
            CREATE INDEX IF NOT EXISTS idx_events_type_ts
              ON events(type, timestamp);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_events_idempotency_key
              ON events(idempotency_key)
              WHERE idempotency_key IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_events_session_ts
              ON events(session_id, timestamp)
              WHERE session_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_events_session_seq
              ON events(session_id, seq)
              WHERE session_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_events_caused_by_ts
              ON events(caused_by, timestamp)
              WHERE caused_by IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_events_turn_ts
              ON events(turn_id, timestamp)
              WHERE turn_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_events_turn_seq
              ON events(turn_id, seq)
              WHERE turn_id IS NOT NULL;
            """
        )
        self.connection.commit()

    def _ensure_events_table_has_sequence(self) -> None:
        row = self.connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name = 'events'
            """
        ).fetchone()
        if row is None:
            return
        columns = {
            str(column["name"])
            for column in self.connection.execute("PRAGMA table_info(events)")
        }
        if "seq" in columns:
            return
        self.connection.executescript(
            """
            ALTER TABLE events RENAME TO events_legacy;
            CREATE TABLE events (
              seq INTEGER PRIMARY KEY AUTOINCREMENT,
              id TEXT UNIQUE NOT NULL,
              type TEXT NOT NULL,
              source TEXT NOT NULL,
              payload TEXT NOT NULL,
              idempotency_key TEXT,
              caused_by TEXT,
              session_id TEXT,
              turn_id TEXT,
              timestamp INTEGER NOT NULL
            ) STRICT;
            INSERT INTO events
              (id, type, source, payload, idempotency_key, caused_by, session_id,
               turn_id, timestamp)
            SELECT id, type, source, payload, idempotency_key, caused_by, session_id,
                   turn_id, timestamp
            FROM events_legacy
            ORDER BY timestamp ASC, id ASC;
            DROP TABLE events_legacy;
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
               turn_id, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                event.turn_id,
                event.timestamp_micros,
            ),
        )
        self.connection.commit()
        if cursor.rowcount == 1:
            inserted = self.get(event.id)
            if inserted is None:
                raise sqlite3.IntegrityError(f"append failed for event {event.id}")
            return AppendOutcome(event=inserted, inserted=True)
        return AppendOutcome(event=self._duplicate_for(event), inserted=False)

    def get(self, event_id: str) -> Event | None:
        row = self.connection.execute(
            """
            SELECT seq, id, type, source, payload, idempotency_key, caused_by,
                   session_id, turn_id, timestamp
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
        if filter.turn_id is not None:
            clauses.append("turn_id = ?")
            params.append(filter.turn_id)
        if filter.caused_by is not None:
            clauses.append("caused_by = ?")
            params.append(filter.caused_by)
        if filter.after is not None:
            if filter.after.seq is not None:
                clauses.append("seq > ?")
                params.append(filter.after.seq)
            else:
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
            SELECT seq, id, type, source, payload, idempotency_key, caused_by,
                   session_id, turn_id, timestamp
            FROM events
            {where}
            ORDER BY seq ASC
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
        return self.list_events(Filter(turn_id=turn_id))

    def clear_session_events(self, session_id: str, *, event_type_prefix: str) -> int:
        cursor = self.connection.execute(
            """
            DELETE FROM events
            WHERE session_id = ? AND type LIKE ? ESCAPE '\\'
            """,
            (session_id, like_prefix(event_type_prefix)),
        )
        self.connection.commit()
        return int(cursor.rowcount)

    def _duplicate_for(self, event: Event) -> Event:
        if event.idempotency_key is not None:
            row = self.connection.execute(
                """
                SELECT seq, id, type, source, payload, idempotency_key, caused_by,
                       session_id, turn_id, timestamp
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
                SELECT seq, id, type, source, payload, idempotency_key, caused_by,
                       session_id, turn_id, timestamp
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
        return root / ZETA_STORE_NAME
    state_dir = os.environ.get("ZETA_STATE_DIR")
    base = Path(state_dir).expanduser() if state_dir else Path.home() / ".zeta"
    return base / ZETA_STORE_NAME


def publish_event(draft: DraftEvent, *, sink: EventSink) -> AppendOutcome:
    return sink.accept(draft)


def read_event_log(path: Path | str, filter: Filter | None = None) -> list[Event]:
    store = SqliteEventStore(path)
    try:
        return store.list_events(filter or Filter())
    finally:
        store.close()


def publish_event_to_log(path: Path | str, draft: DraftEvent) -> Event:
    store = SqliteEventStore(path)
    try:
        return store.accept(draft).event
    finally:
        store.close()


def append_event_to_log(path: Path | str, event: Event) -> Event:
    return append_event_to_log_outcome(path, event).event


def append_event_to_log_outcome(path: Path | str, event: Event) -> AppendOutcome:
    store = SqliteEventStore(path)
    try:
        return store.append(event)
    finally:
        store.close()


def event_log_children(
    path: Path | str,
    event_id: str,
    *,
    limit: int | None = None,
) -> list[Event]:
    store = SqliteEventStore(path)
    try:
        return store.children(event_id, limit=limit)
    finally:
        store.close()


def event_log_causal_chain(path: Path | str, event_id: str) -> list[Event]:
    store = SqliteEventStore(path)
    try:
        return store.causal_chain(event_id)
    finally:
        store.close()


def event_log_turn_events(path: Path | str, turn_id: str) -> list[Event]:
    store = SqliteEventStore(path)
    try:
        return store.events_for_turn(turn_id)
    finally:
        store.close()


def row_to_event(row: sqlite3.Row) -> Event:
    payload = json.loads(str(row["payload"]))
    if not isinstance(payload, dict):
        payload = {"value": payload}
    seq = int(row["seq"]) if "seq" in row.keys() else 0
    return Event(
        id=str(row["id"]),
        event_type=str(row["type"]),
        source=str(row["source"]),
        payload=payload,
        idempotency_key=optional_str(row["idempotency_key"]),
        caused_by=optional_str(row["caused_by"]),
        session_id=optional_str(row["session_id"]),
        turn_id=optional_str(row["turn_id"]),
        timestamp_micros=int(row["timestamp"]),
        seq=seq,
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
