"""SQLite implementation of the content-addressed substrate.

`SqliteObjectStore` provides durable local storage for the substrate. It keeps
objects content-addressed, scopes refs and derivations when a session id is
supplied, and indexes derivation inputs so callers can traverse both producer
and consumer relationships.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from zeta.substrate.objects import Derivation, Object, ObjectId, Ref, RefUpdate
from zeta.substrate.store import (
    IncompatibleSchemaError,
    StoreBase,
    StoreStats,
    escape_like,
)


def _dump_canonical(value: Any) -> str:
    """Serialize a value to the store's canonical, content-stable JSON form."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
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


class SqliteObjectStore(StoreBase):
    """Synchronous SQLite implementation of the substrate store protocol."""

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
    def batch(self) -> Any:
        """Group writes into one transaction resolved at the outermost exit.

        The outermost batch commits on success and rolls back if the body
        raised, so a failed batch never leaves partial writes committed.
        """
        self._ensure_writable()
        with self._write_lock:
            self._batch_depth += 1
            try:
                yield
            except BaseException:
                if self._batch_depth == 1:
                    self.connection.rollback()
                raise
            else:
                if self._batch_depth == 1:
                    self.connection.commit()
            finally:
                self._batch_depth -= 1

    def _commit(self) -> None:
        if self._batch_depth == 0:
            self.connection.commit()

    def _fetchone(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._write_lock:
            return self.connection.execute(sql, params).fetchone()

    def _fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._write_lock:
            return self.connection.execute(sql, params).fetchall()

    def _ensure_writable(self) -> None:
        if self.read_only:
            raise sqlite3.OperationalError("trace store is read-only")

    def close(self) -> None:
        self.connection.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def _init_schema(self) -> None:
        with self._write_lock:
            if self._schema_is_incompatible():
                raise IncompatibleSchemaError("incompatible Zeta SQLite schema")
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
                  id TEXT NOT NULL,
                  session_id TEXT,
                  producer TEXT NOT NULL,
                  output_id TEXT NOT NULL,
                  input_ids_json TEXT NOT NULL,
                  params_json TEXT NOT NULL,
                  created_at REAL NOT NULL
                );
                """
            )
            self.connection.executescript(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS derivations_scope_id_idx
                  ON derivations(COALESCE(session_id, ''), id);
                CREATE INDEX IF NOT EXISTS derivations_session_output_id_idx
                  ON derivations(session_id, output_id, created_at);
                CREATE INDEX IF NOT EXISTS derivations_output_id_idx
                  ON derivations(output_id, created_at);
                CREATE TABLE IF NOT EXISTS derivation_inputs (
                  session_id TEXT,
                  derivation_id TEXT NOT NULL,
                  input_id TEXT NOT NULL,
                  position INTEGER NOT NULL
                );
                """
            )
            self.connection.executescript(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS derivation_inputs_scope_position_idx
                  ON derivation_inputs(COALESCE(session_id, ''), derivation_id, position);
                CREATE INDEX IF NOT EXISTS derivation_inputs_input_id_idx
                  ON derivation_inputs(input_id);
                """
            )
            self.connection.commit()

    def _schema_is_incompatible(self) -> bool:
        required_columns = {
            "objects": {"id", "kind", "schema", "data_json", "links_json"},
            "refs": {"scope", "name", "object_id"},
            "derivations": {
                "id",
                "session_id",
                "producer",
                "output_id",
                "input_ids_json",
                "params_json",
                "created_at",
            },
            "derivation_inputs": {
                "session_id",
                "derivation_id",
                "input_id",
                "position",
            },
        }
        for table, columns in required_columns.items():
            existing = self._table_columns(table)
            if existing and not columns.issubset(existing):
                return True
        return False

    def _table_columns(self, table: str) -> set[str]:
        return {
            str(row["name"])
            for row in self.connection.execute(f"PRAGMA table_info({table})")
        }

    def _index_derivation_inputs(
        self,
        session_id: str | None,
        derivation_id_value: str,
        input_ids: tuple[ObjectId, ...],
    ) -> None:
        self.connection.executemany(
            """
            INSERT OR IGNORE INTO derivation_inputs
              (session_id, derivation_id, input_id, position)
            VALUES (?, ?, ?, ?)
            """,
            [
                (session_id, derivation_id_value, input_id, position)
                for position, input_id in enumerate(input_ids)
            ],
        )

    def put_object(self, obj: Object) -> ObjectId:
        self._ensure_writable()
        object_id_value = obj.content_address()
        with self._write_lock:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO objects
                  (id, kind, schema, data_json, links_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    object_id_value,
                    obj.kind,
                    obj.schema,
                    _dump_canonical(obj.data),
                    _dump_canonical(list(obj.links)),
                ),
            )
            self._commit()
        return object_id_value

    def get_object(self, object_id: ObjectId) -> Object | None:
        row = self._fetchone(
            "SELECT kind, schema, data_json, links_json FROM objects WHERE id = ?",
            (object_id,),
        )
        if row is None:
            return None
        return _object_from_row(row)

    def object_ids_with_prefix(self, prefix: str, limit: int = 16) -> list[ObjectId]:
        rows = self._fetchall(
            r"SELECT id FROM objects WHERE id LIKE ? ESCAPE '\' ORDER BY id LIMIT ?",
            (f"{escape_like(prefix)}%", limit),
        )
        return [str(row["id"]) for row in rows]

    def move_ref(
        self,
        name: str,
        expected: ObjectId | None,
        new: ObjectId,
    ) -> RefUpdate:
        self._ensure_writable()
        with self._write_lock:
            standalone = self._batch_depth == 0
            if standalone:
                self.connection.execute("BEGIN IMMEDIATE")
            try:
                old_object_id = self._ref_object_id(name)
                if old_object_id != expected:
                    updated = False
                elif expected is None:
                    cursor = self.connection.execute(
                        """
                        INSERT INTO refs (scope, name, object_id) VALUES (?, ?, ?)
                        ON CONFLICT(scope, name) DO NOTHING
                        """,
                        (self.scope, name, new),
                    )
                    updated = cursor.rowcount == 1
                else:
                    cursor = self.connection.execute(
                        """
                        UPDATE refs
                        SET object_id = ?
                        WHERE scope = ? AND name = ? AND object_id = ?
                        """,
                        (new, self.scope, name, expected),
                    )
                    updated = cursor.rowcount == 1
            except Exception:
                if standalone:
                    self.connection.rollback()
                raise
            if standalone:
                self.connection.commit()
            return RefUpdate(
                name=name,
                old_object_id=old_object_id,
                new_object_id=new,
                updated=updated,
            )

    def _ref_object_id(self, name: str) -> ObjectId | None:
        row = self._fetchone(
            "SELECT object_id FROM refs WHERE scope = ? AND name = ?",
            (self.scope, name),
        )
        if row is None:
            return None
        return str(row["object_id"])

    def get_ref(self, name: str) -> Ref | None:
        object_id_value = self._ref_object_id(name)
        if object_id_value is None:
            return None
        return Ref(name=name, object_id=object_id_value)

    def record_derivation(self, derivation: Derivation) -> str:
        self._ensure_writable()
        id_value = derivation.content_address()
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
                    derivation.producer,
                    derivation.output_id,
                    _dump_canonical(list(derivation.input_ids)),
                    _dump_canonical(derivation.params),
                    time.time(),
                ),
            )
            self._index_derivation_inputs(
                self.session_id, id_value, derivation.input_ids
            )
            self._commit()
        return id_value

    def derivations_for_output(self, output_id: ObjectId) -> list[Derivation]:
        session_filter, params = self._session_filter("output_id = ?", output_id)
        rows = self._fetchall(
            f"""
            SELECT producer, output_id, input_ids_json, params_json
            FROM derivations
            WHERE {session_filter}
            ORDER BY created_at, id
            """,
            params,
        )
        return [_derivation_from_row(row) for row in rows]

    def derivation_records_for_output(
        self, output_id: ObjectId
    ) -> list[dict[str, Any]]:
        """Return raw derivation rows for an output, with id and created_at.

        Exports need both fields to rebuild recency ordering elsewhere;
        the Derivation dataclass deliberately carries neither.
        """
        session_filter, params = self._session_filter("output_id = ?", output_id)
        rows = self._fetchall(
            f"""
            SELECT id, producer, output_id, input_ids_json, params_json, created_at
            FROM derivations
            WHERE {session_filter}
            ORDER BY created_at, id
            """,
            params,
        )
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
        with self._write_lock:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO objects
                  (id, kind, schema, data_json, links_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    object_id_value,
                    obj.kind,
                    obj.schema,
                    _dump_canonical(obj.data),
                    _dump_canonical(list(obj.links)),
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
        stored_id = derivation.content_address()
        if self.session_id is None:
            stored_id = derivation_id_value
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
                    derivation.producer,
                    derivation.output_id,
                    _dump_canonical(list(derivation.input_ids)),
                    _dump_canonical(derivation.params),
                    created_at,
                ),
            )
            self._index_derivation_inputs(
                self.session_id,
                stored_id,
                derivation.input_ids,
            )
            self._commit()

    def derivations_for_input(self, input_id: ObjectId) -> list[Derivation]:
        session_filter = "derivation_inputs.input_id = ?"
        params: tuple[Any, ...] = (input_id,)
        if self.session_id is not None:
            session_filter = (
                "derivations.session_id = ? AND derivation_inputs.input_id = ?"
            )
            params = (self.session_id, input_id)
        rows = self._fetchall(
            f"""
            SELECT derivations.producer, derivations.output_id,
                   derivations.input_ids_json, derivations.params_json
            FROM derivations
            JOIN derivation_inputs
              ON derivation_inputs.derivation_id = derivations.id
             AND derivation_inputs.session_id IS derivations.session_id
            WHERE {session_filter}
            GROUP BY derivations.session_id, derivations.id
            ORDER BY derivations.created_at, derivations.id
            """,
            params,
        )
        return [_derivation_from_row(row) for row in rows]

    def refs(self) -> list[Ref]:
        rows = self._fetchall(
            "SELECT name, object_id FROM refs WHERE scope = ? ORDER BY name",
            (self.scope,),
        )
        return [
            Ref(name=str(row["name"]), object_id=str(row["object_id"])) for row in rows
        ]

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
               AND derivation_inputs.session_id IS derivations.session_id
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
        rows = self._fetchall(
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
        )
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
            self.connection.execute(
                "DELETE FROM derivation_inputs WHERE session_id = ?",
                (target,),
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

    def stats(self) -> StoreStats:
        row = self._fetchone(
            """
            SELECT COUNT(*) AS object_count,
                   COALESCE(SUM(LENGTH(data_json) + LENGTH(links_json)), 0)
                     AS total_bytes
            FROM objects
            """
        )
        if row is None:
            return StoreStats(object_count=0, total_bytes=0)
        return StoreStats(
            object_count=int(row["object_count"]),
            total_bytes=int(row["total_bytes"]),
        )
