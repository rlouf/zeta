"""Runtime store wrapper for orchestration-owned SQLite state."""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zeta import ids
from zeta.events import DraftEvent, Event
from zeta.harness.metrics import MetricAttribute, NullRuntimeMetrics, RuntimeMetrics
from zeta.harness.projections import runtime_event_projection
from zeta.harness.protocols import CoordinationStore, QueueClaim, RuntimeJournal
from zeta.journal.sqlite import SqliteEventStore
from zeta.journal.store import Filter
from zeta.journal.types import AppendOutcome
from zeta.substrate.sqlite import sqlite_table_names

RUNTIME_PROJECTION_TABLES = frozenset(
    {"queue_items", "attempts", "attempt_results", "locks"}
)


def _in_memory_read_only_event_store(
    source: SqliteEventStore | None = None,
) -> SqliteEventStore:
    """Keep absent or incomplete on-disk projections out of inspection writes."""

    projection = runtime_event_projection()
    store = SqliteEventStore(
        ":memory:",
        projections=(projection,),
    )
    if source is not None:
        source.connection.backup(store.connection)
        projection.reset_schema(store.connection)
        projection.init_schema(store.connection)
        for event in store.list_events(Filter()):
            projection.index(store.connection, event)
    projection.recover(store.connection)
    store.connection.commit()
    store.connection.execute("PRAGMA query_only=ON")
    store.read_only = True
    return store


def _read_only_event_store(path: Path) -> SqliteEventStore:
    """Rebuild incomplete runtime indexes transiently instead of migrating state."""

    if not path.exists():
        return _in_memory_read_only_event_store()

    projection = runtime_event_projection()
    store = SqliteEventStore(
        path,
        projections=(projection,),
        read_only=True,
    )
    table_names = sqlite_table_names(store.connection)
    if "events" not in table_names:
        store.close()
        return _in_memory_read_only_event_store()

    projection_version = None
    if "event_projection_versions" in table_names:
        row = store.connection.execute(
            "SELECT version FROM event_projection_versions WHERE name = ?",
            (projection.name,),
        ).fetchone()
        projection_version = int(row["version"]) if row is not None else None
    if (
        RUNTIME_PROJECTION_TABLES.issubset(table_names)
        and projection_version == projection.version
    ):
        return store

    try:
        return _in_memory_read_only_event_store(store)
    finally:
        store.close()


