"""Immutable compiled-project generation manifests."""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from connectors import EventConnector, EventConnectorRegistry

from zeta._version import __version__
from zeta.authoring.resources import (
    AgentProject,
    SkillRegistry,
    SkillResource,
    load_agent_project,
    validate_agent_project,
)
from zeta.authoring.schemas import EventRegistry
from zeta.authoring.spec import (
    AgentSpec,
    ExecutorSpec,
    ModelSpec,
    RetrySpec,
    ScheduleEntry,
    executor_config,
)
from zeta.capabilities.executors import (
    ToolExecutorProviderRegistry,
    load_tool_executor_provider_registry,
)
from zeta.capabilities.registry import CapabilityRegistry
from zeta.events import DraftEvent, Event
from zeta.harness.store import RuntimeEventStore
from zeta.journal.store import Filter
from zeta.models.profiles import ModelSelection

PROJECT_SNAPSHOT_RECORDED = "runtime.project_snapshot.recorded"
PROJECT_SNAPSHOT_SCHEMA = "zeta.project_snapshot"
EXECUTION_MANIFEST_SCHEMA = "zeta.execution_manifest"


class ProjectSnapshotUnavailable(RuntimeError):
    """Raised when a recorded project generation cannot be executed safely."""


@dataclass(frozen=True)
class ProjectSnapshot:
    """One validated authored project and its content-addressed manifest."""

    generation_id: str
    project: AgentProject
    manifest: dict[str, Any]

    def execution_manifest(self, spec: AgentSpec) -> dict[str, Any]:
        selected_skills = {
            name: self.manifest["skills"][name]
            for name in spec.skills
            if name in self.manifest["skills"]
        }
        selected_capabilities = {
            name: value
            for name, value in self.manifest["capabilities"].items()
            if name in spec.tools or value["name"] in spec.tools
        }
        relevant_events = {
            event_type: self.manifest["events"].get(event_type)
            for event_type in (*spec.accepts, *spec.returns)
        }
        manifest = {
            "schema": EXECUTION_MANIFEST_SCHEMA,
            "version": 1,
            "project_generation": self.generation_id,
            "agent": agent_manifest(spec),
            "events": relevant_events,
            "skills": selected_skills,
            "capabilities": selected_capabilities,
            "connectors": self.manifest["connectors"],
            "model": self.manifest["model"],
            "runtime_version": self.manifest["runtime_version"],
        }
        return {**manifest, "id": content_id("execution_manifest", manifest)}


def load_project_snapshot(
    agents_dir: Path,
    *,
    registry,
    tool_registry: CapabilityRegistry,
    model_selection: ModelSelection | None,
    tool_executors: ToolExecutorProviderRegistry | None = None,
) -> ProjectSnapshot:
    project = load_agent_project(agents_dir, registry=registry)
    validate_agent_project(
        project,
        tool_executors=tool_executors or load_tool_executor_provider_registry(),
    )
    manifest = project_manifest(
        project,
        tool_registry=tool_registry,
        model_selection=model_selection,
    )
    return ProjectSnapshot(
        generation_id=content_id("project", manifest),
        project=project,
        manifest=manifest,
    )


def record_project_snapshot(
    events: RuntimeEventStore,
    snapshot: ProjectSnapshot,
) -> Event:
    return events.accept(
        DraftEvent(
            PROJECT_SNAPSHOT_RECORDED,
            "zeta",
            {
                "generation_id": snapshot.generation_id,
                "manifest": snapshot.manifest,
            },
            idempotency_key=f"project_snapshot:{snapshot.generation_id}",
        )
    ).event


def load_recorded_project_snapshot(
    events: RuntimeEventStore,
    generation_id: str,
    *,
    registry: EventConnectorRegistry | None,
    tool_executors: ToolExecutorProviderRegistry | None = None,
) -> ProjectSnapshot:
    for event in events.list_events(Filter(event_type=PROJECT_SNAPSHOT_RECORDED)):
        if event.payload.get("generation_id") != generation_id:
            continue
        manifest = event.payload.get("manifest")
        if not isinstance(manifest, Mapping):
            break
        parsed_manifest = dict(manifest)
        if content_id("project", parsed_manifest) != generation_id:
            raise ProjectSnapshotUnavailable(
                f"recorded project snapshot {generation_id!r} failed verification"
            )
        project = project_from_manifest(parsed_manifest, registry=registry)
        validate_agent_project(
            project,
            tool_executors=tool_executors or load_tool_executor_provider_registry(),
        )
        return ProjectSnapshot(generation_id, project, parsed_manifest)
    raise ProjectSnapshotUnavailable(
        f"recorded project snapshot {generation_id!r} was not found"
    )


