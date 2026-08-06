import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
from connectors import EventConnectorRegistry
from zeta.authoring.spec import ExecutorSpec
from zeta.capabilities.registry import CapabilityRegistry
from zeta.context.transforms import ContentWorkspace
from zeta.events import DraftEvent
from zeta.harness.dispatch import QueueingDispatcher
from zeta.harness.project import (
    ProjectSnapshot,
    ProjectSnapshotUnavailable,
    agent_from_manifest,
    agent_manifest,
    content_id,
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
from zeta.substrate import InMemoryStore


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
publishes:
  - work.completed
---
Handle the work.
""",
        encoding="utf-8",
    )
    (events / "work.requested.json").write_text(
        json.dumps({"type": "object", "additionalProperties": False}),
        encoding="utf-8",
    )
    (events / "work.completed.json").write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"result": {"type": "string"}},
                "required": ["result"],
                "additionalProperties": False,
            }
        ),
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


def agent_tool_source(
    owner: str,
    name: str,
    *,
    prefix: str = "",
    factory: bool = False,
) -> str:
    export = "def tool():\n    return capability" if factory else "tool = capability"
    return f'''
from zeta.capabilities.executors import InProcessCapabilityExecutor
from zeta.capabilities.registry import RegisteredCapability
from zeta.capabilities.types import Capability, CapabilityId

def run(params):
    return {{"ok": True, "echo": {prefix!r} + params["text"]}}

capability = RegisteredCapability(
    Capability(
        CapabilityId("agent.{owner}", "{name}"),
        "Echo text from an agent-authored tool.",
        {{
            "type": "object",
            "required": ["text"],
            "properties": {{"text": {{"type": "string"}}}},
            "additionalProperties": False,
        }},
    ),
    InProcessCapabilityExecutor(run),
)
{export}
'''


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


def test_project_snapshot_activates_an_agent_content_tool_in_the_next_generation(
    tmp_path: Path,
) -> None:
    agents = write_snapshot_project(tmp_path)
    registry = CapabilityRegistry()
    content = InMemoryStore()
    before = load_project_snapshot(
        agents,
        registry=EventConnectorRegistry(),
        tool_registry=registry,
        model_selection=None,
        content_store=content,
    )
    workspace = ContentWorkspace(
        content,
        run_id="run-tool",
        session_id="session-tool",
        owner="worker",
    )
    transformed = workspace.transform(
        {
            "expected_head": workspace.initialize(),
            "reason": "Reuse the echo operation.",
            "inputs": {},
            "transformation": {
                "type": "literal",
                "value": {
                    "name": "echo",
                    "capability_id": "agent.worker.echo",
                    "source": agent_tool_source("worker", "echo"),
                },
            },
            "destination": {
                "key": "tools/echo",
                "kind": "tool_definition",
                "scope": "agent",
                "expected_object_id": None,
            },
        }
    )

    assert before.tool_registry.resolve("echo") is None
    workspace.promote(transformed.promotions[0])
    after = load_project_snapshot(
        agents,
        registry=EventConnectorRegistry(),
        tool_registry=registry,
        model_selection=None,
        content_store=content,
    )

    assert after.generation_id != before.generation_id
    assert after.project.specs[0].tools == ("agent.worker.echo",)
    assert after.tool_registry.invoke("agent.worker.echo", {"text": "hello"}) == {
        "ok": True,
        "echo": "hello",
    }
    assert after.manifest["agent_tools"][0]["object_id"] == transformed.output_ids[0]

    runtime = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")
    record_project_snapshot(runtime, after)
    restored = load_recorded_project_snapshot(
        runtime,
        after.generation_id,
        registry=EventConnectorRegistry(),
        tool_registry=registry,
    )
    assert restored.tool_registry.invoke("echo", {"text": "again"}) == {
        "ok": True,
        "echo": "again",
    }
    runtime.close()


def test_project_snapshot_compiles_a_file_tool_through_the_agent_tool_path(
    tmp_path: Path,
) -> None:
    agents = write_snapshot_project(tmp_path)
    tools = agents / "tools" / "worker"
    tools.mkdir(parents=True)
    source = agent_tool_source("worker", "echo", prefix="file:")
    (tools / "echo.py").write_text(source, encoding="utf-8")

    snapshot = load_snapshot(agents)

    assert snapshot.project.specs[0].tools == ("agent.worker.echo",)
    assert snapshot.tool_registry.invoke(
        "agent.worker.echo",
        {"text": "hello"},
    ) == {"ok": True, "echo": "file:hello"}
    assert snapshot.manifest["agent_tools"] == [
        {
            "owner": "worker",
            "key": "tools/echo",
            "object_id": snapshot.manifest["agent_tools"][0]["object_id"],
            "name": "echo",
            "capability_id": "agent.worker.echo",
            "source": source,
        }
    ]


def test_project_snapshot_loads_a_file_tool_factory(tmp_path: Path) -> None:
    agents = write_snapshot_project(tmp_path)
    tools = agents / "tools" / "worker"
    tools.mkdir(parents=True)
    (tools / "echo.py").write_text(
        agent_tool_source("worker", "echo", factory=True),
        encoding="utf-8",
    )

    snapshot = load_snapshot(agents)

    assert snapshot.tool_registry.invoke("agent.worker.echo", {"text": "hello"}) == {
        "ok": True,
        "echo": "hello",
    }


def test_project_snapshot_rejects_an_invalid_agent_tool_schema(tmp_path: Path) -> None:
    agents = write_snapshot_project(tmp_path)
    tools = agents / "tools" / "worker"
    tools.mkdir(parents=True)
    source = agent_tool_source("worker", "echo").replace(
        '"type": "object",',
        '"type": "not-a-json-schema-type",',
        1,
    )
    (tools / "echo.py").write_text(source, encoding="utf-8")

    with pytest.raises(ProjectSnapshotUnavailable, match="could not be imported"):
        load_snapshot(agents)


def test_project_snapshot_rejects_duplicate_file_and_content_tools(
    tmp_path: Path,
) -> None:
    agents = write_snapshot_project(tmp_path)
    tools = agents / "tools" / "worker"
    tools.mkdir(parents=True)
    (tools / "echo.py").write_text(
        agent_tool_source("worker", "echo", prefix="file:"),
        encoding="utf-8",
    )
    content = InMemoryStore()
    workspace = ContentWorkspace(
        content,
        run_id="run-tool",
        session_id="session-tool",
        owner="worker",
    )
    transformed = workspace.transform(
        {
            "expected_head": workspace.initialize(),
            "reason": "Create another echo implementation.",
            "inputs": {},
            "transformation": {
                "type": "literal",
                "value": {
                    "name": "echo",
                    "capability_id": "agent.worker.echo",
                    "source": agent_tool_source("worker", "echo", prefix="graph:"),
                },
            },
            "destination": {
                "key": "tools/echo",
                "kind": "tool_definition",
                "scope": "agent",
                "expected_object_id": None,
            },
        }
    )
    workspace.promote(transformed.promotions[0])

    with pytest.raises(ProjectSnapshotUnavailable, match="already registered"):
        load_project_snapshot(
            agents,
            registry=EventConnectorRegistry(),
            tool_registry=CapabilityRegistry(),
            model_selection=None,
            content_store=content,
        )


def test_project_snapshot_keeps_each_agent_tool_generation_stable(
    tmp_path: Path,
) -> None:
    agents = write_snapshot_project(tmp_path)
    registry = CapabilityRegistry()
    content = InMemoryStore()
    workspace = ContentWorkspace(
        content,
        run_id="run-tool",
        session_id="session-tool",
        owner="worker",
    )
    head = workspace.initialize()
    first_change = workspace.transform(
        {
            "expected_head": head,
            "reason": "Create the first echo implementation.",
            "inputs": {},
            "transformation": {
                "type": "literal",
                "value": {
                    "name": "echo",
                    "capability_id": "agent.worker.echo",
                    "source": agent_tool_source("worker", "echo", prefix="one:"),
                },
            },
            "destination": {
                "key": "tools/echo",
                "kind": "tool_definition",
                "scope": "agent",
                "expected_object_id": None,
            },
        }
    )
    workspace.promote(first_change.promotions[0])
    first = load_project_snapshot(
        agents,
        registry=EventConnectorRegistry(),
        tool_registry=registry,
        model_selection=None,
        content_store=content,
    )
    second_change = workspace.transform(
        {
            "expected_head": first_change.head,
            "reason": "Change the echo implementation.",
            "inputs": {},
            "transformation": {
                "type": "literal",
                "value": {
                    "name": "echo",
                    "capability_id": "agent.worker.echo",
                    "source": agent_tool_source("worker", "echo", prefix="two:"),
                },
            },
            "destination": {
                "key": "tools/echo",
                "kind": "tool_definition",
                "scope": "agent",
                "expected_object_id": first_change.output_ids[0],
            },
        }
    )
    workspace.promote(second_change.promotions[0])

    second = load_project_snapshot(
        agents,
        registry=EventConnectorRegistry(),
        tool_registry=registry,
        model_selection=None,
        content_store=content,
    )

    assert first.generation_id != second.generation_id
    assert first.tool_registry.invoke("echo", {"text": "hello"}) == {
        "ok": True,
        "echo": "one:hello",
    }
    assert second.tool_registry.invoke("echo", {"text": "hello"}) == {
        "ok": True,
        "echo": "two:hello",
    }


def test_project_snapshot_round_trips_publishes(tmp_path: Path) -> None:
    snapshot = load_snapshot(write_snapshot_project(tmp_path))
    store = RuntimeEventStore.open(tmp_path / "zeta.sqlite3")

    record_project_snapshot(store, snapshot)
    restored = load_recorded_project_snapshot(
        store,
        snapshot.generation_id,
        registry=EventConnectorRegistry(),
    )

    agent = snapshot.manifest["agents"][0]
    assert snapshot.manifest["version"] == 3
    assert agent["publishes"] == ["work.completed"]
    assert "returns" not in agent
    assert restored.project.specs[0].publishes == ("work.completed",)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("schema", "zeta.other_snapshot", "unsupported project snapshot schema"),
        ("version", 1, "unsupported project snapshot version"),
    ],
)
def test_recorded_project_snapshot_rejects_other_formats(
    tmp_path: Path,
    field: str,
    value: object,
    error: str,
) -> None:
    current = load_snapshot(write_snapshot_project(tmp_path))
    manifest = {**current.manifest, field: value}
    generation_id = content_id("project", manifest)
    legacy = ProjectSnapshot(generation_id, current.project, manifest)
    store = RuntimeEventStore.open(tmp_path / "zeta.sqlite3")
    record_project_snapshot(store, legacy)

    with pytest.raises(ProjectSnapshotUnavailable, match=error):
        load_recorded_project_snapshot(
            store,
            generation_id,
            registry=EventConnectorRegistry(),
        )


def test_execution_manifest_contains_publishes_and_relevant_schemas(
    tmp_path: Path,
) -> None:
    snapshot = load_snapshot(write_snapshot_project(tmp_path))

    execution_manifest = snapshot.execution_manifest(snapshot.project.specs[0])

    assert execution_manifest["version"] == 2
    assert execution_manifest["agent"]["publishes"] == ["work.completed"]
    assert "returns" not in execution_manifest["agent"]
    assert set(execution_manifest["events"]) == {"work.requested", "work.completed"}
    assert execution_manifest["events"]["work.requested"] == {
        "type": "object",
        "additionalProperties": False,
    }
    assert execution_manifest["events"]["work.completed"] == {
        "type": "object",
        "properties": {"result": {"type": "string"}},
        "required": ["result"],
        "additionalProperties": False,
    }


def test_execution_manifest_preserves_returns_and_relevant_schemas(
    tmp_path: Path,
) -> None:
    snapshot = load_snapshot(write_snapshot_project(tmp_path))
    spec = replace(snapshot.project.specs[0], returns=("work.completed",))

    execution_manifest = snapshot.execution_manifest(spec)
    restored = agent_from_manifest(agent_manifest(spec))

    assert execution_manifest["agent"]["returns"] == ["work.completed"]
    assert set(execution_manifest["events"]) == {"work.requested", "work.completed"}
    assert restored.returns == ("work.completed",)


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
        event_registry=snapshot.project.events,
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
    assert queue_item["session_id"] == f"agent/{spec.slug}/{outcome.event.id}"
    assert attempt["session_id"] == queue_item["session_id"]
    assert attempt["project_generation"] == snapshot.generation_id
    assert attempt["execution_manifest_id"] == execution_manifest["id"]
    assert attempt["execution_manifest"] == execution_manifest
    assert started.payload["execution_manifest"] == execution_manifest
    assert store.session_status(queue_item["session_id"]) == {
        "session_id": queue_item["session_id"],
        "agent_id": spec.slug,
        "status": "idle",
        "active_run_id": None,
        "queued_turns": 0,
        "active_wait": None,
        "latest_run": {
            "run_id": attempt["run_id"],
            "status": "completed",
        },
        "updated_at": store.list_sessions()[0]["updated_at"],
    }


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
