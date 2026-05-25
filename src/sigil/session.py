"""Inspection helpers for Sigil's hidden session state.

Session state is useful only if it can be inspected. These helpers make the
current shell's continuity files visible without exposing mutation outside the
explicit `clear` command.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .security import normalize_trust_record
from .state import session_dir, session_id, state_dir

SESSION_FILES = (
    "last-command.json",
    "last-question.jsonl",
    "last-tools.jsonl",
    "last-failure.json",
    "last-fix.json",
)


def session_paths() -> dict[str, str]:
    """Return the global and current-session paths users need for debugging."""
    return {
        "state": str(state_dir()),
        "session": str(session_dir()),
        "session_id": session_id(),
        "events": str(state_dir() / "events.jsonl"),
    }


def known_sessions() -> list[dict[str, Any]]:
    """List session directories with coarse file presence metadata."""
    root = state_dir() / "sessions"
    if not root.exists():
        return []
    sessions = []
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if not path.is_dir():
            continue
        sessions.append(
            {
                "session_id": path.name,
                "path": str(path),
                "files": sorted(item.name for item in path.iterdir() if item.is_file()),
            }
        )
    return sessions


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


def session_summary(limit: int = 8) -> dict[str, Any]:
    """Return a read-only summary of the current Sigil session."""
    snapshot = current_session_snapshot()
    files = snapshot["files"]
    events = [
        event for event in read_event_log() if event.get("session") == session_id()
    ][-limit:]

    last_question = files.get("last-question.jsonl") or []
    last_tools = files.get("last-tools.jsonl") or []
    summary = {
        "session_id": snapshot["session_id"],
        "path": snapshot["path"],
        "continuity": {
            "has_command": files.get("last-command.json") is not None,
            "has_failure": files.get("last-failure.json") is not None,
            "has_fix": files.get("last-fix.json") is not None,
            "question_turns": len(last_question)
            if isinstance(last_question, list)
            else 0,
            "tool_events": len(last_tools) if isinstance(last_tools, list) else 0,
        },
        "recent_events": [
            {
                "id": event.get("id", ""),
                "type": event.get("type", "event"),
                "glyph": event.get("glyph", ""),
                "integrity": event.get("integrity", "unknown"),
                "capability": event.get("capability", "none"),
                "taint": event.get("taint", []),
                "inputs": event.get("inputs", []),
            }
            for event in events
        ],
    }
    return summary


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