def project_manifest(
    project: AgentProject,
    *,
    tool_registry: CapabilityRegistry,
    model_selection: ModelSelection | None,
) -> dict[str, Any]:
    return {
        "schema": PROJECT_SNAPSHOT_SCHEMA,
        "version": 1,
        "agents": [agent_manifest(spec) for spec in project.specs],
        "events": {event_type: schema for event_type, schema in project.events.items()},
        "skills": {
            name: {
                "sha256": skill.sha256,
                "body": skill.body,
                "path": str(skill.path),
            }
            for name, skill in sorted(project.skills.skills.items())
        },
        "connectors": [
            connector_manifest(connector)
            for connector in sorted(
                project.connectors.event_connectors(), key=lambda item: item.id
            )
        ],
        "capabilities": capability_manifests(tool_registry),
        "model": model_manifest(model_selection),
        "runtime_version": __version__,
    }


def project_from_manifest(
    manifest: Mapping[str, Any],
    *,
    registry: EventConnectorRegistry | None,
) -> AgentProject:
    connectors = registry or EventConnectorRegistry()
    recorded_connectors = manifest.get("connectors")
    current_connectors = [
        connector_manifest(connector)
        for connector in sorted(connectors.event_connectors(), key=lambda item: item.id)
    ]
    if recorded_connectors != current_connectors:
        raise ProjectSnapshotUnavailable(
            "recorded project snapshot connector code is not available"
        )
    raw_events = manifest.get("events")
    if not isinstance(raw_events, Mapping):
        raise ProjectSnapshotUnavailable("project snapshot has invalid events")
    events = EventRegistry(
        {
            str(event_type): schema if isinstance(schema, Mapping) else None
            for event_type, schema in raw_events.items()
        }
    )
    raw_skills = manifest.get("skills")
    if not isinstance(raw_skills, Mapping):
        raise ProjectSnapshotUnavailable("project snapshot has invalid skills")
    skills = SkillRegistry(
        {
            str(name): skill_from_manifest(str(name), value)
            for name, value in raw_skills.items()
        }
    )
    raw_agents = manifest.get("agents")
    if not isinstance(raw_agents, list):
        raise ProjectSnapshotUnavailable("project snapshot has invalid agents")
    return AgentProject(
        specs=tuple(agent_from_manifest(value) for value in raw_agents),
        events=events,
        skills=skills,
        connectors=connectors,
    )


def agent_from_manifest(value: Any) -> AgentSpec:
    if not isinstance(value, Mapping):
        raise ProjectSnapshotUnavailable("project snapshot has invalid agent")
    raw_model = value.get("model")
    model = (
        ModelSpec(name=str(raw_model["name"]), url=str(raw_model["url"]))
        if isinstance(raw_model, Mapping)
        else None
    )
    raw_retry = value.get("retry")
    retry = (
        RetrySpec(
            max_attempts=_optional_int(raw_retry.get("max_attempts")),
            backoff_seconds=_optional_float(raw_retry.get("backoff_seconds")),
        )
        if isinstance(raw_retry, Mapping)
        else None
    )
    raw_executor = value.get("executor")
    if not isinstance(raw_executor, Mapping):
        raise ProjectSnapshotUnavailable("project snapshot has invalid tool executor")
    provider = raw_executor.get("provider")
    config = raw_executor.get("config")
    if (
        not isinstance(provider, str)
        or provider == ""
        or not isinstance(config, Mapping)
    ):
        raise ProjectSnapshotUnavailable("project snapshot has invalid tool executor")
    try:
        normalized_config = executor_config(config)
    except ValueError as exc:
        raise ProjectSnapshotUnavailable(
            "project snapshot has invalid tool executor config"
        ) from exc
    raw_schedules = value.get("schedules")
    schedules = (
        tuple(
            ScheduleEntry(
                cron=str(schedule["cron"]),
                timezone=_optional_str(schedule.get("timezone")),
                catchup=_optional_str(schedule.get("catchup")),
            )
            for schedule in raw_schedules
            if isinstance(schedule, Mapping)
        )
        if isinstance(raw_schedules, list)
        else ()
    )
    raw_manifest = value.get("manifest")
    return AgentSpec(
        slug=str(value["slug"]),
        name=str(value["name"]),
        description=str(value["description"]),
        instructions=str(value["instructions"]),
        path=Path(str(value["path"])),
        sha256=str(value["sha256"]),
        enabled=bool(value.get("enabled", True)),
        session=str(value.get("session", "per-event")),
        model=model,
        executor=ExecutorSpec(provider=provider, config=normalized_config),
        accepts=_string_tuple(value.get("accepts")),
        returns=_string_tuple(value.get("returns")),
        skills=_string_tuple(value.get("skills")),
        tools=_string_tuple(value.get("tools")),
        schedules=schedules,
        retry=retry,
        base_dir=(
            Path(str(value["base_dir"])) if value.get("base_dir") is not None else None
        ),
        manifest=dict(raw_manifest) if isinstance(raw_manifest, Mapping) else {},
    )


