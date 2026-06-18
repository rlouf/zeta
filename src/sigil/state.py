"""Global paths and durable events for Sigil.

Session-local continuity lives in `sigil.sessions`; this module owns the shared
state directory and the frontend event journal.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zeta.store.events import (
    EVENT_STORE_NAME,
    Filter,
    event_log_causal_chain,
    event_log_children,
    event_log_turn_events,
    read_event_log,
)
from zeta.timeline import publish_event_payload_to_log

if TYPE_CHECKING:
    from zeta.events import Event


def state_dir() -> Path:
    """Return the global Sigil state directory."""
    base = os.environ.get("SIGIL_STATE_DIR")
    if base:
        return Path(base)
    return Path.home() / ".sigil"


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
    from sigil.sessions import session_id

    payload = {"source": "sigil", **event}
    return publish_event_payload_to_log(
        event_store_path(),
        payload,
        session_id=session_id(),
        cwd=os.getcwd(),
    )


def append_prompt_submitted_event(event: dict[str, Any]) -> Event:
    prompt_event = dict(event)
    prompt_event["type"] = "zeta.prompt.submitted"
    return append_event(prompt_event)
