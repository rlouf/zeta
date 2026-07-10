"""Immutable compiled-project generation manifests."""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from connectors import EventConnector
from zeta._version import __version__
from zeta.agents.resources import (
    AgentProject,
    load_agent_project,
    validate_agent_project,
)
from zeta.agents.spec import AgentSpec
from zeta.capabilities.registry import CapabilityRegistry
from zeta.models.profiles import ModelSelection
from zeta.records.events import DraftEvent, Event

from zetad.store import RuntimeEventStore

PROJECT_SNAPSHOT_RECORDED = "runtime.project_snapshot.recorded"
PROJECT_SNAPSHOT_SCHEMA = "zeta.project_snapshot"
EXECUTION_MANIFEST_SCHEMA = "zeta.execution_manifest"


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
) -> ProjectSnapshot:
    project = load_agent_project(agents_dir, registry=registry)
    validate_agent_project(project)
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
        "events": {
            event_type: schema for event_type, schema in project.events.items()
        },
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


def agent_manifest(spec: AgentSpec) -> dict[str, Any]:
    return {
        "slug": spec.slug,
        "name": spec.name,
        "description": spec.description,
        "instructions": spec.instructions,
        "path": str(spec.path),
        "sha256": spec.sha256,
        "enabled": spec.enabled,
        "resumable": spec.resumable,
        "model": (
            {"name": spec.model.name, "url": spec.model.url}
            if spec.model is not None
            else None
        ),
        "accepts": list(spec.accepts),
        "returns": list(spec.returns),
        "skills": list(spec.skills),
        "tools": list(spec.tools),
        "schedules": [
            {"cron": schedule.cron, "timezone": schedule.timezone}
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
