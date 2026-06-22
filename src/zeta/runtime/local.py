"""Local process resource construction for Zeta runtime scopes."""

from __future__ import annotations

import os
from pathlib import Path

from zeta.capabilities.registry import CapabilityRegistry
from zeta.runtime.config import zeta_state_dir
from zeta.runtime.scope import SessionScope


def default_session() -> SessionScope:
    """Return the default process session for pure Zeta runtime calls."""

    state_dir = zeta_state_dir()
    session_id = os.environ.get("ZETA_SESSION_ID") or "default"
    return session_for_id(
        session_id=session_id,
        state_dir=state_dir,
        session_dir=state_dir / "sessions" / session_id,
    )


def session_for_id(
    *,
    session_id: str,
    state_dir: Path,
    session_dir: Path,
    tool_registry: CapabilityRegistry | None = None,
) -> SessionScope:
    """Build the default Zeta runtime dependencies for one session scope."""

    from zeta.store.events import SqliteEventStore, event_store_path
    from zeta.store.substrate import SqliteStore, zeta_sqlite_path

    if tool_registry is None:
        from zeta.capabilities.registry import registry as tool_registry

    return SessionScope(
        session_id=session_id,
        event_sink=SqliteEventStore(event_store_path(state_dir)),
        trace_store=SqliteStore(zeta_sqlite_path(state_dir), session_id=session_id),
        tool_registry=tool_registry,
        state_dir=state_dir,
        session_dir=session_dir,
    )
