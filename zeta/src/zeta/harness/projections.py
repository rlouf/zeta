"""SQLite projections for durable runtime orchestration state."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from zeta import ids
from zeta.events import Event
from zeta.harness.attempts import attempt_from_event_payload
from zeta.harness.queue import (
    is_queueable_event,
    pending_queue_item_id,
    project_one_queue_item,
)


class RuntimeEventProjection:
    """Projects runtime queue and attempt events into queryable tables."""

    name = "zeta.harness.runtime"
    version = 5

    def init_schema(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS queue_items (
              queue_item_id TEXT PRIMARY KEY,
              event_id TEXT NOT NULL,
              target_agent TEXT NOT NULL,
              project_generation TEXT,
              status TEXT NOT NULL,
              available_at INTEGER,
              claimed_by TEXT,
              claimed_token TEXT,
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
              claim_token TEXT,
              status TEXT NOT NULL,
              started_at TEXT NOT NULL,
              heartbeat_at INTEGER,
              finished_at TEXT,
              error TEXT,
              session_id TEXT,
              run_id TEXT,
              project_generation TEXT,
              execution_manifest_id TEXT,
              execution_manifest_json TEXT,
              summary TEXT,
              input_tokens INTEGER,
              output_tokens INTEGER,
              tool_calls_json TEXT
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

            CREATE TABLE IF NOT EXISTS locks (
              key TEXT PRIMARY KEY,
              owner TEXT NOT NULL,
              acquired_at INTEGER NOT NULL,
              expires_at INTEGER NOT NULL
            ) STRICT;

            CREATE TABLE IF NOT EXISTS scheduled_events (
              handle TEXT PRIMARY KEY,
              event_type TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              publish_at_ms INTEGER NOT NULL,
              source_agent_id TEXT NOT NULL,
              source_session_id TEXT,
              source_run_id TEXT,
              source_queue_item_id TEXT NOT NULL,
              position INTEGER NOT NULL,
              created_event_id TEXT NOT NULL,
              status TEXT NOT NULL,
              published_event_id TEXT,
              terminal_event_id TEXT,
              updated_at INTEGER NOT NULL
            ) STRICT;

            CREATE INDEX IF NOT EXISTS idx_scheduled_events_due
              ON scheduled_events(
                status,
                publish_at_ms,
                source_queue_item_id,
                position
              );
            """
        )

    def clear(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            DELETE FROM locks;
            DELETE FROM scheduled_events;
            DELETE FROM attempt_results;
            DELETE FROM attempts;
            DELETE FROM queue_items;
            """
        )

    def reset_schema(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            DROP TABLE IF EXISTS locks;
            DROP TABLE IF EXISTS scheduled_events;
            DROP TABLE IF EXISTS attempt_results;
            DROP TABLE IF EXISTS attempts;
            DROP TABLE IF EXISTS queue_items;
            """
        )

    def recover(self, connection: sqlite3.Connection) -> None:
        """Discard replayed ownership and make incomplete claims runnable."""
        connection.execute(
            """
            UPDATE queue_items
            SET status = CASE
                  WHEN target_agent = '' THEN 'pending'
                  ELSE 'available'
                END,
                claimed_by = NULL,
                claimed_token = NULL,
                claimed_until = NULL
            WHERE status = 'claimed'
            """
        )
        connection.execute(
            """
            UPDATE scheduled_events
            SET status = 'pending'
            WHERE status = 'claimed'
            """
        )

    def index(self, connection: sqlite3.Connection, event: Event) -> None:
        if event.event_type.startswith("runtime.scheduled_event."):
            _index_one_scheduled_event(connection, event)
            return
        if is_queueable_event(event):
            _index_pending_queue_item(connection, event)
            return
        if event.event_type.startswith("runtime.queue_item."):
            _index_one_queue_item(connection, event)
            return
        if event.event_type.startswith("runtime.attempt."):
            _index_one_attempt(connection, event)


def runtime_event_projection() -> RuntimeEventProjection:
    return RuntimeEventProjection()


def _index_one_scheduled_event(
    connection: sqlite3.Connection,
    event: Event,
) -> None:
    handle = _optional_str(event.payload.get("handle"))
    if handle is None:
        return
    if event.event_type == "runtime.scheduled_event.created":
        _index_scheduled_event_created(connection, event, handle)
        return
    if event.event_type == "runtime.scheduled_event.published":
        published_event_id = _optional_str(event.payload.get("published_event_id"))
        if published_event_id is None:
            return
        _index_scheduled_event_terminal(
            connection,
            event,
            handle,
            status="published",
            published_event_id=published_event_id,
        )
        return
    if event.event_type == "runtime.scheduled_event.cancelled":
        _index_scheduled_event_terminal(
            connection,
            event,
            handle,
            status="cancelled",
            published_event_id=None,
        )


