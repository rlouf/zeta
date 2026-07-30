import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
from connectors import EventConnectorRegistry
from zeta.authoring.spec import ExecutorSpec
from zeta.capabilities.registry import CapabilityRegistry
from zeta.events import DraftEvent
from zeta.harness.dispatch import QueueingDispatcher
from zeta.harness.project import (
    ProjectSnapshotUnavailable,
    agent_from_manifest,
    agent_manifest,
    load_project_snapshot,
    load_recorded_project_snapshot,
    record_project_snapshot,
)
from zeta.harness.routing import (
    AgentDefinition,
    EventPattern,
    ExecutableAgent,
    compile_agent_definition,
)
from zeta.harness.store import RuntimeEventStore
from zeta.journal.store import Filter
from zeta.loop.outcomes import AgentRunResult
from zeta.models.profiles import ModelSelection


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


def test_project_snapshot_preserves_executor_config(tmp_path: Path) -> None:
    spec = load_snapshot(write_snapshot_project(tmp_path)).project.specs[0]
    config = {
        "app": "zeta-tools",
        "options": {
            "regions": ["eu-west", "us-east"],
            "retries": 3,
            "enabled": True,
            "timeout": 1.5,
            "fallback": None,
        },
    }
    manifest = agent_manifest(replace(spec, executor=ExecutorSpec("remote", config)))

    restored = agent_from_manifest(manifest)

    assert restored.executor == ExecutorSpec("remote", config)
    assert restored.executor.config is not config
    assert restored.executor.config["options"] is not config["options"]
    assert (
        restored.executor.config["options"]["regions"]
        is not config["options"]["regions"]
    )


def test_project_snapshot_rejects_non_json_executor_config(tmp_path: Path) -> None:
    spec = load_snapshot(write_snapshot_project(tmp_path)).project.specs[0]
    manifest = agent_manifest(spec)
    manifest["executor"]["config"] = {"threshold": float("nan")}

    with pytest.raises(
        ProjectSnapshotUnavailable,
        match="invalid tool executor config",
    ):
        agent_from_manifest(manifest)


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

    async def successful_agent_loop(*_args: object) -> AgentRunResult:
        return AgentRunResult(final_answer="done")

    executor = compile_agent_definition(
        spec,
        agent_loop=successful_agent_loop,
        project_generation=snapshot.generation_id,
        execution_manifest=execution_manifest,
    )
    store = RuntimeEventStore.open(tmp_path / "zeta.sqlite3")
    dispatcher = QueueingDispatcher(store, executors=[executor])
    outcome = asyncio.run(
        dispatcher.publish_event(DraftEvent("work.requested", "test", {}))
    )
    asyncio.run(dispatcher.drain())

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
    del current
    router = QueueingDispatcher(store, executors=[old])
    asyncio.run(router.publish_event(DraftEvent("work.requested", "test", {})))
    lifecycle = asyncio.run(router.drain())

    assert calls == ["old"]
    assert lifecycle[-1].payload["result"] == {"version": "old"}
