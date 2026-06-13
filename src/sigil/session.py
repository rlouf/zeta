"""Session continuity state: inspection helpers and shell turn recording.

Session state is useful only if it can be inspected. These helpers make the
current shell's continuity files visible, and own the recent-turns buffer the
shell bindings write through `record_turn`.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from .events import Event, Filter, event_store, time_from_timestamp_micros
from .failure import (
    failure_context_prompt,
    last_failure_or_none,
    record_failure,
    truncate_snippet,
)
from .ledger import append_effect_record, append_turn_record
from .protocols import (
    EFFECT_KIND_COMMAND,
    TURN_OUTCOME_EXECUTED,
    TURN_OUTCOME_FAILED,
    effect_record,
    turn_contract,
    turn_record,
)
from .state import (
    append_jsonl_line,
    read_jsonl,
    session_dir,
    session_id,
    state_dir,
    write_text_atomic,
)

RUN_WORKFLOW = "run"

SESSION_FILES = (
    "last-failure.json",
    "recent-turns.jsonl",
)

RECENT_TURNS_FILE = "recent-turns.jsonl"
RECENT_TURNS_LIMIT = 50
TURN_SKIP_PREFIXES = (",", "sigil ", "__sigil_")


def session_paths() -> dict[str, str]:
    """Return the global and current-session paths users need for debugging."""
    from .events import event_store_path

    return {
        "state": str(state_dir()),
        "session": str(session_dir()),
        "session_id": session_id(),
        "events": str(event_store_path()),
    }


def known_sessions() -> list[dict[str, Any]]:
    """List session directories with coarse file presence and event metadata."""
    root = state_dir() / "sessions"
    if not root.exists():
        return []
    latest_by_session = latest_events_by_session(read_events())
    sessions = []
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if not path.is_dir():
            continue
        latest = latest_by_session.get(path.name)
        sessions.append(
            {
                "session_id": path.name,
                "path": str(path),
                "files": sorted(item.name for item in path.iterdir() if item.is_file()),
                "last_event_time": event_time(latest) if latest is not None else None,
                "last_event_type": latest.event_type if latest is not None else None,
                "last_cwd": latest.payload.get("cwd") if latest is not None else None,
            }
        )
    return sessions


def latest_events_by_session(events: list[Event]) -> dict[str, Event]:
    """Return the newest event seen for each session id."""
    latest: dict[str, Event] = {}
    for event in events:
        if event.session_id is None:
            continue
        current = latest.get(event.session_id)
        if current is None or event_time(event) >= event_time(current):
            latest[event.session_id] = event
    return latest


def event_time(event: Event | dict[str, Any]) -> float:
    """Return event time as a sortable float, treating malformed times as zero."""
    if isinstance(event, Event):
        return time_from_timestamp_micros(event.timestamp_micros)
    value = event.get("time")
    return value if isinstance(value, int | float) else 0.0


def read_session_file(path: Path) -> Any:
    """Read a session file as JSON, JSONL, or text for display."""
    if not path.exists():
        return None
    if path.suffix == ".jsonl":
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                rows.append({"malformed": line})
        return rows
    if path.suffix == ".json":
        text = path.read_text(encoding="utf-8")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return path.read_text(encoding="utf-8")


def read_events() -> list[Event]:
    """Read the global event journal."""
    return event_store().list_events(Filter())


def current_session_snapshot() -> dict[str, Any]:
    """Return the current session's continuity files as structured data."""
    root = session_dir()
    return {
        "session_id": session_id(),
        "path": str(root),
        "files": {name: read_session_file(root / name) for name in SESSION_FILES},
    }


def clear_current_session() -> list[str]:
    """Remove the whole session directory and report the files it held.

    Continuity lives in more than the inspectable files: the Zeta trace store
    and the active-model selection must go too, or the next agent step resumes
    the conversation the user just cleared.
    """
    # Imported lazily: `sigil.cli` must not load zeta at import time.
    from .zeta.trace import close_default_stores

    root = session_dir()
    if not root.exists():
        return []
    close_default_stores()
    removed = [str(path) for path in sorted(root.rglob("*")) if path.is_file()]
    shutil.rmtree(root)
    return removed


