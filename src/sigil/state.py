"""Persistent state for Sigil sessions.

Global state captures audit/debug events. Session state captures continuity for
one shell, so multiple terminal windows do not overwrite each other's comma
context.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zeta.events import (
    EVENT_STORE_NAME,
    Filter,
    event_log_causal_chain,
    event_log_children,
    event_log_turn_events,
    publish_event_payload_to_log,
    read_event_log,
)

if TYPE_CHECKING:
    from zeta.events import Event

EVENT_LOG_MAX_BYTES = 10 * 1024 * 1024
SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")


def state_dir() -> Path:
    """Return the global Sigil state directory."""
    base = os.environ.get("SIGIL_STATE_DIR")
    if base:
        return Path(base)
    return Path.home() / ".sigil"


def safe_session_id(raw: str) -> str:
    """Map a raw session id onto a safe path component.

    The id becomes a path component under the state directory, so values
    that could escape it (separators, `..`, control characters) map to a
    deterministic digest instead of being used verbatim.
    """
    if SESSION_ID_PATTERN.fullmatch(raw) and raw not in {".", ".."}:
        return raw
    return "unsafe-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def session_id() -> str:
    """Return the current shell session identifier."""
    return safe_session_id(os.environ.get("SIGIL_SESSION_ID") or "default")


def session_dir(session_id: str | None = None) -> Path:
    """Return the directory that stores continuity for one shell session.

    Without an explicit id this is the current session, honoring the
    `SIGIL_SESSION_DIR` override. An explicit id names another session
    under the state directory; the override never applies to it.
    """
    if session_id is None:
        base = os.environ.get("SIGIL_SESSION_DIR")
        if base:
            return Path(base)
    raw = session_id or os.environ.get("SIGIL_SESSION_ID") or "default"
    return state_dir() / "sessions" / safe_session_id(raw)


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


def _with_envelope(event: dict[str, Any]) -> dict[str, Any]:
    """Stamp default cwd onto session-local JSONL event data."""
    return {
        "cwd": os.getcwd(),
        **event,
    }


def _session_root() -> Path:
    """Return the session directory, creating it if needed."""
    root = session_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root


def event_store_path() -> Path:
    """Return Sigil's frontend event journal path."""
    return state_dir() / EVENT_STORE_NAME


def read_events() -> list[Event]:
    """Read Sigil's frontend event journal."""
    return read_event_log(event_store_path(), Filter())


def history_view(events: list[Event] | None = None) -> Any:
    """Return a Zeta history view over Sigil's durable events."""
    from zeta.history import HistoryView

    if events is not None:
        return HistoryView(events)
    return HistoryView.from_store(event_store_path())


def event_children(event_id: str, *, limit: int | None = None) -> list[Event]:
    return event_log_children(event_store_path(), event_id, limit=limit)


def causal_chain(event_id: str) -> list[Event]:
    return event_log_causal_chain(event_store_path(), event_id)


def events_for_turn(turn_id: str) -> list[Event]:
    return event_log_turn_events(event_store_path(), turn_id)


def append_event(event: dict[str, Any]) -> Event:
    """Append a global audit/debug event with session metadata."""
    return publish_event_payload_to_log(
        event_store_path(),
        event,
        session_id=session_id(),
        cwd=os.getcwd(),
    )


def append_prompt_submitted_event(event: dict[str, Any]) -> Event:
    prompt_event = dict(event)
    prompt_event["type"] = "sigil.prompt.submitted"
    return append_event(prompt_event)


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
    write_text_atomic(
        _session_root() / name, json.dumps(value, ensure_ascii=False, indent=2) + "\n"
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
    payload = _with_envelope(event)
    append_jsonl_line(_session_root() / name, payload)
    return payload


def write_jsonl(name: str, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace a session-scoped JSONL file atomically."""
    payloads = [_with_envelope(event) for event in events]
    write_text_atomic(
        _session_root() / name,
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
