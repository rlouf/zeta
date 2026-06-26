"""SQLite projections for durable runtime orchestration state."""

from __future__ import annotations

import json
import sqlite3

from zeta.events import Event
from zeta.orchestration.attempts import attempt_from_event_payload
from zeta.orchestration.queue import project_one_queue_item


class RuntimeEventProjection:
    """Projects runtime queue and attempt events into queryable tables."""

    def init_schema(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS queue_items (
              queue_item_id TEXT PRIMARY KEY,
              event_id TEXT NOT NULL,
              target_agent TEXT NOT NULL,
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
            """
        )

    def clear(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            DELETE FROM attempt_results;
            DELETE FROM attempts;
            DELETE FROM queue_items;
            """
        )

    def index(self, connection: sqlite3.Connection, event: Event) -> None:
        if event.event_type.startswith("runtime.queue_item."):
            _index_one_queue_item(connection, event)
            return
        if event.event_type.startswith("runtime.attempt."):
            _index_one_attempt(connection, event)


def runtime_event_projection() -> RuntimeEventProjection:
    return RuntimeEventProjection()


def _index_one_queue_item(connection: sqlite3.Connection, event: Event) -> None:
    queue_item = project_one_queue_item(event)
    if queue_item is None:
        return
    raw_error = event.payload.get("error")
    error = raw_error if isinstance(raw_error, str) else None
    connection.execute(
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
            queue_item.queue_item_id,
            queue_item.event_id,
            queue_item.target_agent,
            queue_item.status,
            event.timestamp_ms if queue_item.status == "available" else None,
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
    connection.execute(
        """
        INSERT INTO attempts
          (attempt_id, queue_item_id, event_id, attempt_number, target_agent,
           worker_name, claim_token, status, started_at, heartbeat_at,
           finished_at, error, session_id, run_id, summary, input_tokens, output_tokens,
           tool_calls_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(attempt_id) DO UPDATE SET
          claim_token = COALESCE(attempts.claim_token, excluded.claim_token),
          status = excluded.status,
          heartbeat_at = excluded.heartbeat_at,
          finished_at = excluded.finished_at,
          error = excluded.error,
          session_id = excluded.session_id,
          run_id = excluded.run_id,
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


def _runtime_status(event: Event) -> str:
    status = event.payload.get("status")
    if isinstance(status, str):
        return status
    if event.event_type == "runtime.attempt.started":
        return "running"
    return event.event_type.rsplit(".", 1)[-1]


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None