RECENT_TURNS_PROMPT_LIMIT = 10
RECENT_TURN_LINE_CHARS = 120
RECENT_TURN_SNIPPET_CHARS = 500


def recent_turns(limit: int = RECENT_TURNS_LIMIT) -> list[dict[str, Any]]:
    """Return the most recent shell turns recorded by the bindings."""
    rows = read_jsonl(RECENT_TURNS_FILE)
    if limit < len(rows):
        return rows[-limit:]
    return rows


def latest_active_failure() -> dict[str, Any] | None:
    """Return the last failure only when it is still the latest shell turn."""
    failure = last_failure_or_none()
    if failure is None:
        return None
    turns = recent_turns(limit=1)
    if not turns:
        return failure
    status = turns[-1].get("status")
    if isinstance(status, int) and status != 0:
        return failure
    return None


def active_failure_context(since: float | None = None) -> str:
    """Return last-failure context when the latest shell command failed.

    A failure older than ``since`` returns nothing: the model already saw it.
    """
    failure = latest_active_failure()
    if failure is None:
        return ""
    if since is not None and event_time(failure) <= since:
        return ""
    return "Last failed command context:\n" + failure_context_prompt(failure)


def recent_turns_context(
    limit: int = RECENT_TURNS_PROMPT_LIMIT,
    since: float | None = None,
) -> str:
    """Return a compact summary of shell turns newer than ``since``, if any."""
    turns = recent_turns(limit=limit)
    if since is not None:
        turns = [turn for turn in turns if event_time(turn) > since]
    if not turns:
        return ""
    lines = ["Recent shell activity:"]
    for turn in turns:
        command = str(turn.get("command", "")).rstrip("\n")
        if len(command) > RECENT_TURN_LINE_CHARS:
            command = command[:RECENT_TURN_LINE_CHARS] + "…"
        status = turn.get("status", "?")
        lines.append(f"  {command} (exit {status})")
        stderr = compact_turn_snippet(turn.get("stderr_snippet"))
        stdout = compact_turn_snippet(turn.get("stdout_snippet"))
        if stderr:
            lines.append(f"    stderr: {stderr}")
        if stdout:
            lines.append(f"    stdout: {stdout}")
    return "\n".join(lines)


def compact_turn_snippet(value: object) -> str:
    """Return one compact line of captured command output."""
    if not isinstance(value, str) or not value:
        return ""
    text = " ".join(value.split())
    if len(text) <= RECENT_TURN_SNIPPET_CHARS:
        return text
    return text[-RECENT_TURN_SNIPPET_CHARS:]


def turn_is_skippable(command: str) -> bool:
    """Return True for commands the per-turn buffer should ignore."""
    if not command or not command.strip():
        return True
    if command[0].isspace():
        return True
    for prefix in TURN_SKIP_PREFIXES:
        if command.startswith(prefix):
            return True
    return False


def record_turn(
    command: str,
    status: int,
    cwd: str | None = None,
    stdout_snippet: str | None = None,
    stderr_snippet: str | None = None,
    duration_ms: int | None = None,
    at: float | None = None,
) -> None:
    """Persist one shell turn and fan out to failure recording on non-zero exit.

    ``at`` carries the original timestamp for turns recorded after the fact,
    such as spooled binding records; live recording stamps the current time.
    """
    if turn_is_skippable(command):
        return

    turn_cwd = cwd or os.getcwd()
    stdout_text = truncate_snippet(stdout_snippet)
    stderr_text = truncate_snippet(stderr_snippet)
    entry = {
        "id": str(uuid.uuid4()),
        "time": time.time() if at is None else at,
        "session": session_id(),
        "command": command,
        "status": status,
        "turn_cwd": turn_cwd,
        "glyph": "turn",
    }
    if stdout_text:
        entry["stdout_snippet"] = stdout_text
    if stderr_text:
        entry["stderr_snippet"] = stderr_text
    _append_recent_turn(entry)
    record_run_ledger(command, status, turn_cwd, duration_ms)

    if status != 0:
        record_failure(
            command,
            status,
            turn_cwd,
            stdout_snippet=stdout_text,
            stderr_snippet=stderr_text,
        )


