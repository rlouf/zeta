"""Inspection helpers for Sigil's hidden session state.

Session state is useful only if it can be inspected. These helpers make the
current shell's continuity files visible without exposing mutation outside the
explicit `clear` command.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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