def _index_scheduled_event_created(
    connection: sqlite3.Connection,
    event: Event,
    handle: str,
) -> None:
    event_type = _optional_str(event.payload.get("event_type"))
    payload = event.payload.get("payload")
    publish_at = _optional_str(event.payload.get("publish_at"))
    source_agent_id = _optional_str(event.payload.get("source_agent_id"))
    source_queue_item_id = _optional_str(event.payload.get("source_queue_item_id"))
    position = event.payload.get("position")
    if (
        event_type is None
        or not isinstance(payload, dict)
        or publish_at is None
        or source_agent_id is None
        or source_queue_item_id is None
        or not isinstance(position, int)
        or isinstance(position, bool)
    ):
        return
    publish_at_ms = _iso_timestamp_ms(publish_at)
    if publish_at_ms is None:
        return
    source_session_id = _optional_str(event.payload.get("source_session_id"))
    connection.execute(
        """
        INSERT INTO scheduled_events
          (handle, event_type, payload_json, publish_at_ms, source_agent_id,
           source_session_id, source_run_id, source_queue_item_id, position,
           created_event_id, status, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        ON CONFLICT(handle) DO NOTHING
        """,
        (
            handle,
            event_type,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            publish_at_ms,
            source_agent_id,
            source_session_id if source_session_id is not None else event.session_id,
            event.run_id,
            source_queue_item_id,
            position,
            event.id,
            event.timestamp_ms,
        ),
    )


def _index_scheduled_event_terminal(
    connection: sqlite3.Connection,
    event: Event,
    handle: str,
    *,
    status: str,
    published_event_id: str | None,
) -> None:
    connection.execute(
        """
        UPDATE scheduled_events
        SET status = ?,
            published_event_id = ?,
            terminal_event_id = ?,
            updated_at = ?
        WHERE handle = ?
          AND status IN ('pending', 'claimed')
        """,
        (
            status,
            published_event_id,
            event.id,
            event.timestamp_ms,
            handle,
        ),
    )


def _iso_timestamp_ms(value: str) -> int | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return int(parsed.timestamp() * 1_000)


def _index_pending_queue_item(connection: sqlite3.Connection, event: Event) -> None:
    connection.execute(
        """
        INSERT INTO queue_items
          (queue_item_id, event_id, target_agent, status, available_at, updated_at)
        VALUES (?, ?, '', 'pending', ?, ?)
        ON CONFLICT(queue_item_id) DO NOTHING
        """,
        (
            pending_queue_item_id(event),
            event.id,
            event.timestamp_ms,
            event.timestamp_ms,
        ),
    )


def _index_one_queue_item(connection: sqlite3.Connection, event: Event) -> None:
    queue_item = project_one_queue_item(event)
    if queue_item is None:
        return
    pending_id = ids.pending_queue_item_id(queue_item.event_id)
    if queue_item.queue_item_id != pending_id:
        connection.execute(
            """
            DELETE FROM queue_items
            WHERE queue_item_id = ? AND target_agent = ''
            """,
            (pending_id,),
        )
    raw_error = event.payload.get("error")
    error = raw_error if isinstance(raw_error, str) else None
    connection.execute(
        """
        INSERT INTO queue_items
          (queue_item_id, event_id, target_agent, project_generation, status, available_at,
           last_error, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(queue_item_id) DO UPDATE SET
          event_id = excluded.event_id,
          target_agent = excluded.target_agent,
          project_generation = COALESCE(
            excluded.project_generation,
            queue_items.project_generation
          ),
          status = excluded.status,
          available_at = CASE
            WHEN excluded.status = 'available' THEN excluded.available_at
            ELSE queue_items.available_at
          END,
          claimed_by = CASE
            WHEN excluded.status = 'claimed' THEN queue_items.claimed_by
            ELSE NULL
          END,
          claimed_token = CASE
            WHEN excluded.status = 'claimed' THEN queue_items.claimed_token
            ELSE NULL
          END,
          claimed_until = CASE
            WHEN excluded.status = 'claimed' THEN queue_items.claimed_until
            ELSE NULL
          END,
          last_error = excluded.last_error,
          updated_at = excluded.updated_at
        """,
        (
            queue_item.queue_item_id,
            queue_item.event_id,
            queue_item.target_agent,
            _optional_str(event.payload.get("project_generation")),
            queue_item.status,
            _queue_item_available_at(event)
            if queue_item.status == "available"
            else None,
            error,
            event.timestamp_ms,
        ),
    )


