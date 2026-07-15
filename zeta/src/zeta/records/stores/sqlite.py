"""SQLite event store.

SQLite is the durable event log used by local runtimes. The schema keeps event
IDs, idempotency keys, causality, session scope, and append sequence in one
table so readers can replay stable slices without decoding payloads.
"""

import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterable, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Protocol

from zeta.records.events import AppendOutcome, DraftEvent, Event, json_native_payload
from zeta.records.stores.event_store import Filter
from zeta.substrate.objects import Derivation, Object
from zeta.substrate.sqlite import SqliteObjectStore
from zeta.substrate.store import escape_like

__all__ = [
    "EVENT_STORE_NAME",
    "EventProjection",
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

ZETA_STORE_NAME = "zeta.sqlite3"
EVENT_STORE_NAME = ZETA_STORE_NAME
ZETA_SQLITE_NAME = ZETA_STORE_NAME


class UnknownSessionError(LookupError):
    """A session id named no recorded trace store."""

    def __init__(self, session_id: str, available: list[str]) -> None:
        super().__init__(session_id)
        self.session_id = session_id
        self.available = available


def trace_state_dir() -> Path:
    root = os.environ.get("ZETA_STATE_DIR")
    return Path(root).expanduser() if root else Path.home() / ".zeta"


def zeta_sqlite_path(root: Path | None = None) -> Path:
    """Return the unified Zeta SQLite store path."""
    return (root or trace_state_dir()) / ZETA_SQLITE_NAME


def default_sqlite_path() -> Path:
    """Return the default unified Zeta SQLite path."""
    return zeta_sqlite_path()


def available_session_ids(root: Path | None = None) -> list[str]:
    """Return the session ids recorded in the unified Zeta store, sorted."""
    path = zeta_sqlite_path(root)
    if not path.exists():
        return []
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        sessions: set[str] = set()
        try:
            rows = connection.execute(
                "SELECT DISTINCT session_id FROM derivations WHERE session_id IS NOT NULL"
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        sessions.update(str(row["session_id"]) for row in rows)
        rows = connection.execute(
            """
            SELECT DISTINCT substr(scope, 9) AS session_id
            FROM refs
            WHERE scope LIKE 'session/%'
            """
        ).fetchall()
        sessions.update(str(row["session_id"]) for row in rows)
        return sorted(session for session in sessions if session)
    finally:
        connection.close()


def open_trace_store(
    session_id: str,
    *,
    read_only: bool = False,
    root: Path | None = None,
) -> SqliteObjectStore:
    """Open the unified Zeta trace store for one session."""
    return SqliteObjectStore(
        zeta_sqlite_path(root), session_id=session_id, read_only=read_only
    )


def open_existing_trace_store(
    session_id: str,
    *,
    read_only: bool = True,
    root: Path | None = None,
) -> SqliteObjectStore:
    """Open a recorded session trace store or raise with known sessions."""
    available = available_session_ids(root)
    if session_id not in available:
        raise UnknownSessionError(session_id, available)
    return open_trace_store(session_id, read_only=read_only, root=root)


def export_trace_refs(
    session_id: str,
    refs: Sequence[str],
    *,
    root: Path | None = None,
) -> dict[str, Any] | None:
    """Export the trace closure for refs in one session, or None."""
    try:
        store = open_existing_trace_store(session_id, read_only=True, root=root)
    except UnknownSessionError:
        return None
    try:
        resolved_refs: dict[str, str] = {}
        for name in refs:
            target = store.get_ref(name)
            if target is not None:
                resolved_refs[name] = target.object_id
        if not resolved_refs:
            return None
        closure = store.graph_closure(list(resolved_refs.values()))
        objects = [
            {
                "id": object_id_value,
                "kind": obj.kind,
                "schema": obj.schema,
                "data": obj.data,
                "links": list(obj.links),
            }
            for object_id_value, obj in closure.items()
        ]
        derivations: list[dict[str, Any]] = []
        seen: set[str] = set()
        for object_id_value in closure:
            for row in store.derivation_records_for_output(object_id_value):
                if row["id"] in seen:
                    continue
                seen.add(row["id"])
                derivations.append(row)
        return {"objects": objects, "derivations": derivations, "refs": resolved_refs}
    finally:
        store.close()


def import_trace_graph(
    session_id: str,
    graph: dict[str, Any],
    *,
    root: Path | None = None,
) -> int:
    """Import exported trace objects, derivations, and refs into a session."""
    store = open_trace_store(session_id, root=root)
    count = 0
    try:
        with store.batch():
            for entry in graph.get("objects") or []:
                store.import_object(
                    str(entry["id"]),
                    Object(
                        kind=str(entry["kind"]),
                        schema=str(entry["schema"]),
                        data=entry["data"],
                        links=tuple(entry["links"]),
                    ),
                )
                count += 1
            for row in graph.get("derivations") or []:
                store.import_derivation(
                    str(row["id"]),
                    Derivation(
                        producer=str(row["producer"]),
                        output_id=str(row["output_id"]),
                        input_ids=tuple(row["input_ids"]),
                        params=row["params"],
                    ),
                    float(row["created_at"]),
                )
            for name, object_id_value in (graph.get("refs") or {}).items():
                current = store.get_ref(str(name))
                expected = current.object_id if current is not None else None
                store.move_ref(str(name), expected, str(object_id_value))
    finally:
        store.close()
    return count


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

    @property
    def write_lock(self) -> AbstractContextManager[bool]:
        """Serialize writes; hold across a read-modify-write on this store.

        The public handle onto the store's write serialization so orchestration
        state co-located in this database can share the lock without depending
        on the store's internal concurrency implementation.
        """
        return self._write_lock

    def begin_immediate(self) -> None:
        """Open an immediate write transaction, retrying transient locks."""
        _execute_with_retry(self.connection, "BEGIN IMMEDIATE")

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

    def get(self, event_id: str) -> Event | None:
        with self._write_lock:
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
        with self._write_lock:
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
        with self._write_lock:
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


def resolve_state_dir(project_root: Path, state_dir: Path | None) -> Path:
    """Resolve the runtime state directory shared by workers and CLI readers.

    An explicit ``state_dir`` always wins. A non-default ``project_root`` keeps
    state under ``<project_root>/.zeta``. Otherwise fall back to ``ZETA_STATE_DIR``
    or ``~/.zeta`` so the worker and the inspection commands agree by default.
    """
    if state_dir is not None:
        return state_dir.expanduser()
    if project_root != Path("."):
        return project_root.expanduser().resolve() / ".zeta"
    env_state_dir = os.environ.get("ZETA_STATE_DIR")
    if env_state_dir:
        return Path(env_state_dir).expanduser()
    return Path.home() / ".zeta"


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


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _like_prefix(prefix: str) -> str:
    return f"{escape_like(prefix)}%"


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
