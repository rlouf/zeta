"""Runtime context for Zeta calls."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from zeta.records.stores import (
    SqliteEventStore,
    SqliteObjectStore,
    event_store_path,
    zeta_sqlite_path,
)

if TYPE_CHECKING:
    from zeta.capabilities.registry import CapabilityRegistry
    from zeta.records.stores import EventStoreProtocol, Store


@dataclass(frozen=True)
class RuntimeContext:
    """Runtime dependencies for one durable continuity scope."""

    session_id: str
    event_sink: EventStoreProtocol
    trace_store: Store
    tool_registry: CapabilityRegistry
    state_dir: Path
    session_dir: Path


def zeta_state_dir() -> Path:
    root = os.environ.get("ZETA_STATE_DIR")
    return Path(root).expanduser() if root else Path.home() / ".zeta"


def default_session() -> RuntimeContext:
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
) -> RuntimeContext:
    """Build the default Zeta runtime dependencies for one session scope."""

    if tool_registry is None:
        from zeta.capabilities.registry import registry as tool_registry

    return RuntimeContext(
        session_id=session_id,
        event_sink=SqliteEventStore(event_store_path(state_dir)),
        trace_store=SqliteObjectStore(
            zeta_sqlite_path(state_dir), session_id=session_id
        ),
        tool_registry=tool_registry,
        state_dir=state_dir,
        session_dir=session_dir,
    )
