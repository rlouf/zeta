"""SQLite event store.

SQLite is the durable event log used by local runtimes. The schema keeps event
IDs, idempotency keys, causality, session scope, and append sequence in one
table so readers can replay stable slices without decoding payloads.
"""

import json
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from zeta.records.events import AppendOutcome, DraftEvent, Event, json_native_payload
from zeta.records.stores._object_sqlite import (
    DEFAULT_SQLITE_NAME,
    ZETA_SQLITE_NAME,
    SqliteObjectStore,
    available_session_ids,
    default_sqlite_path,
    export_trace_refs,
    import_trace_graph,
    open_existing_trace_store,
    open_trace_store,
    trace_state_dir,
    zeta_sqlite_path,
)
from zeta.records.stores.event_store import Filter

__all__ = [
    "DEFAULT_SQLITE_NAME",
    "EVENT_STORE_NAME",
    "EventProjection",
    "QueueClaim",
    "ZETA_SQLITE_NAME",
    "ZETA_STORE_NAME",
    "SqliteEventStore",
    "SqliteObjectStore",
    "available_session_ids",
    "default_sqlite_path",
    "event_store_path",
    "export_trace_refs",
    "import_trace_graph",
    "open_existing_trace_store",
    "open_trace_store",
    "trace_state_dir",
    "zeta_sqlite_path",
]

EVENT_STORE_NAME = "events.sqlite3"
ZETA_STORE_NAME = "zeta.sqlite3"


@dataclass(frozen=True)
class QueueClaim:
    """Opaque ownership token for one active queue claim."""

    queue_item_id: str
    token: str


class EventProjection(Protocol):
    """Maintains derived SQLite state for one class of durable events."""

    def init_schema(self, connection: sqlite3.Connection) -> None:
        """Create the projection schema without mutating existing columns."""

    def clear(self, connection: sqlite3.Connection) -> None:
        """Clear projected rows before rebuilding from durable events."""

    def index(self, connection: sqlite3.Connection, event: Event) -> None:
        """Project one durable event into derived tables."""


