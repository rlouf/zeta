"""Content-addressed object graph for Zeta prompt traces."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

ObjectId = str
DEFAULT_SQLITE_NAME = "zeta-trace.sqlite3"
ZETA_SQLITE_NAME = "zeta.sqlite3"
LOGGER = logging.getLogger("zeta.trace")
_WARNED_FAILURES: set[str] = set()
_REF_EXPECTED_UNSET = object()


@dataclass(frozen=True)
class Object:
    """Content-addressed object with ordered links to other objects."""

    kind: str
    schema: str
    data: dict[str, Any] = field(default_factory=dict)
    links: tuple[ObjectId, ...] = ()


@dataclass(frozen=True)
class Derivation:
    """Record how a trace object was produced."""

    producer: str
    output_id: ObjectId
    input_ids: tuple[ObjectId, ...] = ()
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PromptTrace:
    """Trace ids for one prompt request and its assistant response.

    Component ids ride on the prompt object's links, not here: carrying
    them in every event payload grew the store quadratically with turns.
    """

    prompt_object_id: ObjectId
    assistant_message_object_id: ObjectId | None = None


@dataclass(frozen=True)
class TraceStats:
    """Basic trace store size statistics."""

    object_count: int
    total_bytes: int


def prompt_trace_payload(trace: PromptTrace) -> dict[str, Any]:
    """Return JSON metadata for a prompt trace."""
    payload: dict[str, Any] = {"prompt_object_id": trace.prompt_object_id}
    if trace.assistant_message_object_id is not None:
        payload["assistant_message_object_id"] = trace.assistant_message_object_id
    return payload


def latest_prompt_trace_fields(prompt_traces: Sequence[Any]) -> dict[str, Any]:
    """Return event fields for the most recent valid prompt trace."""
    if not prompt_traces:
        return {}
    trace = prompt_traces[-1]
    if not isinstance(trace, PromptTrace):
        return {}
    return {"prompt_trace": prompt_trace_payload(trace)}


class UnknownIdError(LookupError):
    """A trace id token matched no ref, object id, or prefix."""

    def __init__(self, token: str) -> None:
        super().__init__(token)
        self.token = token


class AmbiguousIdError(LookupError):
    """A trace id prefix matched more than one object."""

    def __init__(self, token: str, candidates: list[ObjectId]) -> None:
        super().__init__(token)
        self.token = token
        self.candidates = candidates


class UnknownSessionError(LookupError):
    """A session id named no recorded trace store."""

    def __init__(self, session_id: str, available: list[str]) -> None:
        super().__init__(session_id)
        self.session_id = session_id
        self.available = available


class RefConflictError(RuntimeError):
    """A mutable ref did not match the caller's observed value."""

    def __init__(
        self,
        name: str,
        *,
        expected: ObjectId | None,
        actual: ObjectId | None,
    ) -> None:
        super().__init__(
            f"ref {name!r} changed: expected {expected!r}, found {actual!r}"
        )
        self.name = name
        self.expected = expected
        self.actual = actual


class Store(Protocol):
    """Storage API shared by in-memory and SQLite stores."""

    def put_object(self, obj: Object) -> ObjectId: ...
    def get_object(self, object_id: ObjectId) -> Object | None: ...
    def object_ids_with_prefix(
        self, prefix: str, limit: int = 16
    ) -> list[ObjectId]: ...
    def set_ref(
        self,
        name: str,
        object_id: ObjectId,
        *,
        expected: ObjectId | None | object = _REF_EXPECTED_UNSET,
    ) -> None: ...
    def get_ref(self, name: str) -> ObjectId | None: ...
    def batch(self) -> AbstractContextManager[None]: ...
    def record_derivation(self, derivation: Derivation) -> str: ...
    def derivations_for_output(self, output_id: ObjectId) -> list[Derivation]: ...
    def derivations_for_input(self, input_id: ObjectId) -> list[Derivation]: ...
    def graph_closure(self, roots: list[ObjectId]) -> dict[ObjectId, Object]: ...
    def refs(self) -> dict[str, ObjectId]: ...
    def objects(
        self, kind: str | tuple[str, ...] | None = None, limit: int | None = None
    ) -> list[tuple[ObjectId, Object]]: ...
    def search_objects(
        self,
        pattern: str,
        kind: str | tuple[str, ...] | None = None,
        limit: int | None = None,
    ) -> list[tuple[ObjectId, Object]]: ...
    def prompt_object_ids(self) -> list[ObjectId]: ...
    def stats(self) -> TraceStats: ...


