"""SQLite event store.

SQLite is the durable event log used by local runtimes. The schema keeps event
IDs, idempotency keys, causality, session scope, and append sequence in one
table so readers can replay stable slices without decoding payloads.
"""

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from zeta.events import AppendOutcome
from zeta.kernel.events import DraftEvent, Event
from zeta.store.events.filter import Filter

EVENT_STORE_NAME = "events.sqlite3"
ZETA_STORE_NAME = "zeta.sqlite3"


class SqliteEventStore:
    """Durable event store backed by a single SQLite database."""

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
        _execute_with_retry(self.connection, "PRAGMA busy_timeout=5000")
        _execute_with_retry(self.connection, "PRAGMA case_sensitive_like=ON")
        if self.path != Path(":memory:"):
            _execute_with_retry(self.connection, "PRAGMA journal_mode=WAL")
            _execute_with_retry(self.connection, "PRAGMA synchronous=NORMAL")
        self._init_schema()

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

            CREATE TABLE IF NOT EXISTS queue_items (
              queue_item_id TEXT PRIMARY KEY,
              event_id TEXT NOT NULL,
              target_agent TEXT NOT NULL,
              status TEXT NOT NULL,
              available_at INTEGER,
              claimed_by TEXT,
              claimed_until INTEGER,
              attempt_count INTEGER NOT NULL DEFAULT 0,
              last_error TEXT,
              updated_at INTEGER NOT NULL
            ) STRICT;

            CREATE TABLE IF NOT EXISTS attempts (
              attempt_id TEXT PRIMARY KEY,
              queue_item_id TEXT NOT NULL,
              event_id TEXT NOT NULL,
              attempt_number INTEGER NOT NULL,
              target_agent TEXT NOT NULL,
              worker_name TEXT,
              status TEXT NOT NULL,
              started_at TEXT NOT NULL,
              heartbeat_at INTEGER,
              finished_at TEXT,
              error TEXT,
              session_id TEXT,
              run_id TEXT
            ) STRICT;

            CREATE TABLE IF NOT EXISTS attempt_results (
              attempt_id TEXT PRIMARY KEY,
              final_status TEXT NOT NULL,
              summary TEXT,
              result_json TEXT,
              events_json TEXT,
              tool_calls_json TEXT,
              usage_json TEXT,
              finished_at TEXT
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
        columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(events)").fetchall()
        }
        if "run_id" not in columns:
            self.connection.execute("ALTER TABLE events ADD COLUMN run_id TEXT")
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
        self.connection.commit()

    def accept(self, draft: DraftEvent) -> AppendOutcome:
        return self.append(Event.from_draft(draft))

    def append(self, event: Event) -> AppendOutcome:
        payload = json.dumps(
            dict(event.payload), ensure_ascii=False, separators=(",", ":")
        )
        cursor = self.connection.execute(
            """
            INSERT INTO events
              (id, type, source, payload, idempotency_key, caused_by, session_id,
               run_id, turn_id, timestamp)
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
        self.connection.commit()
        if cursor.rowcount == 1:
            inserted = self.get(event.id)
            if inserted is None:
                raise sqlite3.IntegrityError(f"append failed for event {event.id}")
            self._project_runtime_event(inserted)
            self.connection.commit()
            return AppendOutcome(event=inserted, inserted=True)
        return AppendOutcome(event=self._duplicate_for(event), inserted=False)

    def _project_runtime_event(self, event: Event) -> None:
        if event.event_type.startswith("runtime.queue_item."):
            self._project_queue_item_event(event)
            return
        if event.event_type.startswith("runtime.attempt."):
            self._project_attempt_event(event)

    def _project_queue_item_event(self, event: Event) -> None:
        queue_item_id = _payload_str(event, "queue_item_id")
        event_id = _payload_str(event, "event_id")
        target_agent = _payload_str(event, "target_agent")
        if queue_item_id is None or event_id is None or target_agent is None:
            return
        status = _runtime_status(event)
        self.connection.execute(
            """
            INSERT INTO queue_items
              (queue_item_id, event_id, target_agent, status, available_at,
               last_error, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(queue_item_id) DO UPDATE SET
              event_id = excluded.event_id,
              target_agent = excluded.target_agent,
              status = excluded.status,
              available_at = COALESCE(queue_items.available_at, excluded.available_at),
              last_error = excluded.last_error,
              updated_at = excluded.updated_at
            """,
            (
                queue_item_id,
                event_id,
                target_agent,
                status,
                event.timestamp_ms if status == "available" else None,
                _payload_str(event, "error"),
                event.timestamp_ms,
            ),
        )

    def _project_attempt_event(self, event: Event) -> None:
        attempt_id = _payload_str(event, "attempt_id")
        queue_item_id = _payload_str(event, "queue_item_id")
        event_id = _payload_str(event, "event_id")
        target_agent = _payload_str(event, "target_agent")
        started_at = _payload_str(event, "started_at")
        attempt_number = _payload_int(event, "attempt_number")
        if (
            attempt_id is None
            or queue_item_id is None
            or event_id is None
            or target_agent is None
            or started_at is None
            or attempt_number is None
        ):
            return
        status = _runtime_status(event)
        self.connection.execute(
            """
            INSERT INTO attempts
              (attempt_id, queue_item_id, event_id, attempt_number, target_agent,
               worker_name, status, started_at, heartbeat_at, finished_at, error,
               session_id, run_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(attempt_id) DO UPDATE SET
              status = excluded.status,
              heartbeat_at = excluded.heartbeat_at,
              finished_at = excluded.finished_at,
              error = excluded.error,
              session_id = excluded.session_id,
              run_id = excluded.run_id
            """,
            (
                attempt_id,
                queue_item_id,
                event_id,
                attempt_number,
                target_agent,
                _payload_str(event, "worker_name"),
                status,
                started_at,
                event.timestamp_ms,
                _payload_str(event, "finished_at"),
                _payload_str(event, "error"),
                event.session_id,
                event.run_id,
            ),
        )
        if status == "running":
            self.connection.execute(
                """
                UPDATE queue_items
                SET attempt_count = CASE
                  WHEN attempt_count < ? THEN ?
                  ELSE attempt_count
                END
                WHERE queue_item_id = ?
                """,
                (attempt_number, attempt_number, queue_item_id),
            )
        if status in {"completed", "failed", "cancelled"}:
            self._project_attempt_result(event, attempt_id, status)

    def _project_attempt_result(
        self,
        event: Event,
        attempt_id: str,
        status: str,
    ) -> None:
        result = event.payload.get("result")
        result_json = None
        if result is not None:
            result_json = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        self.connection.execute(
            """
            INSERT INTO attempt_results
              (attempt_id, final_status, summary, result_json, events_json,
               tool_calls_json, usage_json, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(attempt_id) DO UPDATE SET
              final_status = excluded.final_status,
              summary = excluded.summary,
              result_json = excluded.result_json,
              events_json = excluded.events_json,
              tool_calls_json = excluded.tool_calls_json,
              usage_json = excluded.usage_json,
              finished_at = excluded.finished_at
            """,
            (
                attempt_id,
                status,
                _payload_str(event, "summary"),
                result_json,
                _payload_json(event, "events"),
                _payload_json(event, "tool_calls"),
                _payload_json(event, "usage"),
                _payload_str(event, "finished_at"),
            ),
        )

    def claim_next_queue_item(
        self,
        worker_name: str,
        *,
        lease_ms: int,
        now_ms: int,
    ) -> str | None:
        _execute_with_retry(self.connection, "BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT queue_item_id
                FROM queue_items
                WHERE status = 'available'
                  AND (available_at IS NULL OR available_at <= ?)
                ORDER BY available_at ASC, queue_item_id ASC
                LIMIT 1
                """,
                (now_ms,),
            ).fetchone()
            if row is None:
                self.connection.commit()
                return None
            queue_item_id = str(row["queue_item_id"])
            cursor = self.connection.execute(
                """
                UPDATE queue_items
                SET status = 'claimed',
                    claimed_by = ?,
                    claimed_until = ?,
                    updated_at = ?
                WHERE queue_item_id = ?
                  AND status = 'available'
                  AND (available_at IS NULL OR available_at <= ?)
                """,
                (
                    worker_name,
                    now_ms + lease_ms,
                    now_ms,
                    queue_item_id,
                    now_ms,
                ),
            )
            self.connection.commit()
            if cursor.rowcount != 1:
                return None
            return queue_item_id
        except Exception:
            self.connection.rollback()
            raise

    def reconcile_expired_queue_claims(self, *, now_ms: int) -> int:
        cursor = self.connection.execute(
            """
            UPDATE queue_items
            SET status = 'available',
                claimed_by = NULL,
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


def _payload_str(event: Event, key: str) -> str | None:
    value = event.payload.get(key)
    if isinstance(value, str):
        return value
    return None


def _payload_int(event: Event, key: str) -> int | None:
    value = event.payload.get(key)
    if isinstance(value, int):
        return value
    return None


def _payload_json(event: Event, key: str) -> str | None:
    value = event.payload.get(key)
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _runtime_status(event: Event) -> str:
    status = _payload_str(event, "status")
    if status is not None:
        return status
    if event.event_type == "runtime.attempt.started":
        return "running"
    return event.event_type.rsplit(".", 1)[-1]


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _like_prefix(prefix: str) -> str:
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}%"


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