class SqliteEventStore:
    """Durable event store backed by a single SQLite database."""

    def __init__(
        self,
        path: Path | str,
        *,
        projections: Iterable[EventProjection] = (),
    ) -> None:
        self.path = Path(path)
        self._projections = tuple(projections)
        if self.path != Path(":memory:"):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            str(self.path),
            timeout=5.0,
            check_same_thread=False,
        )
        self.connection.row_factory = sqlite3.Row
        _execute_with_retry(self.connection, "PRAGMA busy_timeout=5000")
        _execute_with_retry(self.connection, "PRAGMA case_sensitive_like=ON")
        if self.path != Path(":memory:"):
            _execute_with_retry(self.connection, "PRAGMA journal_mode=WAL")
            _execute_with_retry(self.connection, "PRAGMA synchronous=NORMAL")
        self._write_lock = threading.RLock()
        self._init_schema()

    def close(self) -> None:
        self.connection.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def _init_schema(self) -> None:
        with self._write_lock:
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
                  run_id TEXT,
                  turn_id TEXT,
                  timestamp INTEGER NOT NULL
                ) STRICT;

                CREATE TABLE IF NOT EXISTS session_mappings (
                  session_id TEXT PRIMARY KEY,
                  run_id TEXT,
                  updated_at INTEGER NOT NULL
                ) STRICT;

                CREATE TABLE IF NOT EXISTS locks (
                  key TEXT PRIMARY KEY,
                  owner TEXT NOT NULL,
                  acquired_at INTEGER NOT NULL,
                  expires_at INTEGER NOT NULL
                ) STRICT;
                """
            )
            self.connection.executescript(
                """
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
                CREATE INDEX IF NOT EXISTS idx_events_run_ts
                  ON events(run_id, timestamp)
                  WHERE run_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_events_run_seq
                  ON events(run_id, seq)
                  WHERE run_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_events_turn_ts
                  ON events(turn_id, timestamp)
                  WHERE turn_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_events_turn_seq
                  ON events(turn_id, seq)
                  WHERE turn_id IS NOT NULL;
                """
            )
            for projection in self._projections:
                projection.init_schema(self.connection)
            self.connection.commit()

    def accept(self, draft: DraftEvent) -> AppendOutcome:
        return self.append(Event.from_draft(draft))

    def append(self, event: Event) -> AppendOutcome:
        payload = json.dumps(
            json_native_payload(event.payload),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._write_lock:
            _execute_with_retry(self.connection, "BEGIN IMMEDIATE")
            try:
                cursor = self.connection.execute(
                    """
                    INSERT INTO events
                      (id, type, source, payload, idempotency_key, caused_by,
                       session_id, run_id, turn_id, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        event.run_id,
                        event.turn_id,
                        event.timestamp_ms,
                    ),
                )
                if cursor.rowcount != 1:
                    self.connection.commit()
                    return AppendOutcome(
                        event=self._duplicate_for(event), inserted=False
                    )
                inserted = self.get(event.id)
                if inserted is None:
                    raise sqlite3.IntegrityError(f"append failed for event {event.id}")
                self._index_one_session_mapping(inserted)
                self._index_one_runtime_event(inserted)
                self.connection.commit()
                return AppendOutcome(event=inserted, inserted=True)
            except Exception:
                self.connection.rollback()
                raise

    def _index_one_session_mapping(self, event: Event) -> None:
        if event.session_id is None or event.run_id is None:
            return
        self.connection.execute(
            """
            INSERT INTO session_mappings (session_id, run_id, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
              run_id = excluded.run_id,
              updated_at = excluded.updated_at
            """,
            (event.session_id, event.run_id, event.timestamp_ms),
        )

    def _index_one_runtime_event(self, event: Event) -> None:
        for projection in self._projections:
            projection.index(self.connection, event)

    def rebuild_projections(self) -> int:
        with self._write_lock:
            _execute_with_retry(self.connection, "BEGIN IMMEDIATE")
            try:
                events = self.list_events(Filter())
                for projection in self._projections:
                    projection.clear(self.connection)
                self.connection.execute("DELETE FROM session_mappings")
                for event in events:
                    self._index_one_session_mapping(event)
                    self._index_one_runtime_event(event)
                self.connection.commit()
                return len(events)
            except Exception:
                self.connection.rollback()
                raise

    def ensure_pending_queue_item(self, event: Event) -> str:
        queue_item_id = _pending_queue_item_id(event)
        with self._write_lock:
            self.connection.execute(
                """
                INSERT INTO queue_items
                  (queue_item_id, event_id, target_agent, status, available_at,
                   updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(queue_item_id) DO NOTHING
                """,
                (
                    queue_item_id,
                    event.id,
                    "",
                    "pending",
                    event.timestamp_ms,
                    event.timestamp_ms,
                ),
            )
            self.connection.commit()
        return queue_item_id

    def event_has_queue_item(self, event_id: str) -> bool:
        row = self.connection.execute(
            """
            SELECT 1
            FROM queue_items
            WHERE event_id = ?
            LIMIT 1
            """,
            (event_id,),
        ).fetchone()
        return row is not None

    def queue_item(self, queue_item_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT queue_item_id, event_id, target_agent, status
            FROM queue_items
            WHERE queue_item_id = ?
            """,
            (queue_item_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_queue_items(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT queue_item_id, event_id, target_agent, status, available_at,
                   claimed_by, claimed_until, attempt_count, last_error, updated_at
            FROM queue_items
            ORDER BY updated_at ASC, queue_item_id ASC
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def list_attempts(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT a.attempt_id, a.queue_item_id, a.event_id, a.attempt_number,
                   a.target_agent, a.worker_name, a.status, a.started_at,
                   a.heartbeat_at, a.finished_at, a.error, a.session_id, a.run_id,
                   COALESCE(a.summary, r.summary) AS summary,
                   a.input_tokens, a.output_tokens,
                   COALESCE(a.tool_calls_json, r.tool_calls_json) AS tool_calls_json,
                   r.final_status, r.result_json, r.events_json, r.usage_json
            FROM attempts a
            LEFT JOIN attempt_results r ON r.attempt_id = a.attempt_id
            ORDER BY a.started_at ASC, a.attempt_id ASC
            """
        ).fetchall()
        return [_row_to_attempt(row) for row in rows]

    def list_locks(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT key, owner, acquired_at, expires_at
            FROM locks
            ORDER BY key ASC
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def acquire_locks(
        self,
        keys: Iterable[str],
        owner: str,
        *,
        lease_ms: int,
        now_ms: int,
    ) -> bool:
        requested = tuple(dict.fromkeys(keys))
        if not requested:
            return True
        placeholders = _sql_placeholders(requested)
        with self._write_lock:
            _execute_with_retry(self.connection, "BEGIN IMMEDIATE")
            try:
                self.connection.execute(
                    "DELETE FROM locks WHERE expires_at < ?",
                    (now_ms,),
                )
                conflict = self.connection.execute(
                    f"""
                    SELECT key
                    FROM locks
                    WHERE key IN ({placeholders})
                      AND owner != ?
                      AND expires_at >= ?
                    LIMIT 1
                    """,
                    (*requested, owner, now_ms),
                ).fetchone()
                if conflict is not None:
                    self.connection.rollback()
                    return False
                for key in requested:
                    self.connection.execute(
                        """
                        INSERT INTO locks
                          (key, owner, acquired_at, expires_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(key) DO UPDATE SET
                          owner = excluded.owner,
                          acquired_at = excluded.acquired_at,
                          expires_at = excluded.expires_at
                        WHERE locks.owner = excluded.owner
                           OR locks.expires_at < ?
                        """,
                        (key, owner, now_ms, now_ms + lease_ms, now_ms),
                    )
                self.connection.commit()
                return True
            except Exception:
                self.connection.rollback()
                raise

    def release_locks(self, keys: Iterable[str], owner: str) -> int:
        requested = tuple(dict.fromkeys(keys))
        if not requested:
            return 0
        placeholders = _sql_placeholders(requested)
        with self._write_lock:
            cursor = self.connection.execute(
                f"""
                DELETE FROM locks
                WHERE owner = ?
                  AND key IN ({placeholders})
                """,
                (owner, *requested),
            )
            self.connection.commit()
        return int(cursor.rowcount)

    def reconcile_expired_locks(self, *, now_ms: int) -> int:
        with self._write_lock:
            cursor = self.connection.execute(
                """
                DELETE FROM locks
                WHERE expires_at < ?
                """,
                (now_ms,),
            )
            self.connection.commit()
        return int(cursor.rowcount)

    def heartbeat_attempt(
        self,
        attempt_id: str,
        queue_item_id: str,
        worker_name: str,
        *,
        claim_token: str,
        lease_ms: int,
        now_ms: int,
    ) -> bool:
        with self._write_lock:
            _execute_with_retry(self.connection, "BEGIN IMMEDIATE")
            try:
                cursor = self.connection.execute(
                    """
                    UPDATE attempts
                    SET heartbeat_at = ?
                    WHERE attempt_id = ?
                      AND queue_item_id = ?
                      AND worker_name = ?
                      AND claim_token = ?
                      AND status = 'running'
                      AND EXISTS (
                        SELECT 1
                        FROM queue_items
                        WHERE queue_item_id = ?
                          AND claimed_by = ?
                          AND claimed_token = ?
                          AND status = 'claimed'
                      )
                    """,
                    (
                        now_ms,
                        attempt_id,
                        queue_item_id,
                        worker_name,
                        claim_token,
                        queue_item_id,
                        worker_name,
                        claim_token,
                    ),
                )
                if cursor.rowcount != 1:
                    self.connection.rollback()
                    return False
                self.connection.execute(
                    """
                    UPDATE queue_items
                    SET claimed_until = ?,
                        updated_at = ?
                    WHERE queue_item_id = ?
                      AND claimed_by = ?
                      AND claimed_token = ?
                      AND status = 'claimed'
                    """,
                    (
                        now_ms + lease_ms,
                        now_ms,
                        queue_item_id,
                        worker_name,
                        claim_token,
                    ),
                )
                self.connection.commit()
                return True
            except Exception:
                self.connection.rollback()
                raise

    def claim_next_queue_item(
        self,
        worker_name: str,
        *,
        lease_ms: int,
        now_ms: int,
        exclude_queue_item_ids: Iterable[str] = (),
    ) -> QueueClaim | None:
        excluded = tuple(dict.fromkeys(exclude_queue_item_ids))
        excluded_clause = ""
        excluded_params: tuple[str, ...] = ()
        if excluded:
            excluded_clause = (
                f"AND queue_item_id NOT IN ({_sql_placeholders(excluded)})"
            )
            excluded_params = excluded
        with self._write_lock:
            _execute_with_retry(self.connection, "BEGIN IMMEDIATE")
            try:
                row = self.connection.execute(
                    f"""
                    SELECT queue_item_id
                    FROM queue_items
                    WHERE status IN ('pending', 'available')
                      AND (available_at IS NULL OR available_at <= ?)
                      {excluded_clause}
                    ORDER BY available_at ASC, queue_item_id ASC
                    LIMIT 1
                    """,
                    (now_ms, *excluded_params),
                ).fetchone()
                if row is None:
                    self.connection.commit()
                    return None
                queue_item_id = str(row["queue_item_id"])
                claim_token = secrets.token_urlsafe(24)
                cursor = self.connection.execute(
                    """
                    UPDATE queue_items
                    SET status = 'claimed',
                        claimed_by = ?,
                        claimed_token = ?,
                        claimed_until = ?,
                        updated_at = ?
                    WHERE queue_item_id = ?
                      AND status IN ('pending', 'available')
                      AND (available_at IS NULL OR available_at <= ?)
                    """,
                    (
                        worker_name,
                        claim_token,
                        now_ms + lease_ms,
                        now_ms,
                        queue_item_id,
                        now_ms,
                    ),
                )
                self.connection.commit()
                if cursor.rowcount != 1:
                    return None
                return QueueClaim(queue_item_id, claim_token)
            except Exception:
                self.connection.rollback()
                raise

    def release_queue_claim(
        self,
        queue_item_id: str,
        worker_name: str,
        *,
        claim_token: str,
        now_ms: int,
    ) -> bool:
        with self._write_lock:
            cursor = self.connection.execute(
                """
                UPDATE queue_items
                SET status = CASE
                      WHEN target_agent = '' THEN 'pending'
                      ELSE 'available'
                    END,
                    claimed_by = NULL,
                    claimed_token = NULL,
                    claimed_until = NULL,
                    updated_at = ?
                WHERE queue_item_id = ?
                  AND claimed_by = ?
                  AND claimed_token = ?
                  AND status = 'claimed'
                """,
                (now_ms, queue_item_id, worker_name, claim_token),
            )
            self.connection.commit()
        return cursor.rowcount == 1

    def queue_claim_is_current(
        self,
        queue_item_id: str,
        worker_name: str,
        claim_token: str,
    ) -> bool:
        row = self.connection.execute(
            """
            SELECT 1
            FROM queue_items
            WHERE queue_item_id = ?
              AND claimed_by = ?
              AND claimed_token = ?
              AND status = 'claimed'
            LIMIT 1
            """,
            (queue_item_id, worker_name, claim_token),
        ).fetchone()
        return row is not None

    def reconcile_expired_queue_claims(self, *, now_ms: int) -> int:
        with self._write_lock:
            cursor = self.connection.execute(
                """
                UPDATE queue_items
                SET status = CASE
                      WHEN target_agent = '' THEN 'pending'
                      ELSE 'available'
                    END,
                    claimed_by = NULL,
                    claimed_token = NULL,
                    claimed_until = NULL,
                    updated_at = ?
                WHERE status = 'claimed'
                  AND claimed_until IS NOT NULL
                  AND claimed_until < ?
                """,
                (now_ms, now_ms),
            )
            self.connection.commit()
        return int(cursor.rowcount)

    def get(self, event_id: str) -> Event | None:
        row = self.connection.execute(
            """
            SELECT seq, id, type, source, payload, idempotency_key, caused_by,
                   session_id, run_id, turn_id, timestamp
            FROM events
            WHERE id = ?
            """,
            (event_id,),
        ).fetchone()
        return _row_to_event(row) if row is not None else None

    def list_events(self, filter: Filter) -> list[Event]:
        clauses: list[str] = []
        params: list[Any] = []
        if filter.event_type is not None:
            clauses.append("type = ?")
            params.append(filter.event_type)
        if filter.event_type_prefix is not None:
            clauses.append("type LIKE ? ESCAPE '\\'")
            params.append(_like_prefix(filter.event_type_prefix))
        if filter.session_id is not None:
            clauses.append("session_id = ?")
            params.append(filter.session_id)
        if filter.run_id is not None:
            clauses.append("run_id = ?")
            params.append(filter.run_id)
        if filter.turn_id is not None:
            clauses.append("turn_id = ?")
            params.append(filter.turn_id)
        if filter.caused_by is not None:
            clauses.append("caused_by = ?")
            params.append(filter.caused_by)
        if filter.after_cursor is not None:
            clauses.append("seq > ?")
            params.append(filter.after_cursor)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit = ""
        if filter.limit is not None:
            limit = "LIMIT ?"
            params.append(filter.limit)
        rows = self.connection.execute(
            f"""
            SELECT seq, id, type, source, payload, idempotency_key, caused_by,
                   session_id, run_id, turn_id, timestamp
            FROM events
            {where}
            ORDER BY seq ASC
            {limit}
            """,
            params,
        ).fetchall()
        return [_row_to_event(row) for row in rows]

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
        with self._write_lock:
            cursor = self.connection.execute(
                """
                DELETE FROM events
                WHERE session_id = ? AND type LIKE ? ESCAPE '\\'
                """,
                (session_id, _like_prefix(event_type_prefix)),
            )
            self.connection.commit()
        return int(cursor.rowcount)

    def _duplicate_for(self, event: Event) -> Event:
        if event.idempotency_key is not None:
            row = self.connection.execute(
                """
                SELECT seq, id, type, source, payload, idempotency_key, caused_by,
                       session_id, run_id, turn_id, timestamp
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
                       session_id, run_id, turn_id, timestamp
                FROM events
                WHERE id = ?
                """,
                (event.id,),
            ).fetchone()
        if row is None:
            raise sqlite3.IntegrityError(f"append conflict for event {event.id}")
        return _row_to_event(row)


def event_store_path(root: Path | None = None) -> Path:
    if root is not None:
        return root / ZETA_STORE_NAME
    state_dir = os.environ.get("ZETA_STATE_DIR")
    base = Path(state_dir).expanduser() if state_dir else Path.home() / ".zeta"
    return base / ZETA_STORE_NAME


def _row_to_event(row: sqlite3.Row) -> Event:
    payload = json.loads(str(row["payload"]))
    if not isinstance(payload, dict):
        payload = {"value": payload}
    return Event(
        id=str(row["id"]),
        event_type=str(row["type"]),
        source=str(row["source"]),
        payload=dict(payload),
        idempotency_key=_optional_str(row["idempotency_key"]),
        caused_by=_optional_str(row["caused_by"]),
        session_id=_optional_str(row["session_id"]),
        run_id=_optional_str(row["run_id"]),
        turn_id=_optional_str(row["turn_id"]),
        timestamp_ms=int(row["timestamp"]),
        cursor=int(row["seq"]),
    )


def _row_to_attempt(row: sqlite3.Row) -> dict[str, Any]:
    usage = _json_column(row["usage_json"])
    return {
        "attempt_id": str(row["attempt_id"]),
        "queue_item_id": str(row["queue_item_id"]),
        "event_id": str(row["event_id"]),
        "attempt_number": int(row["attempt_number"]),
        "target_agent": str(row["target_agent"]),
        "worker_name": _optional_str(row["worker_name"]),
        "status": str(row["status"]),
        "started_at": str(row["started_at"]),
        "heartbeat_at": row["heartbeat_at"],
        "finished_at": _optional_str(row["finished_at"]),
        "error": _optional_str(row["error"]),
        "session_id": _optional_str(row["session_id"]),
        "run_id": _optional_str(row["run_id"]),
        "input_tokens": _row_token_count(row["input_tokens"], usage, "input_tokens"),
        "output_tokens": _row_token_count(row["output_tokens"], usage, "output_tokens"),
        "final_status": _optional_str(row["final_status"]),
        "summary": _optional_str(row["summary"]),
        "result": _json_column(row["result_json"]),
        "events": _json_column(row["events_json"]),
        "tool_calls": _json_column(row["tool_calls_json"]),
        "usage": usage,
    }


def _row_token_count(
    value: Any,
    usage: Any,
    key: str,
) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(usage, dict):
        token_count = usage.get(key)
        if isinstance(token_count, int):
            return token_count
    return None


def _pending_queue_item_id(event: Event) -> str:
    return f"qi_{event.id}"


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _json_column(value: object) -> Any | None:
    if not isinstance(value, str):
        return None
    return json.loads(value)


def _like_prefix(prefix: str) -> str:
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}%"


def _sql_placeholders(values: tuple[object, ...]) -> str:
    return ", ".join("?" for _ in values)


def _execute_with_retry(connection: sqlite3.Connection, sql: str) -> None:
    deadline = time.monotonic() + 5
    while True:
        try:
            connection.execute(sql)
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)