def trace_state_dir() -> Path:
    root = os.environ.get("ZETA_STATE_DIR")
    return Path(root).expanduser() if root else Path.home() / ".zeta"


def zeta_sqlite_path(root: Path | None = None) -> Path:
    """Return the unified Zeta SQLite store path."""
    return (root or trace_state_dir()) / ZETA_SQLITE_NAME


def resolve_object_id(store: Store, token: str) -> ObjectId:
    """Resolve a ref name, full object id, or unique id prefix to an object id.

    A bare hex token matches the digest part, so `sha256:` never needs
    typing. Refs win over prefixes; an ambiguous prefix raises with the
    candidate ids.
    """
    if not token:
        raise UnknownIdError(token)
    ref_target = store.get_ref(token)
    if ref_target is not None:
        return ref_target
    if store.get_object(token) is not None:
        return token
    prefix = token if token.startswith("sha256:") else f"sha256:{token}"
    candidates = store.object_ids_with_prefix(prefix)
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        raise AmbiguousIdError(token, candidates)
    raise UnknownIdError(token)


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
        if _table_exists(connection, "derivations") and _column_exists(
            connection, "derivations", "session_id"
        ):
            rows = connection.execute(
                "SELECT DISTINCT session_id FROM derivations WHERE session_id IS NOT NULL"
            ).fetchall()
            sessions.update(str(row["session_id"]) for row in rows)
        if _table_exists(connection, "refs") and _column_exists(
            connection, "refs", "scope"
        ):
            rows = connection.execute(
                """
                SELECT DISTINCT substr(scope, 9) AS session_id
                FROM refs
                WHERE scope LIKE 'session/%'
                """
            ).fetchall()
            sessions.update(str(row["session_id"]) for row in rows)
        if _table_exists(connection, "events"):
            rows = connection.execute(
                "SELECT DISTINCT session_id FROM events WHERE session_id IS NOT NULL"
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
) -> SqliteStore:
    """Open the unified Zeta trace store for one session."""
    return SqliteStore(
        zeta_sqlite_path(root), session_id=session_id, read_only=read_only
    )


def open_existing_trace_store(
    session_id: str,
    *,
    read_only: bool = True,
    root: Path | None = None,
) -> SqliteStore:
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
                resolved_refs[name] = target
        if not resolved_refs:
            return None
        closure = store.graph_closure(list(resolved_refs.values()))
        objects = [
            {
                "id": object_id,
                "kind": obj.kind,
                "schema": obj.schema,
                "data": obj.data,
                "links": list(obj.links),
            }
            for object_id, obj in closure.items()
        ]
        derivations: list[dict[str, Any]] = []
        seen: set[str] = set()
        for object_id in closure:
            for row in store.derivation_records_for_output(object_id):
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
            for name, object_id in (graph.get("refs") or {}).items():
                store.set_ref(str(name), str(object_id))
    finally:
        store.close()
    return count


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _column_exists(connection: sqlite3.Connection, table: str, column: str) -> bool:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return any(str(row["name"]) == column for row in rows)


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row["name"]) for row in rows}


def warn_trace_failure_once(operation: str, exc: BaseException) -> None:
    """Log one warning per operation before fail-open degradation."""
    if operation in _WARNED_FAILURES:
        return
    _WARNED_FAILURES.add(operation)
    LOGGER.warning("trace disabled for %s after failure: %s", operation, exc)


def object_payload(obj: Object) -> dict[str, Any]:
    """Return the canonical payload that is hashed and stored."""
    return {
        "kind": obj.kind,
        "schema": obj.schema,
        "data": obj.data,
        "links": list(obj.links),
    }


def object_id(obj: Object) -> ObjectId:
    """Return the deterministic content address for an object."""
    digest = hashlib.sha256(canonical_json(object_payload(obj)).encode()).hexdigest()
    return f"sha256:{digest}"


def derivation_payload(derivation: Derivation) -> dict[str, Any]:
    """Return the canonical derivation payload."""
    return {
        "producer": derivation.producer,
        "output_id": derivation.output_id,
        "input_ids": list(derivation.input_ids),
        "params": derivation.params,
    }


def derivation_id(derivation: Derivation) -> str:
    """Return the deterministic content address for a derivation record."""
    digest = hashlib.sha256(
        canonical_json(derivation_payload(derivation)).encode()
    ).hexdigest()
    return f"derivation:{digest}"


