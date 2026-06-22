"""Assemble local Zeta services for this process."""

from __future__ import annotations

import os
from pathlib import Path

from agents.loader import load_specs_recursive
from agents.spec import AgentSpec
from zeta.agents.runtime import compile_agent_definitions
from zeta.capabilities.registry import CapabilityRegistry
from zeta.orchestration import worker
from zeta.orchestration.dispatch import ExecutableAgent
from zeta.records.stores import (
    SqliteEventStore,
    SqliteStore,
    event_store_path,
    zeta_sqlite_path,
)
from zeta.run.threads import SessionScope


def zeta_state_dir() -> Path:
    root = os.environ.get("ZETA_STATE_DIR")
    return Path(root).expanduser() if root else Path.home() / ".zeta"


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
) -> worker.RuntimeServices:
    resolved_project_root = project_root.expanduser().resolve()
    resolved_state_dir = (
        state_dir.expanduser().resolve()
        if state_dir is not None
        else resolved_project_root / ".zeta"
    )
    specs = project_specs(resolved_project_root)
    return worker.RuntimeServices(
        project_root=resolved_project_root,
        state_dir=resolved_state_dir,
        events=SqliteEventStore(event_store_path(resolved_state_dir)),
        specs=specs,
        executors=executors_for_specs(specs),
    )


def project_specs(project_root: Path) -> tuple[AgentSpec, ...]:
    agents_dir = project_root / "agents"
    if not agents_dir.exists():
        return ()
    return tuple(load_specs_recursive(agents_dir))


def executors_for_specs(specs: tuple[AgentSpec, ...]) -> tuple[ExecutableAgent, ...]:
    return tuple(agent for spec in specs for agent in compile_agent_definitions(spec))
