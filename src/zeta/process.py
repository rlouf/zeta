"""Assemble local Zeta services for this process."""

from __future__ import annotations

from pathlib import Path

from zeta.agents.loader import load_specs_recursive
from zeta.agents.spec import AgentSpec
from zeta.orchestration import worker
from zeta.orchestration.agents import ExecutableAgent, compile_agent_definitions
from zeta.records.stores import (
    SqliteEventStore,
    event_store_path,
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
    specs = load_project_specs(resolved_project_root)
    return worker.RuntimeServices(
        project_root=resolved_project_root,
        state_dir=resolved_state_dir,
        events=SqliteEventStore(event_store_path(resolved_state_dir)),
        specs=specs,
        executors=executors_for_specs(specs),
    )


def load_project_specs(project_root: Path) -> tuple[AgentSpec, ...]:
    agents_dir = project_root / "agents"
    if not agents_dir.exists():
        return ()
    return tuple(load_specs_recursive(agents_dir))


def executors_for_specs(specs: tuple[AgentSpec, ...]) -> tuple[ExecutableAgent, ...]:
    return tuple(agent for spec in specs for agent in compile_agent_definitions(spec))