@dataclass(frozen=True)
class RuntimeEventStore:
    """Event log plus orchestration-owned runtime indexes."""

    events: SqliteEventStore
    metrics: RuntimeMetrics = field(default_factory=NullRuntimeMetrics)

    @classmethod
    def open(
        cls,
        path: Path | str,
        *,
        metrics: RuntimeMetrics | None = None,
        read_only: bool = False,
    ) -> RuntimeEventStore:
        """Keep first-time inspections from establishing runtime state."""

        store_path = Path(path)
        if read_only:
            event_store = _read_only_event_store(store_path)
        else:
            event_store = SqliteEventStore(
                store_path,
                projections=(runtime_event_projection(),),
            )
        return cls(
            event_store,
            metrics or NullRuntimeMetrics(),
        )

    @property
    def path(self) -> Path:
        return self.events.path

    @property
    def journal(self) -> RuntimeJournal:
        return self

    @property
    def coordination(self) -> CoordinationStore:
        return self

    @property
    def connection(self) -> sqlite3.Connection:
        return self.events.connection

    def close(self) -> None:
        self.events.close()

    def accept(self, draft: DraftEvent) -> AppendOutcome:
        started = time.perf_counter()
        try:
            return self.events.accept(draft)
        finally:
            self.observe_runtime_metric(
                "sqlite.event_append_ms",
                _elapsed_ms(started),
                event_type=draft.event_type,
            )

    def append(self, event: Event) -> AppendOutcome:
        started = time.perf_counter()
        try:
            return self.events.append(event)
        finally:
            self.observe_runtime_metric(
                "sqlite.event_append_ms",
                _elapsed_ms(started),
                event_type=event.event_type,
            )

    def observe_runtime_metric(
        self,
        name: str,
        value: float,
        **attributes: MetricAttribute,
    ) -> None:
        try:
            self.metrics.observe(name, value, attributes=attributes)
        except Exception:
            # Metrics must never change runtime state transitions.
            return

    def rebuild_projections(self) -> int:
        return self.events.rebuild_projections()

    def get(self, event_id: str) -> Event | None:
        return self.events.get(event_id)

    def list_events(self, filter: Filter) -> list[Event]:
        return self.events.list_events(filter)

    def children(self, event_id: str, *, limit: int | None = None) -> list[Event]:
        return self.events.children(event_id, limit=limit)

    def causal_chain(self, event_id: str) -> list[Event]:
        return self.events.causal_chain(event_id)

    def events_for_turn(self, turn_id: str) -> list[Event]:
        return self.events.events_for_turn(turn_id)

    def events_for_run(self, run_id: str) -> list[Event]:
        return self.events.events_for_run(run_id)

    def clear_session_events(self, session_id: str, *, event_type_prefix: str) -> int:
        return self.events.clear_session_events(
            session_id,
            event_type_prefix=event_type_prefix,
        )

    def ensure_pending_queue_item(self, event: Event) -> str:
        queue_item_id = _pending_queue_item_id(event)
        with self.events.write_lock:
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
        with self.events.write_lock:
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
        with self.events.write_lock:
            row = self.connection.execute(
                """
                SELECT queue_item_id, event_id, target_agent, project_generation,
                       status
                FROM queue_items
                WHERE queue_item_id = ?
                """,
                (queue_item_id,),
            ).fetchone()
        if row is None:
            return None
        return _without_none_snapshot_fields(dict(row))

    def queue_item_attempt_count(self, queue_item_id: str) -> int:
        with self.events.write_lock:
            row = self.connection.execute(
                "SELECT attempt_count FROM queue_items WHERE queue_item_id = ?",
                (queue_item_id,),
            ).fetchone()
        return int(row["attempt_count"]) if row is not None else 0

    def list_queue_items(self) -> list[dict[str, Any]]:
        with self.events.write_lock:
            rows = self.connection.execute(
                """
                SELECT queue_item_id, event_id, target_agent, project_generation,
                       status, available_at,
                       claimed_by, claimed_until, attempt_count, last_error, updated_at
                FROM queue_items
                ORDER BY updated_at ASC, queue_item_id ASC
                """
            ).fetchall()
        return [_without_none_snapshot_fields(dict(row)) for row in rows]

    def list_attempts(self) -> list[dict[str, Any]]:
        with self.events.write_lock:
            rows = self.connection.execute(
                """
                SELECT a.attempt_id, a.queue_item_id, a.event_id, a.attempt_number,
                       a.target_agent, a.worker_name, a.status, a.started_at,
                       a.heartbeat_at, a.finished_at, a.error, a.session_id, a.run_id,
                       a.project_generation, a.execution_manifest_id,
                       a.execution_manifest_json,
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
        with self.events.write_lock:
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
        started = time.perf_counter()
        requested = tuple(dict.fromkeys(keys))
        if not requested:
            return True
        placeholders = _sql_placeholders(requested)
        with self.events.write_lock:
            self.events.begin_immediate()
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
                    self.observe_runtime_metric("runtime.lock_conflicts", 1)
                    self.observe_runtime_metric(
                        "sqlite.lock_acquire_ms",
                        _elapsed_ms(started),
                        lock_count=len(requested),
                    )
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
                self.observe_runtime_metric(
                    "sqlite.lock_acquire_ms",
                    _elapsed_ms(started),
                    lock_count=len(requested),
                )
                return True
            except Exception:
                self.connection.rollback()
                self.observe_runtime_metric(
                    "sqlite.lock_acquire_ms",
                    _elapsed_ms(started),
                    lock_count=len(requested),
                )
                raise

    def release_locks(self, keys: Iterable[str], owner: str) -> int:
        requested = tuple(dict.fromkeys(keys))
        if not requested:
            return 0
        placeholders = _sql_placeholders(requested)
        with self.events.write_lock:
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

    def renew_locks(
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
        with self.events.write_lock:
            self.events.begin_immediate()
            try:
                cursor = self.connection.execute(
                    f"""
                    UPDATE locks
                    SET expires_at = ?
                    WHERE owner = ?
                      AND key IN ({placeholders})
                      AND expires_at >= ?
                    """,
                    (now_ms + lease_ms, owner, *requested, now_ms),
                )
                if cursor.rowcount != len(requested):
                    self.connection.rollback()
                    return False
                self.connection.commit()
                return True
            except Exception:
                self.connection.rollback()
                raise

    def reconcile_expired_locks(self, *, now_ms: int) -> int:
        with self.events.write_lock:
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
        started = time.perf_counter()
        with self.events.write_lock:
            self.events.begin_immediate()
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
                    self.observe_runtime_metric(
                        "sqlite.heartbeat_write_ms",
                        _elapsed_ms(started),
                    )
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
                self.observe_runtime_metric(
                    "sqlite.heartbeat_write_ms",
                    _elapsed_ms(started),
                )
                return True
            except Exception:
                self.connection.rollback()
                self.observe_runtime_metric(
                    "sqlite.heartbeat_write_ms",
                    _elapsed_ms(started),
                )
                raise

    def claim_next_queue_item(
        self,
        worker_name: str,
        *,
        lease_ms: int,
        now_ms: int,
        exclude_queue_item_ids: Iterable[str] = (),
    ) -> QueueClaim | None:
        started = time.perf_counter()
        excluded = tuple(dict.fromkeys(exclude_queue_item_ids))
        excluded_clause = ""
        excluded_params: tuple[str, ...] = ()
        if excluded:
            excluded_clause = (
                f"AND queue_item_id NOT IN ({_sql_placeholders(excluded)})"
            )
            excluded_params = excluded
        with self.events.write_lock:
            self.events.begin_immediate()
            try:
                row = self.connection.execute(
                    f"""
                    SELECT queue_item_id, available_at, updated_at
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
                    self.observe_runtime_metric(
                        "sqlite.queue_claim_ms",
                        _elapsed_ms(started),
                        claimed=False,
                    )
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
                    self.observe_runtime_metric(
                        "sqlite.queue_claim_ms",
                        _elapsed_ms(started),
                        claimed=False,
                    )
                    return None
                available_at = row["available_at"]
                enqueued_at = (
                    int(available_at)
                    if isinstance(available_at, int)
                    else int(row["updated_at"])
                )
                self.observe_runtime_metric(
                    "runtime.queue_lag_ms",
                    max(0, now_ms - enqueued_at),
                    queue_item_id=queue_item_id,
                )
                self.observe_runtime_metric(
                    "sqlite.queue_claim_ms",
                    _elapsed_ms(started),
                    claimed=True,
                )
                return QueueClaim(queue_item_id, claim_token)
            except Exception:
                self.connection.rollback()
                self.observe_runtime_metric(
                    "sqlite.queue_claim_ms",
                    _elapsed_ms(started),
                    claimed=False,
                )
                raise

    def release_queue_claim(
        self,
        queue_item_id: str,
        worker_name: str,
        *,
        claim_token: str,
        now_ms: int,
    ) -> bool:
        with self.events.write_lock:
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
        with self.events.write_lock:
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
        with self.events.write_lock:
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
                  AND (claimed_until IS NULL OR claimed_until < ?)
                """,
                (now_ms, now_ms),
            )
            self.connection.commit()
        return int(cursor.rowcount)


def _row_to_attempt(row: sqlite3.Row) -> dict[str, Any]:
    usage = _json_column(row["usage_json"])
    return _without_none_snapshot_fields(
        {
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
            "project_generation": _optional_str(row["project_generation"]),
            "execution_manifest_id": _optional_str(row["execution_manifest_id"]),
            "execution_manifest": _json_column(row["execution_manifest_json"]),
            "input_tokens": _row_token_count(
                row["input_tokens"], usage, "input_tokens"
            ),
            "output_tokens": _row_token_count(
                row["output_tokens"], usage, "output_tokens"
            ),
            "final_status": _optional_str(row["final_status"]),
            "summary": _optional_str(row["summary"]),
            "result": _json_column(row["result_json"]),
            "events": _json_column(row["events_json"]),
            "tool_calls": _json_column(row["tool_calls_json"]),
            "usage": usage,
        }
    )


def _without_none_snapshot_fields(record: dict[str, Any]) -> dict[str, Any]:
    for key in (
        "project_generation",
        "execution_manifest_id",
        "execution_manifest",
    ):
        if record.get(key) is None:
            record.pop(key, None)
    return record


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
    return ids.pending_queue_item_id(event.id)


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000


def _json_column(value: object) -> Any | None:
    if not isinstance(value, str):
        return None
    return json.loads(value)


def _sql_placeholders(values: tuple[object, ...]) -> str:
    return ", ".join("?" for _ in values)
