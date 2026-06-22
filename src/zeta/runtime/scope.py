"""Execution resource scope for Zeta runtime calls."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zeta.capabilities.registry import CapabilityRegistry
    from zeta.store.events import EventStoreProtocol
    from zeta.store.substrate import Store


@dataclass(frozen=True)
class SessionScope:
    """Runtime dependencies for one durable session scope."""

    session_id: str
    event_sink: EventStoreProtocol
    trace_store: Store
    tool_registry: CapabilityRegistry
    state_dir: Path
    session_dir: Path