def _index_one_attempt(connection: sqlite3.Connection, event: Event) -> None:
    attempt = attempt_from_event_payload(
        {**event.payload, "status": _runtime_status(event)}
    )
    if attempt is None:
        return
    raw_worker_name = event.payload.get("worker_name")
    worker_name = raw_worker_name if isinstance(raw_worker_name, str) else None
    claim_token = None
    if attempt.status == "running" and worker_name is not None:
        claim_token_row = connection.execute(
            """
            SELECT claimed_token
            FROM queue_items
            WHERE queue_item_id = ?
              AND claimed_by = ?
              AND status = 'claimed'
            """,
            (attempt.queue_item_id, worker_name),
        ).fetchone()
        if claim_token_row is not None:
            claim_token = _optional_str(claim_token_row["claimed_token"])
    raw_summary = event.payload.get("summary")
    summary = raw_summary if isinstance(raw_summary, str) else None
    raw_tool_calls = event.payload.get("tool_calls")
    tool_calls_json = (
        json.dumps(raw_tool_calls, ensure_ascii=False, separators=(",", ":"))
        if raw_tool_calls is not None
        else None
    )
    execution_manifest = event.payload.get("execution_manifest")
    execution_manifest_json = (
        json.dumps(execution_manifest, ensure_ascii=False, separators=(",", ":"))
        if execution_manifest is not None
        else None
    )
    connection.execute(
        """
        INSERT INTO attempts
          (attempt_id, queue_item_id, event_id, attempt_number, target_agent,
           worker_name, claim_token, status, started_at, heartbeat_at,
           finished_at, error, session_id, run_id, summary, input_tokens, output_tokens,
           project_generation, execution_manifest_id, execution_manifest_json,
           tool_calls_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(attempt_id) DO UPDATE SET
          claim_token = COALESCE(attempts.claim_token, excluded.claim_token),
          status = excluded.status,
          heartbeat_at = excluded.heartbeat_at,
          finished_at = excluded.finished_at,
          error = excluded.error,
          session_id = excluded.session_id,
          run_id = excluded.run_id,
          project_generation = COALESCE(
            excluded.project_generation,
            attempts.project_generation
          ),
          execution_manifest_id = COALESCE(
            excluded.execution_manifest_id,
            attempts.execution_manifest_id
          ),
          execution_manifest_json = COALESCE(
            excluded.execution_manifest_json,
            attempts.execution_manifest_json
          ),
          summary = excluded.summary,
          input_tokens = excluded.input_tokens,
          output_tokens = excluded.output_tokens,
          tool_calls_json = excluded.tool_calls_json
        """,
        (
            attempt.attempt_id,
            attempt.queue_item_id,
            attempt.event_id,
            attempt.attempt_number,
            attempt.target_agent,
            worker_name,
            claim_token,
            attempt.status,
            attempt.started_at,
            event.timestamp_ms,
            attempt.finished_at,
            attempt.error,
            attempt.session_id,
            attempt.run_id,
            summary,
            _usage_token(event, "input_tokens", "prompt_tokens"),
            _usage_token(event, "output_tokens", "completion_tokens"),
            _optional_str(event.payload.get("project_generation")),
            _optional_str(event.payload.get("execution_manifest_id")),
            execution_manifest_json,
            tool_calls_json,
        ),
    )
    if attempt.status == "running":
        connection.execute(
            """
            UPDATE queue_items
            SET attempt_count = CASE
              WHEN attempt_count < ? THEN ?
              ELSE attempt_count
            END
            WHERE queue_item_id = ?
            """,
            (
                attempt.attempt_number,
                attempt.attempt_number,
                attempt.queue_item_id,
            ),
        )
    if attempt.status in {"completed", "failed", "cancelled"}:
        _index_one_attempt_result(connection, event, attempt.attempt_id, attempt.status)


def _index_one_attempt_result(
    connection: sqlite3.Connection,
    event: Event,
    attempt_id: str,
    status: str,
) -> None:
    result = event.payload.get("result")
    result_json = None
    if result is not None:
        result_json = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    raw_summary = event.payload.get("summary")
    summary = raw_summary if isinstance(raw_summary, str) else None
    raw_events = event.payload.get("events")
    events_json = (
        json.dumps(raw_events, ensure_ascii=False, separators=(",", ":"))
        if raw_events is not None
        else None
    )
    raw_tool_calls = event.payload.get("tool_calls")
    tool_calls_json = (
        json.dumps(raw_tool_calls, ensure_ascii=False, separators=(",", ":"))
        if raw_tool_calls is not None
        else None
    )
    raw_usage = event.payload.get("usage")
    usage_json = (
        json.dumps(raw_usage, ensure_ascii=False, separators=(",", ":"))
        if raw_usage is not None
        else None
    )
    raw_finished_at = event.payload.get("finished_at")
    finished_at = raw_finished_at if isinstance(raw_finished_at, str) else None
    connection.execute(
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
            summary,
            result_json,
            events_json,
            tool_calls_json,
            usage_json,
            finished_at,
        ),
    )


def _usage_token(event: Event, *keys: str) -> int | None:
    usage = event.payload.get("usage")
    if not isinstance(usage, dict):
        return None
    for key in keys:
        value = usage.get(key)
        if isinstance(value, int):
            return value
    return None


def _queue_item_available_at(event: Event) -> int:
    not_before = event.payload.get("not_before")
    if isinstance(not_before, int) and not isinstance(not_before, bool):
        return not_before
    if isinstance(not_before, float):
        return int(not_before)
    return event.timestamp_ms


def _runtime_status(event: Event) -> str:
    status = event.payload.get("status")
    if isinstance(status, str):
        return status
    if event.event_type == "runtime.attempt.started":
        return "running"
    return event.event_type.rsplit(".", 1)[-1]


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None