def escape_like(text: str) -> str:
    """Escape SQLite LIKE wildcards so they match literally."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def canonical_json(value: Any) -> str:
    """Serialize JSON data deterministically for content hashing."""
    return json.dumps(
        normalize_json(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def normalize_json(value: Any) -> Any:
    """Normalize Python-native JSON values before deterministic serialization."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, tuple | list):
        return [normalize_json(item) for item in value]
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("canonical JSON object keys must be strings")
            normalized[key] = normalize_json(item)
        return normalized
    raise TypeError(f"value is not JSON serializable: {type(value).__name__}")


class StoreBase:
    """Shared graph helpers for concrete stores."""

    def prompt_object_ids(self) -> list[ObjectId]:
        store = cast(Store, self)
        return [object_id_value for object_id_value, _ in store.objects(kind="prompt")]

    def graph_closure(self, roots: list[ObjectId]) -> dict[ObjectId, Object]:
        store = cast(Store, self)
        closure: dict[ObjectId, Object] = {}
        pending = list(roots)
        while pending:
            object_id_value = pending.pop()
            if object_id_value in closure:
                continue
            obj = store.get_object(object_id_value)
            if obj is None:
                continue
            closure[object_id_value] = obj
            pending.extend(reversed(obj.links))
        return closure


class InMemoryStore(StoreBase):
    """Process-local trace store for tests and short-lived traces."""

    def __init__(self) -> None:
        self._objects: dict[ObjectId, Object] = {}
        self._refs: dict[str, ObjectId] = {}
        self.derivations: dict[str, Derivation] = {}

    def put_object(self, obj: Object) -> ObjectId:
        stored = normalize_object(obj)
        object_id_value = object_id(stored)
        self._objects.setdefault(object_id_value, stored)
        return object_id_value

    def get_object(self, object_id: ObjectId) -> Object | None:
        return self._objects.get(object_id)

    def object_ids_with_prefix(self, prefix: str, limit: int = 16) -> list[ObjectId]:
        matches = sorted(
            object_id_value
            for object_id_value in self._objects
            if object_id_value.startswith(prefix)
        )
        return matches[:limit]

    def objects(
        self, kind: str | tuple[str, ...] | None = None, limit: int | None = None
    ) -> list[tuple[ObjectId, Object]]:
        kinds = (kind,) if isinstance(kind, str) else kind
        listed = [
            (object_id_value, obj)
            for object_id_value, obj in reversed(self._objects.items())
            if kinds is None or obj.kind in kinds
        ]
        return listed if limit is None else listed[:limit]

    def search_objects(
        self,
        pattern: str,
        kind: str | tuple[str, ...] | None = None,
        limit: int | None = None,
    ) -> list[tuple[ObjectId, Object]]:
        needle = pattern.lower()
        listed = [
            (object_id_value, obj)
            for object_id_value, obj in self.objects(kind=kind)
            if needle in canonical_json(obj.data).lower()
        ]
        return listed if limit is None else listed[:limit]

    def set_ref(
        self,
        name: str,
        object_id: ObjectId,
        *,
        expected: ObjectId | None | object = _REF_EXPECTED_UNSET,
    ) -> None:
        if expected is not _REF_EXPECTED_UNSET:
            actual = self._refs.get(name)
            if actual != expected:
                raise RefConflictError(
                    name,
                    expected=cast(ObjectId | None, expected),
                    actual=actual,
                )
        self._refs[name] = object_id

    def get_ref(self, name: str) -> ObjectId | None:
        return self._refs.get(name)

    @contextmanager
    def batch(self) -> Iterator[None]:
        yield

    def record_derivation(self, derivation: Derivation) -> str:
        stored = normalize_derivation(derivation)
        id_value = derivation_id(stored)
        self.derivations.setdefault(id_value, stored)
        return id_value

    def derivations_for_output(self, output_id: ObjectId) -> list[Derivation]:
        return [
            derivation
            for derivation in self.derivations.values()
            if derivation.output_id == output_id
        ]

    def derivations_for_input(self, input_id: ObjectId) -> list[Derivation]:
        return [
            derivation
            for derivation in self.derivations.values()
            if input_id in derivation.input_ids
        ]

    def refs(self) -> dict[str, ObjectId]:
        return dict(sorted(self._refs.items()))

    def stats(self) -> TraceStats:
        return TraceStats(
            object_count=len(self._objects),
            total_bytes=sum(
                len(canonical_json(object_payload(obj)).encode("utf-8"))
                for obj in self._objects.values()
            ),
        )


