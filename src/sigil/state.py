"""Persistent state for Sigil sessions.

Global state captures audit/debug events. Session state captures continuity for
one shell, so multiple terminal windows do not overwrite each other's comma
context.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

ANSWER_HISTORY = "last-answer.jsonl"
EVENT_LOG_MAX_BYTES = 10 * 1024 * 1024


def state_dir() -> Path:
    """Return the global Sigil state directory."""
    base = os.environ.get("SIGIL_STATE_DIR")
    if base:
        return Path(base)
    return Path.home() / ".sigil"


def session_id() -> str:
    """Return the current shell session identifier."""
    return os.environ.get("SIGIL_SESSION_ID") or "default"


def session_dir() -> Path:
    """Return the directory that stores continuity for this shell session."""
    base = os.environ.get("SIGIL_SESSION_DIR")
    if base:
        return Path(base)
    return state_dir() / "sessions" / session_id()


def append_jsonl_line(path: Path, payload: dict[str, Any]) -> None:
    """Append one JSONL payload as a single unbuffered write.

    Concurrent shells append to the same files; one write(2) call per line
    keeps lines from interleaving regardless of payload size.
    """
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    with path.open("ab", buffering=0) as f:
        f.write(line.encode("utf-8"))


def rotate_oversized_log(path: Path) -> None:
    """Move a log aside once it exceeds the size cap, keeping one generation."""
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size < EVENT_LOG_MAX_BYTES:
        return
    try:
        path.replace(path.with_name(f"{path.name}.1"))
    except OSError:
        pass


def append_event(event: dict[str, Any]) -> dict[str, Any]:
    """Append a global audit/debug event with session metadata."""
    root = state_dir()
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": str(uuid.uuid4()),
        "time": time.time(),
        "cwd": os.getcwd(),
        "session": session_id(),
        **event,
    }
    log_path = root / "events.jsonl"
    rotate_oversized_log(log_path)
    append_jsonl_line(log_path, payload)
    return payload


def write_text_atomic(path: Path, text: str) -> None:
    """Replace a file through a unique fsynced tmp file in the same directory.

    Unique tmp names keep concurrent writers from clobbering each other's
    half-written files; fsync before rename keeps a crash from leaving an
    empty renamed file behind.
    """
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f"{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(f.name, path)


def write_json(name: str, value: Any) -> None:
    """Atomically write a session-scoped JSON document."""
    root = session_dir()
    root.mkdir(parents=True, exist_ok=True)
    write_text_atomic(
        root / name, json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    )


def remove_json(name: str) -> bool:
    """Remove a session-scoped JSON document if it exists."""
    path = session_dir() / name
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def append_jsonl(name: str, event: dict[str, Any]) -> dict[str, Any]:
    """Append a session-scoped JSONL event."""
    root = session_dir()
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": str(uuid.uuid4()),
        "time": time.time(),
        "cwd": os.getcwd(),
        "session": session_id(),
        **event,
    }
    append_jsonl_line(root / name, payload)
    return payload


def write_jsonl(name: str, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace a session-scoped JSONL file atomically."""
    root = session_dir()
    root.mkdir(parents=True, exist_ok=True)
    payloads = []
    for event in events:
        payloads.append(
            {
                "id": str(uuid.uuid4()),
                "time": time.time(),
                "cwd": os.getcwd(),
                "session": session_id(),
                **event,
            }
        )
    write_text_atomic(
        root / name,
        "".join(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
            for payload in payloads
        ),
    )
    return payloads


def read_jsonl(name: str) -> list[dict[str, Any]]:
    """Read a session-scoped JSONL file, skipping malformed lines."""
    return read_jsonl_path(session_dir() / name)


def read_jsonl_path(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file at an explicit path, skipping malformed lines."""
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except Exception:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def read_json(name: str) -> Any | None:
    """Read a session-scoped JSON document if it exists and parses."""
    path = session_dir() / name
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return value
