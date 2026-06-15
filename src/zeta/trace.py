"""Content-addressed object graph for Zeta prompt traces."""

from __future__ import annotations

import atexit
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
LOGGER = logging.getLogger("zeta.trace")
_WARNED_FAILURES: set[str] = set()


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


class Store(Protocol):
    """Storage API shared by in-memory and SQLite stores."""

    def put_object(self, obj: Object) -> ObjectId: ...
    def get_object(self, object_id: ObjectId) -> Object | None: ...
    def object_ids_with_prefix(
        self, prefix: str, limit: int = 16
    ) -> list[ObjectId]: ...
    def set_ref(self, name: str, object_id: ObjectId) -> None: ...
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


def trace_session_dir(session_id: str | None = None) -> Path:
    session = session_id or os.environ.get("ZETA_SESSION_ID") or "default"
    return trace_state_dir() / "sessions" / session


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
    """Return the default per-session trace SQLite path."""
    return trace_session_dir() / DEFAULT_SQLITE_NAME


def session_sqlite_path(session_id: str) -> Path:
    """Return the trace SQLite path for a named session."""
    return trace_session_dir(session_id) / DEFAULT_SQLITE_NAME


def available_session_ids() -> list[str]:
    """Return the session ids that have a recorded trace store, sorted."""
    sessions_root = trace_state_dir() / "sessions"
    return sorted(
        path.parent.name for path in sessions_root.glob(f"*/{DEFAULT_SQLITE_NAME}")
    )


_DEFAULT_STORES: dict[Path, SqliteStore] = {}


def default_store(session_id: str | None = None) -> SqliteStore:
    """Return the process-wide store for the current session path.

    An explicit session id opens that session's store read-only and
    uncached; a missing store raises UnknownSessionError with the
    recorded session ids.
    """
    if session_id is not None:
        path = session_sqlite_path(session_id)
        if not path.exists():
            raise UnknownSessionError(session_id, available_session_ids())
        return SqliteStore(path, read_only=True)
    path = default_sqlite_path()
    store = _DEFAULT_STORES.get(path)
    if store is None:
        store = SqliteStore(path)
        _DEFAULT_STORES[path] = store
    return store


def close_default_stores() -> None:
    """Close every cached default store; the next call reopens."""
    while _DEFAULT_STORES:
        _, store = _DEFAULT_STORES.popitem()
        store.close()


atexit.register(close_default_stores)


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

    def set_ref(self, name: str, object_id: ObjectId) -> None:
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

    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self.path = path
        self.read_only = read_only
        if read_only:
            self.connection = sqlite3.connect(
                f"{path.as_uri()}?mode=ro&immutable=1",
                uri=True,
            )
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self._write_lock = threading.RLock()
        self._batch_depth = 0
        if not read_only:
            self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=5000")
        if not read_only:
            self._init_schema()

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
                  name TEXT PRIMARY KEY,
                  object_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS derivations (
                  id TEXT PRIMARY KEY,
                  producer TEXT NOT NULL,
                  output_id TEXT NOT NULL,
                  input_ids_json TEXT NOT NULL,
                  params_json TEXT NOT NULL,
                  created_at REAL NOT NULL
                );
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

    def set_ref(self, name: str, object_id: ObjectId) -> None:
        self._ensure_writable()
        with self._write_lock:
            self.connection.execute(
                """
                INSERT INTO refs (name, object_id) VALUES (?, ?)
                ON CONFLICT(name) DO UPDATE SET object_id = excluded.object_id
                """,
                (name, object_id),
            )
            self._commit()

    def get_ref(self, name: str) -> ObjectId | None:
        row = self.connection.execute(
            "SELECT object_id FROM refs WHERE name = ?",
            (name,),
        ).fetchone()
        if row is None:
            return None
        return str(row["object_id"])

    def record_derivation(self, derivation: Derivation) -> str:
        self._ensure_writable()
        stored = normalize_derivation(derivation)
        id_value = derivation_id(stored)
        with self._write_lock:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO derivations
                  (id, producer, output_id, input_ids_json, params_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    id_value,
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
        rows = self.connection.execute(
            """
            SELECT producer, output_id, input_ids_json, params_json
            FROM derivations
            WHERE output_id = ?
            ORDER BY created_at, id
            """,
            (output_id,),
        ).fetchall()
        return [_derivation_from_row(row) for row in rows]

    def derivation_records_for_output(
        self, output_id: ObjectId
    ) -> list[dict[str, Any]]:
        """Return raw derivation rows for an output, with id and created_at.

        Exports need both fields to rebuild recency ordering elsewhere;
        the Derivation dataclass deliberately carries neither.
        """
        rows = self.connection.execute(
            """
            SELECT id, producer, output_id, input_ids_json, params_json, created_at
            FROM derivations
            WHERE output_id = ?
            ORDER BY created_at, id
            """,
            (output_id,),
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
        with self._write_lock:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO derivations
                  (id, producer, output_id, input_ids_json, params_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    derivation_id_value,
                    stored.producer,
                    stored.output_id,
                    canonical_json(list(stored.input_ids)),
                    canonical_json(stored.params),
                    created_at,
                ),
            )
            self._index_derivation_inputs(derivation_id_value, stored.input_ids)
            self._commit()

    def derivations_for_input(self, input_id: ObjectId) -> list[Derivation]:
        rows = self.connection.execute(
            """
            SELECT derivations.producer, derivations.output_id,
                   derivations.input_ids_json, derivations.params_json
            FROM derivations
            JOIN derivation_inputs
              ON derivation_inputs.derivation_id = derivations.id
            WHERE derivation_inputs.input_id = ?
            GROUP BY derivations.id
            ORDER BY derivations.created_at, derivations.id
            """,
            (input_id,),
        ).fetchall()
        return [_derivation_from_row(row) for row in rows]

    def refs(self) -> dict[str, ObjectId]:
        rows = self.connection.execute(
            "SELECT name, object_id FROM refs ORDER BY name"
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
        params: list[Any] = []
        if kinds is not None:
            placeholders = ", ".join("?" for _ in kinds)
            clauses.append(f"objects.kind IN ({placeholders})")
            params.extend(kinds)
        if pattern is not None:
            clauses.append(r"objects.data_json LIKE ? ESCAPE '\'")
            params.append(f"%{escape_like(pattern)}%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit_clause = "LIMIT ?" if limit is not None else ""
        if limit is not None:
            params.append(limit)
        rows = self.connection.execute(
            f"""
            SELECT objects.id, objects.kind, objects.schema,
                   objects.data_json, objects.links_json
            FROM objects
            LEFT JOIN derivations ON derivations.output_id = objects.id
            {where}
            GROUP BY objects.id
            ORDER BY COALESCE(MAX(derivations.created_at), 0) DESC, objects.id DESC
            {limit_clause}
            """,
            params,
        ).fetchall()
        return [(str(row["id"]), _object_from_row(row)) for row in rows]

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
