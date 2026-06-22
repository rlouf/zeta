"""Local process resource construction for Zeta runtime scopes."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

from agents.loader import load_specs_recursive
from zeta.agents.runtime import compile_agent_definitions
from zeta.capabilities.registry import CapabilityRegistry
from zeta.dispatch import (
    EventDispatcher,
    QueueItemSnapshot,
    RegisteredAgent,
    queue_item_snapshots,
)
from zeta.kernel.events import Event
from zeta.runtime.config import zeta_state_dir
from zeta.runtime.scope import SessionScope
from zeta.store.events import Filter, SqliteEventStore, event_store_path


@dataclass(frozen=True)
class RuntimeServices:
    """Project-local runtime resources owned by a worker loop."""

    project_root: Path
    state_dir: Path
    events: SqliteEventStore
    agents: tuple[RegisteredAgent, ...]

    def close(self) -> None:
        self.events.close()


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


def build_runtime(
    *,
    project_root: Path,
    state_dir: Path | None = None,
) -> RuntimeServices:
    resolved_project_root = project_root.expanduser().resolve()
    resolved_state_dir = (
        state_dir.expanduser().resolve()
        if state_dir is not None
        else resolved_project_root / ".zeta"
    )
    return RuntimeServices(
        project_root=resolved_project_root,
        state_dir=resolved_state_dir,
        events=SqliteEventStore(event_store_path(resolved_state_dir)),
        agents=project_agents(resolved_project_root),
    )


def project_agents(project_root: Path) -> tuple[RegisteredAgent, ...]:
    agents_dir = project_root / "agents"
    if not agents_dir.exists():
        return ()
    return tuple(
        agent
        for spec in load_specs_recursive(agents_dir)
        for agent in compile_agent_definitions(spec)
    )


def is_runtime_event(event: Event) -> bool:
    return event.event_type.startswith(("runtime.queue_item.", "runtime.attempt."))


def next_available_queue_item(
    snapshots: list[QueueItemSnapshot],
) -> QueueItemSnapshot | None:
    for snapshot in snapshots:
        if snapshot.status == "available":
            return snapshot
    return None


def next_unrouted_event(
    events: list[Event],
    snapshots: list[QueueItemSnapshot],
) -> Event | None:
    routed_event_ids = {snapshot.event_id for snapshot in snapshots}
    for event in events:
        if is_runtime_event(event):
            continue
        if event.id not in routed_event_ids:
            return event
    return None


async def run_once(runtime: RuntimeServices) -> str:
    dispatcher = EventDispatcher(runtime.events, agents=runtime.agents)
    events = runtime.events.list_events(Filter())
    snapshots = queue_item_snapshots(events)
    available = next_available_queue_item(snapshots)
    if available is not None:
        await dispatcher.run_queue_item(available.queue_item_id)
        return f"ran {available.queue_item_id}"
    event = next_unrouted_event(events, snapshots)
    if event is None:
        return "queue empty"
    route = await dispatcher.route(event)
    if not route.queue_items:
        return f"routed {event.id}"
    queue_item = route.queue_items[0]
    await dispatcher.run_queue_item(queue_item)
    return f"ran {queue_item.queue_item_id}"


async def run_forever(
    runtime: RuntimeServices,
    *,
    poll_interval_seconds: float = 1.0,
    stop_event: asyncio.Event | None = None,
) -> None:
    while stop_event is None or not stop_event.is_set():
        outcome = await run_once(runtime)
        if outcome == "queue empty":
            await asyncio.sleep(poll_interval_seconds)
