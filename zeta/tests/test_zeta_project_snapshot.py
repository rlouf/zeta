import asyncio
import json
from pathlib import Path

from connectors import EventConnectorRegistry
from zeta.capabilities.registry import CapabilityRegistry
from zeta.models.profiles import ModelSelection
from zeta.records.events import DraftEvent
from zeta.records.stores.event_store import Filter
from zeta.run.outcomes import AgentRunResult
from zetad.agents import (
    AgentDefinition,
    EventPattern,
    ExecutableAgent,
    compile_agent_definition,
)
from zetad.dispatch import EventDispatcher
from zetad.project import (
    load_project_snapshot,
    load_recorded_project_snapshot,
    record_project_snapshot,
)
from zetad.store import RuntimeEventStore


def write_snapshot_project(root: Path, *, description: str = "Handles work.") -> Path:
    agents = root / "agents"
    events = agents / "events"
    events.mkdir(parents=True)
    (agents / "worker.md").write_text(
        f"""---
name: Worker
description: {description}
accepts:
  - work.requested
---
Handle the work.
""",
        encoding="utf-8",
    )
    (events / "work.requested.json").write_text(
        json.dumps({"type": "object", "additionalProperties": False}),
        encoding="utf-8",
    )
    return agents


def load_snapshot(agents: Path):
    return load_project_snapshot(
        agents,
        registry=EventConnectorRegistry(),
        tool_registry=CapabilityRegistry(),
        model_selection=ModelSelection(
            profile="local",
            model="test-model",
            url="http://model.test/v1",
        ),
    )


def test_project_snapshot_generation_is_content_addressed(tmp_path: Path) -> None:
    agents = write_snapshot_project(tmp_path)

    first = load_snapshot(agents)
    second = load_snapshot(agents)
    (agents / "worker.md").write_text(
        (agents / "worker.md")
        .read_text(encoding="utf-8")
        .replace("Handles work.", "Handles changed work."),
        encoding="utf-8",
    )
    changed = load_snapshot(agents)

    assert first.generation_id == second.generation_id
    assert first.manifest == second.manifest
    assert changed.generation_id != first.generation_id


def test_project_snapshot_is_recorded_once_per_generation(tmp_path: Path) -> None:
    snapshot = load_snapshot(write_snapshot_project(tmp_path))
    store = RuntimeEventStore.open(tmp_path / "zeta.sqlite3")

    first = record_project_snapshot(store, snapshot)
    second = record_project_snapshot(store, snapshot)

    assert first.id == second.id
    assert first.payload["generation_id"] == snapshot.generation_id
    assert len(store.list_events(Filter(event_type=first.event_type))) == 1

    restored = load_recorded_project_snapshot(
        store,
        snapshot.generation_id,
        registry=EventConnectorRegistry(),
    )
    assert restored.generation_id == snapshot.generation_id
    assert restored.project.specs == snapshot.project.specs


def test_attempt_records_project_and_execution_manifests(tmp_path: Path) -> None:
    snapshot = load_snapshot(write_snapshot_project(tmp_path))
    spec = snapshot.project.specs[0]
    execution_manifest = snapshot.execution_manifest(spec)

    async def run_turn(*_args, **_kwargs) -> AgentRunResult:
        return AgentRunResult(final_answer="done")

    executor = compile_agent_definition(
        spec,
        run_turn=run_turn,
        project_generation=snapshot.generation_id,
        execution_manifest=execution_manifest,
    )
    store = RuntimeEventStore.open(tmp_path / "zeta.sqlite3")
    outcome = asyncio.run(
        EventDispatcher(store, executors=[executor]).publish_and_run(
            DraftEvent("work.requested", "test", {})
        )
    )

    queue_item = store.list_queue_items()[0]
    attempt = store.list_attempts()[0]
    started = store.list_events(Filter(event_type="runtime.attempt.started"))[0]

    assert queue_item["event_id"] == outcome.event.id
    assert queue_item["project_generation"] == snapshot.generation_id
    assert attempt["project_generation"] == snapshot.generation_id
    assert attempt["execution_manifest_id"] == execution_manifest["id"]
    assert attempt["execution_manifest"] == execution_manifest
    assert started.payload["execution_manifest"] == execution_manifest


def test_dispatcher_selects_executor_for_routed_generation(tmp_path: Path) -> None:
    calls: list[str] = []

    async def run_old(_invocation) -> dict[str, str]:
        calls.append("old")
        return {"version": "old"}

    async def run_current(_invocation) -> dict[str, str]:
        calls.append("current")
        return {"version": "current"}

    old = ExecutableAgent(
        AgentDefinition(
            "worker",
            (EventPattern("work.requested"),),
            project_generation="project:old",
        ),
        run_old,
    )
    current = ExecutableAgent(
        AgentDefinition(
            "worker",
            (EventPattern("work.requested"),),
            project_generation="project:current",
        ),
        run_current,
    )
    store = RuntimeEventStore.open(tmp_path / "zeta.sqlite3")
    router = EventDispatcher(store, executors=[old])
    triggering = asyncio.run(
        router.publish_event(DraftEvent("work.requested", "test", {}))
    ).event
    routed = asyncio.run(router.route(triggering)).queue_items[0]

    lifecycle = asyncio.run(
        EventDispatcher(store, executors=[current, old]).run_queue_item(
            routed.queue_item_id
        )
    )

    assert calls == ["old"]
    assert lifecycle[-1].payload["result"] == {"version": "old"}
