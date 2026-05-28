"""Inspection helpers for Sigil's hidden session state.

Session state is useful only if it can be inspected. These helpers make the
current shell's continuity files visible without exposing mutation outside the
explicit `clear` command.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .security import create_trust_metadata, normalize_trust_record
from .state import session_dir, session_id, state_dir

SESSION_FILES = (
    "last-question.jsonl",
    "last-tools.jsonl",
    "last-bash-handoff.jsonl",
    "pending-bash-handoff.jsonl",
    "last-failure.json",
    "last-act.jsonl",
    "last-plan.jsonl",
    "recent-turns.jsonl",
)

RECENT_TURNS_FILE = "recent-turns.jsonl"
RECENT_TURNS_LIMIT = 50
TURN_SKIP_PREFIXES = (",", "?", "sigil ", "__sigil_")


def session_paths() -> dict[str, str]:
    """Return the global and current-session paths users need for debugging."""
    return {
        "state": str(state_dir()),
        "session": str(session_dir()),
        "session_id": session_id(),
        "events": str(state_dir() / "events.jsonl"),
    }


def known_sessions() -> list[dict[str, Any]]:
    """List session directories with coarse file presence and event metadata."""
    root = state_dir() / "sessions"
    if not root.exists():
        return []
    latest_by_session = latest_events_by_session(read_event_log())
    sessions = []
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if not path.is_dir():
            continue
        latest = latest_by_session.get(path.name, {})
        sessions.append(
            {
                "session_id": path.name,
                "path": str(path),
                "files": sorted(item.name for item in path.iterdir() if item.is_file()),
                "last_event_time": latest.get("time"),
                "last_event_type": latest.get("type"),
                "last_cwd": latest.get("cwd"),
            }
        )
    return sessions


def latest_events_by_session(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return the newest event seen for each session id."""
    latest: dict[str, dict[str, Any]] = {}
    for event in events:
        event_session = event.get("session")
        if not isinstance(event_session, str):
            continue
        current = latest.get(event_session)
        if current is None or event_time(event) >= event_time(current):
            latest[event_session] = event
    return latest


def event_time(event: dict[str, Any]) -> float:
    """Return event time as a sortable float, treating malformed times as zero."""
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
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return path.read_text(encoding="utf-8")
    return path.read_text(encoding="utf-8")


def read_event_log() -> list[dict[str, Any]]:
    """Read the global event log, skipping malformed lines."""
    path = state_dir() / "events.jsonl"
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(normalize_trust_record(event))
    return events


def latest_event_id(events: list[dict[str, Any]]) -> str | None:
    """Return the latest event id for the current session, falling back globally."""
    current_session = session_id()
    for event in reversed(events):
        event_id = event.get("id")
        if event.get("session") == current_session and isinstance(event_id, str):
            return event_id
    for event in reversed(events):
        event_id = event.get("id")
        if isinstance(event_id, str):
            return event_id
    return None


def event_lineage(event_id: str | None = None) -> dict[str, Any]:
    """Return an event and the transitive inputs it inherited from."""
    events = read_event_log()
    selected = event_id or latest_event_id(events)
    by_id = {str(event["id"]): event for event in events if event.get("id")}
    if not selected:
        return {"event_id": None, "nodes": [], "missing_inputs": []}

    nodes = []
    missing = []
    seen = set()
    queue = [(selected, 0)]
    while queue:
        current, depth = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        event = by_id.get(current)
        if event is None:
            missing.append(current)
            continue
        nodes.append({"id": current, "depth": depth, "event": event})
        for input_id in event.get("inputs", []):
            if isinstance(input_id, str) and input_id:
                queue.append((input_id, depth + 1))

    return {"event_id": selected, "nodes": nodes, "missing_inputs": missing}


def current_session_snapshot() -> dict[str, Any]:
    """Return the current session's continuity files as structured data."""
    root = session_dir()
    return {
        "session_id": session_id(),
        "path": str(root),
        "files": {name: read_session_file(root / name) for name in SESSION_FILES},
    }


def clear_current_session() -> list[str]:
    """Remove current-session continuity files and report what changed."""
    root = session_dir()
    removed = []
    for name in SESSION_FILES:
        path = root / name
        if path.exists():
            path.unlink()
            removed.append(str(path))
    return removed


RECENT_TURNS_PROMPT_LIMIT = 10
RECENT_TURN_LINE_CHARS = 120
RECENT_TURN_SNIPPET_CHARS = 500


def recent_turns(limit: int = RECENT_TURNS_LIMIT) -> list[dict[str, Any]]:
    """Return the most recent shell turns recorded by the bindings."""
    path = session_dir() / RECENT_TURNS_FILE
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            rows.append(normalize_trust_record(event))
    if limit < len(rows):
        return rows[-limit:]
    return rows


def recent_turns_context(limit: int = RECENT_TURNS_PROMPT_LIMIT) -> str:
    """Return a compact summary of the most recent shell turns, if any."""
    turns = recent_turns(limit=limit)
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
) -> None:
    """Persist one shell turn and fan out to failure recording on non-zero exit."""
    if turn_is_skippable(command):
        return

    turn_cwd = cwd or os.getcwd()
    from .failure import truncate_snippet

    stdout_text = truncate_snippet(stdout_snippet)
    stderr_text = truncate_snippet(stderr_snippet)
    security = create_trust_metadata(
        glyph="turn",
        mode="read-only",
    )
    entry = {
        "id": _new_event_id(),
        "time": time.time(),
        "session": session_id(),
        "command": command,
        "status": status,
        "turn_cwd": turn_cwd,
        **security,
    }
    if stdout_text:
        entry["stdout_snippet"] = stdout_text
    if stderr_text:
        entry["stderr_snippet"] = stderr_text
    _append_recent_turn(entry)

    if status != 0:
        from .failure import record_failure

        record_failure(
            command,
            status,
            turn_cwd,
            stdout_snippet=stdout_text,
            stderr_snippet=stderr_text,
        )


def _new_event_id() -> str:
    import uuid

    return str(uuid.uuid4())


def _append_recent_turn(entry: dict[str, Any]) -> None:
    root = session_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = root / RECENT_TURNS_FILE

    existing: list[str] = []
    if path.exists():
        existing = [
            line for line in path.read_text(encoding="utf-8").splitlines() if line
        ]

    serialized = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
    existing.append(serialized)
    if len(existing) > RECENT_TURNS_LIMIT:
        existing = existing[-RECENT_TURNS_LIMIT:]

    tmp = root / f"{RECENT_TURNS_FILE}.tmp"
    tmp.write_text("\n".join(existing) + "\n", encoding="utf-8")
    tmp.replace(path)
