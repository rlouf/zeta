"""Global paths and durable events for Sigil.

Session-local continuity lives in `sigil.sessions`; this module owns the shared
state directory and the frontend event journal.
"""

from __future__ import annotations

import hashlib
import os
import re
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

SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")


def state_dir() -> Path:
    """Return the global Sigil state directory."""
    base = os.environ.get("SIGIL_STATE_DIR")
    if base:
        return Path(base)
    return Path.home() / ".sigil"


def _session_path_component(raw: str) -> str:
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
    return _session_path_component(os.environ.get("SIGIL_SESSION_ID") or "default")


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
    return state_dir() / "sessions" / _session_path_component(raw)


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