def record_run_ledger(
    command: str,
    status: int,
    cwd: str,
    duration_ms: int | None,
) -> None:
    """Append the run-workflow turn and command effect for one shell command."""
    turn_id = str(uuid.uuid4())
    effect_id = str(uuid.uuid4())
    append_effect_record(
        effect_record(
            effect_id,
            turn_id=turn_id,
            kind=EFFECT_KIND_COMMAND,
            staged=False,
            command=command,
            exit_status=status,
            duration_ms=duration_ms,
        )
    )
    turn = turn_record(
        turn_id,
        workflow=RUN_WORKFLOW,
        objective=command,
        contract=turn_contract(RUN_WORKFLOW, (), staged=False),
        outcome=TURN_OUTCOME_EXECUTED if status == 0 else TURN_OUTCOME_FAILED,
        effect_ids=[effect_id],
    )
    turn["cwd"] = cwd
    append_turn_record(turn)


SHELL_TURN_SPOOL_FILE = "shell-turns.spool"
SPOOL_FIELD_SEPARATOR = "\x1f"
SPOOL_RECORD_SEPARATOR = "\x1e"
SPOOL_ORPHAN_AGE_SECONDS = 60.0


def ingest_spooled_turns() -> int:
    """Record shell turns spooled by the bindings; return how many landed.

    The binding appends raw spool records with zero subprocess cost; every
    CLI invocation calls this matching reader, so spooled turns are recorded
    before anything reads recent turns or failure context. The spool is
    claimed by rename, which keeps concurrent CLI processes from
    double-recording the same records.
    """
    recorded = 0
    for path in _claimed_spool_files():
        recorded += _record_spool_text(
            path.read_text(encoding="utf-8", errors="replace")
        )
        path.unlink(missing_ok=True)
    return recorded


def _claimed_spool_files() -> list[Path]:
    root = session_dir()
    spool = root / SHELL_TURN_SPOOL_FILE
    claim = spool.with_name(f"{spool.name}.{os.getpid()}.ingesting")
    claimed: list[Path] = []
    try:
        spool.rename(claim)
        claimed.append(claim)
    except OSError:
        pass
    for orphan in root.glob(f"{SHELL_TURN_SPOOL_FILE}.*.ingesting"):
        if orphan == claim:
            continue
        try:
            age = time.time() - orphan.stat().st_mtime
        except OSError:
            continue
        if age > SPOOL_ORPHAN_AGE_SECONDS:
            claimed.append(orphan)
    return claimed


def _record_spool_text(text: str) -> int:
    recorded = 0
    for record in text.split(SPOOL_RECORD_SEPARATOR):
        fields = record.split(SPOOL_FIELD_SEPARATOR)
        if len(fields) != 4:
            continue
        raw_time, command, raw_status, cwd = fields
        try:
            status = int(raw_status)
        except ValueError:
            continue
        try:
            at = float(raw_time)
        except ValueError:
            at = time.time()
        record_turn(command, status, cwd, at=at)
        recorded += 1
    return recorded


def _append_recent_turn(entry: dict[str, Any]) -> None:
    root = session_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = root / RECENT_TURNS_FILE
    append_jsonl_line(path, entry)
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    if len(lines) > RECENT_TURNS_LIMIT:
        write_text_atomic(path, "\n".join(lines[-RECENT_TURNS_LIMIT:]) + "\n")
