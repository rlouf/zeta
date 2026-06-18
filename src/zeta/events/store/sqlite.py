"""SQLite event store."""

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from ..event import AppendOutcome, DraftEvent, Event
from .base import Filter

EVENT_STORE_NAME = "events.sqlite3"
ZETA_STORE_NAME = "zeta.sqlite3"


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