def _object_from_row(row: sqlite3.Row) -> Object:
    return Object(
        kind=str(row["kind"]),
        schema=str(row["schema"]),
        data=json.loads(str(row["data_json"])),
        links=tuple(json.loads(str(row["links_json"]))),
    )


def _derivation_from_row(row: sqlite3.Row) -> Derivation:
    return Derivation(
        producer=str(row["producer"]),
        output_id=str(row["output_id"]),
        input_ids=tuple(json.loads(str(row["input_ids_json"]))),
        params=json.loads(str(row["params_json"])),
    )


class SqliteStore(StoreBase):
    """Synchronous SQLite trace store using the standard library."""

    def __init__(
        self,
        path: Path,
        *,
        session_id: str | None = None,
        read_only: bool = False,
    ) -> None:
        self.path = path
        self.session_id = session_id
        self.read_only = read_only
        if read_only:
            self.connection = sqlite3.connect(
                f"{path.as_uri()}?mode=ro&immutable=1",
                uri=True,
                check_same_thread=False,
            )
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(str(path), check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._write_lock = threading.RLock()
        self._batch_depth = 0
        if not read_only:
            self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=5000")
        if not read_only:
            self._init_schema()

    @property
    def scope(self) -> str:
        return f"session/{self.session_id}" if self.session_id is not None else "global"

    @contextmanager
    def batch(self) -> Iterator[None]:
        """Group writes into one transaction committed at batch exit."""
        self._ensure_writable()
        with self._write_lock:
            self._batch_depth += 1
            try:
                yield
            finally:
                self._batch_depth -= 1
                if self._batch_depth == 0:
                    self.connection.commit()

    def _commit(self) -> None:
        if self._batch_depth == 0:
            self.connection.commit()

    def _ensure_writable(self) -> None:
        if self.read_only:
            raise sqlite3.OperationalError("trace store is read-only")

    def close(self) -> None:
        self.connection.close()

    def _init_schema(self) -> None:
        with self._write_lock:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS objects (
                  id TEXT PRIMARY KEY,
                  kind TEXT NOT NULL,
                  schema TEXT NOT NULL,
                  data_json TEXT NOT NULL,
                  links_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS refs (
                  scope TEXT NOT NULL DEFAULT 'global',
                  name TEXT NOT NULL,
                  object_id TEXT NOT NULL,
                  PRIMARY KEY (scope, name)
                );
                CREATE TABLE IF NOT EXISTS derivations (
                  id TEXT PRIMARY KEY,
                  session_id TEXT,
                  producer TEXT NOT NULL,
                  output_id TEXT NOT NULL,
                  input_ids_json TEXT NOT NULL,
                  params_json TEXT NOT NULL,
                  created_at REAL NOT NULL
                );
                """
            )
            self._migrate_legacy_schema()
            self.connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS derivations_session_output_id_idx
                  ON derivations(session_id, output_id, created_at);
                CREATE INDEX IF NOT EXISTS derivations_output_id_idx
                  ON derivations(output_id, created_at);
                CREATE TABLE IF NOT EXISTS derivation_inputs (
                  derivation_id TEXT NOT NULL,
                  input_id TEXT NOT NULL,
                  position INTEGER NOT NULL,
                  PRIMARY KEY (derivation_id, position)
                );
                CREATE INDEX IF NOT EXISTS derivation_inputs_input_id_idx
                  ON derivation_inputs(input_id);
                """
            )
            self.connection.commit()
            self._backfill_derivation_inputs()

    def _migrate_legacy_schema(self) -> None:
        """Upgrade old single-session trace schemas opened directly by tests."""
        refs_columns = _table_columns(self.connection, "refs")
        if refs_columns and "scope" not in refs_columns:
            self.connection.executescript(
                """
                ALTER TABLE refs RENAME TO refs_old;
                CREATE TABLE refs (
                  scope TEXT NOT NULL DEFAULT 'global',
                  name TEXT NOT NULL,
                  object_id TEXT NOT NULL,
                  PRIMARY KEY (scope, name)
                );
                INSERT OR IGNORE INTO refs(scope, name, object_id)
                  SELECT 'global', name, object_id FROM refs_old;
                DROP TABLE refs_old;
                """
            )
        derivation_columns = _table_columns(self.connection, "derivations")
        if derivation_columns and "session_id" not in derivation_columns:
            self.connection.execute(
                "ALTER TABLE derivations ADD COLUMN session_id TEXT"
            )

    def _backfill_derivation_inputs(self) -> None:
        """Index pre-existing derivations whose inputs predate the table."""
        with self._write_lock:
            indexed = self.connection.execute(
                "SELECT COUNT(*) AS n FROM derivation_inputs"
            ).fetchone()
            if int(indexed["n"]):
                return
            rows = self.connection.execute(
                "SELECT id, input_ids_json FROM derivations"
            ).fetchall()
            for row in rows:
                self._index_derivation_inputs(
                    str(row["id"]),
                    tuple(json.loads(str(row["input_ids_json"]))),
                )
            self.connection.commit()

    def _index_derivation_inputs(
        self,
        derivation_id_value: str,
        input_ids: tuple[ObjectId, ...],
    ) -> None:
        self.connection.executemany(
            """
            INSERT OR IGNORE INTO derivation_inputs
              (derivation_id, input_id, position)
            VALUES (?, ?, ?)
            """,
            [
                (derivation_id_value, input_id, position)
                for position, input_id in enumerate(input_ids)
            ],
        )

    def put_object(self, obj: Object) -> ObjectId:
        self._ensure_writable()
        stored = normalize_object(obj)
        object_id_value = object_id(stored)
        with self._write_lock:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO objects
                  (id, kind, schema, data_json, links_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    object_id_value,
                    stored.kind,
                    stored.schema,
                    canonical_json(stored.data),
                    canonical_json(list(stored.links)),
                ),
            )
            self._commit()
        return object_id_value

    def get_object(self, object_id: ObjectId) -> Object | None:
        row = self.connection.execute(
            "SELECT kind, schema, data_json, links_json FROM objects WHERE id = ?",
            (object_id,),
        ).fetchone()
        if row is None:
            return None
        return _object_from_row(row)

    def object_ids_with_prefix(self, prefix: str, limit: int = 16) -> list[ObjectId]:
        rows = self.connection.execute(
            r"SELECT id FROM objects WHERE id LIKE ? ESCAPE '\' ORDER BY id LIMIT ?",
            (f"{escape_like(prefix)}%", limit),
        ).fetchall()
        return [str(row["id"]) for row in rows]

    def set_ref(
        self,
        name: str,
        object_id: ObjectId,
        *,
        expected: ObjectId | None | object = _REF_EXPECTED_UNSET,
    ) -> None:
        self._ensure_writable()
        with self._write_lock:
            if expected is _REF_EXPECTED_UNSET:
                self.connection.execute(
                    """
                    INSERT INTO refs (scope, name, object_id) VALUES (?, ?, ?)
                    ON CONFLICT(scope, name) DO UPDATE
                    SET object_id = excluded.object_id
                    """,
                    (self.scope, name, object_id),
                )
            elif expected is None:
                cursor = self.connection.execute(
                    """
                    INSERT INTO refs (scope, name, object_id) VALUES (?, ?, ?)
                    ON CONFLICT(scope, name) DO NOTHING
                    """,
                    (self.scope, name, object_id),
                )
                if cursor.rowcount != 1:
                    raise RefConflictError(
                        name,
                        expected=None,
                        actual=self.get_ref(name),
                    )
            else:
                cursor = self.connection.execute(
                    """
                    UPDATE refs
                    SET object_id = ?
                    WHERE scope = ? AND name = ? AND object_id = ?
                    """,
                    (object_id, self.scope, name, expected),
                )
                if cursor.rowcount != 1:
                    raise RefConflictError(
                        name,
                        expected=cast(ObjectId, expected),
                        actual=self.get_ref(name),
                    )
            self._commit()

    def get_ref(self, name: str) -> ObjectId | None:
        row = self.connection.execute(
            "SELECT object_id FROM refs WHERE scope = ? AND name = ?",
            (self.scope, name),
        ).fetchone()
        if row is None:
            return None
        return str(row["object_id"])

    def record_derivation(self, derivation: Derivation) -> str:
        self._ensure_writable()
        stored = normalize_derivation(derivation)
        id_value = self._derivation_id(stored)
        with self._write_lock:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO derivations
                  (id, session_id, producer, output_id, input_ids_json,
                   params_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    id_value,
                    self.session_id,
                    stored.producer,
                    stored.output_id,
                    canonical_json(list(stored.input_ids)),
                    canonical_json(stored.params),
                    time.time(),
                ),
            )
            self._index_derivation_inputs(id_value, stored.input_ids)
            self._commit()
        return id_value

    def derivations_for_output(self, output_id: ObjectId) -> list[Derivation]:
        session_filter, params = self._session_filter("output_id = ?", output_id)
        rows = self.connection.execute(
            f"""
            SELECT producer, output_id, input_ids_json, params_json
            FROM derivations
            WHERE {session_filter}
            ORDER BY created_at, id
            """,
            params,
        ).fetchall()
        return [_derivation_from_row(row) for row in rows]

    def derivation_records_for_output(
        self, output_id: ObjectId
    ) -> list[dict[str, Any]]:
        """Return raw derivation rows for an output, with id and created_at.

        Exports need both fields to rebuild recency ordering elsewhere;
        the Derivation dataclass deliberately carries neither.
        """
        session_filter, params = self._session_filter("output_id = ?", output_id)
        rows = self.connection.execute(
            f"""
            SELECT id, producer, output_id, input_ids_json, params_json, created_at
            FROM derivations
            WHERE {session_filter}
            ORDER BY created_at, id
            """,
            params,
        ).fetchall()
        return [
            {
                "id": str(row["id"]),
                "producer": str(row["producer"]),
                "output_id": str(row["output_id"]),
                "input_ids": json.loads(str(row["input_ids_json"])),
                "params": json.loads(str(row["params_json"])),
                "created_at": float(row["created_at"]),
            }
            for row in rows
        ]

    def import_object(self, object_id_value: ObjectId, obj: Object) -> None:
        """Insert an object under an exported id instead of recomputing it.

        Trusting the exported id keeps links and refs exact even if
        hashing rules ever differ between the exporting and importing
        versions.
        """
        self._ensure_writable()
        stored = normalize_object(obj)
        with self._write_lock:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO objects
                  (id, kind, schema, data_json, links_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    object_id_value,
                    stored.kind,
                    stored.schema,
                    canonical_json(stored.data),
                    canonical_json(list(stored.links)),
                ),
            )
            self._commit()

    def import_derivation(
        self,
        derivation_id_value: str,
        derivation: Derivation,
        created_at: float,
    ) -> None:
        """Insert an exported derivation, preserving its original timestamp."""
        self._ensure_writable()
        stored = normalize_derivation(derivation)
        stored_id = (
            self._derivation_id(stored)
            if self.session_id is not None
            else derivation_id_value
        )
        with self._write_lock:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO derivations
                  (id, session_id, producer, output_id, input_ids_json,
                   params_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stored_id,
                    self.session_id,
                    stored.producer,
                    stored.output_id,
                    canonical_json(list(stored.input_ids)),
                    canonical_json(stored.params),
                    created_at,
                ),
            )
            self._index_derivation_inputs(stored_id, stored.input_ids)
            self._commit()

    def _derivation_id(self, derivation: Derivation) -> str:
        if self.session_id is None:
            return derivation_id(derivation)
        payload = {"session_id": self.session_id, **derivation_payload(derivation)}
        digest = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
        return f"derivation:{digest}"

    def derivations_for_input(self, input_id: ObjectId) -> list[Derivation]:
        session_filter, params = self._session_filter(
            "derivation_inputs.input_id = ?", input_id
        )
        rows = self.connection.execute(
            f"""
            SELECT derivations.producer, derivations.output_id,
                   derivations.input_ids_json, derivations.params_json
            FROM derivations
            JOIN derivation_inputs
              ON derivation_inputs.derivation_id = derivations.id
            WHERE {session_filter}
            GROUP BY derivations.id
            ORDER BY derivations.created_at, derivations.id
            """,
            params,
        ).fetchall()
        return [_derivation_from_row(row) for row in rows]

    def refs(self) -> dict[str, ObjectId]:
        rows = self.connection.execute(
            "SELECT name, object_id FROM refs WHERE scope = ? ORDER BY name",
            (self.scope,),
        ).fetchall()
        return {str(row["name"]): str(row["object_id"]) for row in rows}

    def objects(
        self, kind: str | tuple[str, ...] | None = None, limit: int | None = None
    ) -> list[tuple[ObjectId, Object]]:
        return self._list_objects(kind=kind, limit=limit)

    def search_objects(
        self,
        pattern: str,
        kind: str | tuple[str, ...] | None = None,
        limit: int | None = None,
    ) -> list[tuple[ObjectId, Object]]:
        return self._list_objects(kind=kind, limit=limit, pattern=pattern)

    def _list_objects(
        self,
        *,
        kind: str | tuple[str, ...] | None,
        limit: int | None,
        pattern: str | None = None,
    ) -> list[tuple[ObjectId, Object]]:
        kinds = (kind,) if isinstance(kind, str) else kind
        clauses: list[str] = []
        join_params: list[Any] = []
        where_params: list[Any] = []
        join = "LEFT JOIN derivations ON derivations.output_id = objects.id"
        created_at_expression = "derivations.created_at"
        if self.session_id is not None:
            join = """
            JOIN (
              SELECT output_id AS object_id, created_at
              FROM derivations
              WHERE session_id = ?
              UNION ALL
              SELECT derivation_inputs.input_id AS object_id,
                     derivations.created_at AS created_at
              FROM derivations
              JOIN derivation_inputs
                ON derivation_inputs.derivation_id = derivations.id
              WHERE derivations.session_id = ?
            ) AS session_objects ON session_objects.object_id = objects.id
            """
            join_params.extend([self.session_id, self.session_id])
            created_at_expression = "session_objects.created_at"
        if kinds is not None:
            placeholders = ", ".join("?" for _ in kinds)
            clauses.append(f"objects.kind IN ({placeholders})")
            where_params.extend(kinds)
        if pattern is not None:
            clauses.append(r"objects.data_json LIKE ? ESCAPE '\'")
            where_params.append(f"%{escape_like(pattern)}%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit_clause = "LIMIT ?" if limit is not None else ""
        params = [*join_params, *where_params]
        if limit is not None:
            params.append(limit)
        rows = self.connection.execute(
            f"""
            SELECT objects.id, objects.kind, objects.schema,
                   objects.data_json, objects.links_json
            FROM objects
            {join}
            {where}
            GROUP BY objects.id
            ORDER BY COALESCE(MAX({created_at_expression}), 0) DESC, objects.id DESC
            {limit_clause}
            """,
            params,
        ).fetchall()
        return [(str(row["id"]), _object_from_row(row)) for row in rows]

    def _session_filter(self, clause: str, *params: Any) -> tuple[str, tuple[Any, ...]]:
        if self.session_id is None:
            return clause, params
        return f"session_id = ? AND {clause}", (self.session_id, *params)

    def clear_session(self, session_id: str | None = None) -> None:
        """Remove session-scoped refs and derivations without deleting objects."""
        self._ensure_writable()
        target = session_id or self.session_id
        if target is None:
            raise ValueError("session id is required")
        with self._write_lock:
            derivation_ids = [
                str(row["id"])
                for row in self.connection.execute(
                    "SELECT id FROM derivations WHERE session_id = ?",
                    (target,),
                ).fetchall()
            ]
            if derivation_ids:
                placeholders = ", ".join("?" for _ in derivation_ids)
                self.connection.execute(
                    f"DELETE FROM derivation_inputs WHERE derivation_id IN ({placeholders})",
                    derivation_ids,
                )
            self.connection.execute(
                "DELETE FROM derivations WHERE session_id = ?",
                (target,),
            )
            self.connection.execute(
                "DELETE FROM refs WHERE scope = ?",
                (f"session/{target}",),
            )
            self._commit()

    def stats(self) -> TraceStats:
        row = self.connection.execute(
            """
            SELECT COUNT(*) AS object_count,
                   COALESCE(SUM(LENGTH(data_json) + LENGTH(links_json)), 0)
                     AS total_bytes
            FROM objects
            """
        ).fetchone()
        return TraceStats(
            object_count=int(row["object_count"]),
            total_bytes=int(row["total_bytes"]),
        )


def normalize_object(obj: Object) -> Object:
    """Return an object with normalized data and link containers."""
    return Object(
        kind=obj.kind,
        schema=obj.schema,
        data=normalize_json(obj.data),
        links=tuple(str(link) for link in obj.links),
    )


def normalize_derivation(derivation: Derivation) -> Derivation:
    """Return a derivation with normalized containers."""
    return Derivation(
        producer=derivation.producer,
        output_id=derivation.output_id,
        input_ids=tuple(str(input_id) for input_id in derivation.input_ids),
        params=normalize_json(derivation.params),
    )
