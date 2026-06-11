"""Delegation ledger index: derived SQLite state over the global event log.

`events.jsonl` stays the append-only source of truth. The index keeps one
row per turn/effect record, keyed on the record ids, so live writes and
`sigil log reindex` replays converge on the same state and log rotation
loses no turn, effect, or cost answer. Index failures degrade fail-open:
the JSONL line is always written and a reindex heals the gap.
"""

from __future__ import annotations

import atexit
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from .protocols import is_effect_record, is_turn_record
from .state import append_event, read_jsonl_path, state_dir

DEFAULT_LEDGER_NAME = "ledger.sqlite3"
EVENT_LOG_NAME = "events.jsonl"
LOGGER = logging.getLogger("sigil.ledger")
_WARNED_FAILURES: set[str] = set()


def warn_ledger_failure_once(operation: str, exc: BaseException) -> None:
    """Log one warning per operation before fail-open degradation."""
    if operation in _WARNED_FAILURES:
        return
    _WARNED_FAILURES.add(operation)
    LOGGER.warning("ledger index disabled for %s after failure: %s", operation, exc)


class LedgerIndex:
    """SQLite index over ledger turn and effect records."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self._init_schema()

    def close(self) -> None:
        self.connection.close()

    def _init_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS turns (
              turn_id TEXT PRIMARY KEY,
              time REAL,
              session TEXT,
              cwd TEXT,
              workflow TEXT,
              objective TEXT,
              outcome TEXT,
              staged INTEGER,
              input_tokens INTEGER,
              output_tokens INTEGER,
              model_calls INTEGER,
              wall_ms INTEGER,
              record_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS turns_time_idx ON turns(time);
            CREATE INDEX IF NOT EXISTS turns_session_time_idx
              ON turns(session, time);
            CREATE TABLE IF NOT EXISTS effects (
              effect_id TEXT PRIMARY KEY,
              turn_id TEXT,
              time REAL,
              session TEXT,
              kind TEXT,
              staged INTEGER,
              path TEXT,
              command TEXT,
              exit_status INTEGER,
              duration_ms INTEGER,
              tool_call_id TEXT,
              resolved_outcome TEXT,
              before_hash TEXT,
              after_hash TEXT,
              record_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS effects_turn_id_idx ON effects(turn_id);
            CREATE INDEX IF NOT EXISTS effects_path_idx ON effects(path);
            CREATE INDEX IF NOT EXISTS effects_tool_call_id_idx
              ON effects(tool_call_id);
            """
        )
        self.connection.commit()

    def index_record(self, payload: dict[str, Any]) -> bool:
        """Index one event payload; non-ledger events return False."""
        if is_turn_record(payload):
            self.index_turn_record(payload)
            return True
        if is_effect_record(payload):
            self.index_effect_record(payload)
            return True
        return False

    def index_turn_record(self, payload: dict[str, Any]) -> None:
        contract = mapping_field(payload, "contract")
        cost = mapping_field(payload, "cost")
        self.connection.execute(
            """
            INSERT OR REPLACE INTO turns
              (turn_id, time, session, cwd, workflow, objective, outcome,
               staged, input_tokens, output_tokens, model_calls, wall_ms,
               record_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(payload.get("turn_id") or ""),
                payload.get("time"),
                payload.get("session"),
                payload.get("cwd"),
                payload.get("workflow"),
                payload.get("objective"),
                payload.get("outcome"),
                1 if contract.get("staged") else 0,
                cost.get("input_tokens"),
                cost.get("output_tokens"),
                cost.get("model_calls"),
                cost.get("wall_ms"),
                record_json(payload),
            ),
        )
        self.connection.commit()

    def index_effect_record(self, payload: dict[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT OR REPLACE INTO effects
              (effect_id, turn_id, time, session, kind, staged, path,
               command, exit_status, duration_ms, tool_call_id,
               resolved_outcome, before_hash, after_hash, record_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(payload.get("effect_id") or ""),
                payload.get("turn_id"),
                payload.get("time"),
                payload.get("session"),
                payload.get("kind"),
                1 if payload.get("staged") else 0,
                payload.get("path"),
                payload.get("command"),
                payload.get("exit_status"),
                payload.get("duration_ms"),
                payload.get("tool_call_id"),
                payload.get("resolved_outcome"),
                payload.get("before_hash"),
                payload.get("after_hash"),
                record_json(payload),
            ),
        )
        self.connection.commit()

    def turn(self, turn_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT record_json FROM turns WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()
        if row is None:
            return None
        return json.loads(str(row["record_json"]))

    def turns(self, limit: int | None = None) -> list[dict[str, Any]]:
        limit_clause = "LIMIT ?" if limit is not None else ""
        params: list[Any] = [] if limit is None else [limit]
        rows = self.connection.execute(
            f"""
            SELECT record_json FROM turns
            ORDER BY time DESC, turn_id DESC
            {limit_clause}
            """,
            params,
        ).fetchall()
        return [json.loads(str(row["record_json"])) for row in rows]

    def effects_for_turn(self, turn_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT record_json FROM effects
            WHERE turn_id = ?
            ORDER BY time, effect_id
            """,
            (turn_id,),
        ).fetchall()
        return [json.loads(str(row["record_json"])) for row in rows]

    def effects_touching(self, path: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT record_json FROM effects
            WHERE path = ?
            ORDER BY time, effect_id
            """,
            (path,),
        ).fetchall()
        return [json.loads(str(row["record_json"])) for row in rows]


def mapping_field(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def record_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def default_ledger_path() -> Path:
    """Return the global ledger index path next to the event log."""
    return state_dir() / DEFAULT_LEDGER_NAME


_DEFAULT_INDEXES: dict[Path, LedgerIndex] = {}


def default_ledger_index() -> LedgerIndex:
    """Return the process-wide ledger index for the current state dir."""
    path = default_ledger_path()
    index = _DEFAULT_INDEXES.get(path)
    if index is None:
        index = LedgerIndex(path)
        _DEFAULT_INDEXES[path] = index
    return index


def close_ledger_indexes() -> None:
    """Close every cached ledger index; the next call reopens."""
    while _DEFAULT_INDEXES:
        _, index = _DEFAULT_INDEXES.popitem()
        index.close()


atexit.register(close_ledger_indexes)


def append_turn_record(record: dict[str, Any]) -> dict[str, Any]:
    """Append one turn record to the event log and index it."""
    payload = append_event(record)
    index_payload("append_turn_record", payload)
    return payload


def append_effect_record(record: dict[str, Any]) -> dict[str, Any]:
    """Append one effect record to the event log and index it."""
    payload = append_event(record)
    index_payload("append_effect_record", payload)
    return payload


def index_payload(operation: str, payload: dict[str, Any]) -> None:
    try:
        default_ledger_index().index_record(payload)
    except Exception as exc:
        warn_ledger_failure_once(operation, exc)


def reindex(index: LedgerIndex | None = None) -> tuple[int, int]:
    """Rebuild the index from both event log generations, oldest first."""
    target = index if index is not None else default_ledger_index()
    log_path = state_dir() / EVENT_LOG_NAME
    turns = 0
    effects = 0
    for path in (log_path.with_name(f"{EVENT_LOG_NAME}.1"), log_path):
        for payload in read_jsonl_path(path):
            if is_turn_record(payload):
                target.index_turn_record(payload)
                turns += 1
            elif is_effect_record(payload):
                target.index_effect_record(payload)
                effects += 1
    return turns, effects
