"""Content-addressed object graph for Zeta prompt traces."""

from __future__ import annotations

import atexit
import hashlib
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

from ..state import session_dir

ObjectId = str
DEFAULT_SQLITE_NAME = "zeta-trace.sqlite3"
LOGGER = logging.getLogger("sigil.zeta.trace")
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
    """Trace ids for one prompt request and its assistant response."""

    prompt_object_id: ObjectId
    assistant_message_object_id: ObjectId | None = None
    component_object_ids: tuple[ObjectId, ...] = ()


@dataclass(frozen=True)
class TraceStats:
    """Basic trace store size statistics."""

    object_count: int
    total_bytes: int


def prompt_trace_payload(trace: PromptTrace) -> dict[str, Any]:
    """Return JSON metadata for a prompt trace."""
    payload: dict[str, Any] = {
        "prompt_object_id": trace.prompt_object_id,
        "component_object_ids": list(trace.component_object_ids),
    }
    if trace.assistant_message_object_id is not None:
        payload["assistant_message_object_id"] = trace.assistant_message_object_id
    return payload


def latest_prompt_trace_fields(
    prompt_traces: list[Any] | tuple[Any, ...],
) -> dict[str, Any]:
    """Return event fields for the most recent valid prompt trace."""
    if not prompt_traces:
        return {}
    trace = prompt_traces[-1]
    if not isinstance(trace, PromptTrace):
        return {}
    return {"prompt_trace": prompt_trace_payload(trace)}


class Store(Protocol):
    """Storage API shared by in-memory and SQLite stores."""

    def put_object(self, obj: Object) -> ObjectId: ...
    def get_object(self, object_id: ObjectId) -> Object | None: ...
    def set_ref(self, name: str, object_id: ObjectId) -> None: ...
    def get_ref(self, name: str) -> ObjectId | None: ...
    def batch(self) -> AbstractContextManager[None]: ...
    def record_derivation(self, derivation: Derivation) -> str: ...
    def derivations_for_output(self, output_id: ObjectId) -> list[Derivation]: ...
    def graph_closure(self, roots: list[ObjectId]) -> dict[ObjectId, Object]: ...
    def refs(self) -> dict[str, ObjectId]: ...
    def prompt_object_ids(self) -> list[ObjectId]: ...
    def stats(self) -> TraceStats: ...


def default_sqlite_path() -> Path:
    """Return the default per-session trace SQLite path."""
    return session_dir() / DEFAULT_SQLITE_NAME


_DEFAULT_STORES: dict[Path, SqliteStore] = {}


def default_store() -> SqliteStore:
    """Return the process-wide store for the current session path."""
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
    LOGGER.warning("zeta trace disabled for %s after failure: %s", operation, exc)


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
        self.objects: dict[ObjectId, Object] = {}
        self._refs: dict[str, ObjectId] = {}
        self.derivations: dict[str, Derivation] = {}

    def put_object(self, obj: Object) -> ObjectId:
        stored = normalize_object(obj)
        object_id_value = object_id(stored)
        self.objects.setdefault(object_id_value, stored)
        return object_id_value

    def get_object(self, object_id: ObjectId) -> Object | None:
        return self.objects.get(object_id)

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

    def refs(self) -> dict[str, ObjectId]:
        return dict(sorted(self._refs.items()))

    def prompt_object_ids(self) -> list[ObjectId]:
        return [
            object_id_value
            for object_id_value, obj in reversed(self.objects.items())
            if obj.kind == "prompt"
        ]

    def stats(self) -> TraceStats:
        return TraceStats(
            object_count=len(self.objects),
            total_bytes=sum(
                len(canonical_json(object_payload(obj)).encode("utf-8"))
                for obj in self.objects.values()
            ),
        )


class SqliteStore(StoreBase):
    """Synchronous SQLite trace store using the standard library."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self._write_lock = threading.RLock()
        self._batch_depth = 0
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self._init_schema()

    @contextmanager
    def batch(self) -> Iterator[None]:
        """Group writes into one transaction committed at batch exit."""
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
                """
            )
            self.connection.commit()

    def put_object(self, obj: Object) -> ObjectId:
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
        return Object(
            kind=str(row["kind"]),
            schema=str(row["schema"]),
            data=json.loads(str(row["data_json"])),
            links=tuple(json.loads(str(row["links_json"]))),
        )

    def set_ref(self, name: str, object_id: ObjectId) -> None:
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
        return [
            Derivation(
                producer=str(row["producer"]),
                output_id=str(row["output_id"]),
                input_ids=tuple(json.loads(str(row["input_ids_json"]))),
                params=json.loads(str(row["params_json"])),
            )
            for row in rows
        ]

    def refs(self) -> dict[str, ObjectId]:
        rows = self.connection.execute(
            "SELECT name, object_id FROM refs ORDER BY name"
        ).fetchall()
        return {str(row["name"]): str(row["object_id"]) for row in rows}

    def prompt_object_ids(self) -> list[ObjectId]:
        rows = self.connection.execute(
            """
            SELECT objects.id
            FROM objects
            LEFT JOIN derivations ON derivations.output_id = objects.id
            WHERE objects.kind = 'prompt'
            GROUP BY objects.id
            ORDER BY COALESCE(MAX(derivations.created_at), 0) DESC, objects.id DESC
            """
        ).fetchall()
        return [str(row["id"]) for row in rows]

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