def skill_from_manifest(name: str, value: Any) -> SkillResource:
    if not isinstance(value, Mapping):
        raise ProjectSnapshotUnavailable("project snapshot has invalid skill")
    body = str(value["body"])
    sha256 = str(value["sha256"])
    if hashlib.sha256(body.encode()).hexdigest() != sha256:
        raise ProjectSnapshotUnavailable(f"recorded skill {name!r} failed verification")
    return SkillResource(name, Path(str(value["path"])), body, sha256)


def agent_manifest(spec: AgentSpec) -> dict[str, Any]:
    return {
        "slug": spec.slug,
        "name": spec.name,
        "description": spec.description,
        "instructions": spec.instructions,
        "path": str(spec.path),
        "sha256": spec.sha256,
        "enabled": spec.enabled,
        "session": spec.session,
        "model": (
            {"name": spec.model.name, "url": spec.model.url}
            if spec.model is not None
            else None
        ),
        "executor": {
            "provider": spec.executor.provider,
            "config": executor_config(spec.executor.config),
        },
        "accepts": list(spec.accepts),
        "returns": list(spec.returns),
        "skills": list(spec.skills),
        "tools": list(spec.tools),
        "schedules": [
            {
                "cron": schedule.cron,
                "timezone": schedule.timezone,
                "catchup": schedule.catchup,
            }
            for schedule in spec.schedules
        ],
        "retry": (
            {
                "max_attempts": spec.retry.max_attempts,
                "backoff_seconds": spec.retry.backoff_seconds,
            }
            if spec.retry is not None
            else None
        ),
        "base_dir": str(spec.base_dir) if spec.base_dir is not None else None,
        "manifest": spec.manifest,
    }


def connector_manifest(connector: EventConnector) -> dict[str, Any]:
    handlers = [
        *connector.ingress.values(),
        *connector.egress.values(),
        *((connector.push_ingress,) if connector.push_ingress is not None else ()),
    ]
    return {
        "id": connector.id,
        "source": [callable_manifest(handler) for handler in handlers],
        "events": {name: schema for name, schema in sorted(connector.events.items())},
        "ingress": sorted(connector.ingress),
        "egress": sorted(connector.egress),
        "egress_semantics": dict(sorted(connector.egress_semantics.items())),
        "filters": {name: schema for name, schema in sorted(connector.filters.items())},
    }


def callable_manifest(handler: Any) -> dict[str, Any]:
    target = (
        handler
        if inspect.isfunction(handler) or inspect.ismethod(handler)
        else type(handler)
    )
    module = inspect.getmodule(target)
    try:
        path = inspect.getsourcefile(target)
    except TypeError:
        path = None
    return {
        "module": module.__name__ if module is not None else None,
        "qualname": getattr(target, "__qualname__", type(handler).__qualname__),
        "source_sha256": file_sha256(Path(path)) if path is not None else None,
    }


def capability_manifests(registry: CapabilityRegistry) -> dict[str, Any]:
    manifests: dict[str, Any] = {}
    for capability_id in registry.list_capability_ids():
        registered = registry.get(capability_id)
        if registered is None:
            continue
        declaration = registered.declaration
        manifests[capability_id] = {
            "provider": declaration.id.provider,
            "name": declaration.id.name,
            "description": declaration.description,
            "input_schema": declaration.input_schema,
            "delivery_semantics": declaration.delivery_semantics,
            "executor": callable_manifest(registered.executor),
        }
    return manifests


def model_manifest(selection: ModelSelection | None) -> dict[str, Any] | None:
    if selection is None:
        return None
    return {
        "profile": selection.profile,
        "model": selection.model,
        "url": selection.url,
        "thinking": selection.thinking,
        "api": selection.api,
    }


def content_id(prefix: str, value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return f"{prefix}:sha256:{hashlib.sha256(encoded).hexdigest()}"


def file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value)


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_float(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) else None
