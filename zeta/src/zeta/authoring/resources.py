"""Authored-agent resource loading hooks."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from importlib import metadata as importlib_metadata
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any

from connectors import (
    ConnectorManifest,
    ConnectorManifestError,
    EventConnectorRegistry,
    connector_manifest_from_describe,
)

from zeta.authoring.manifest import Manifest
from zeta.authoring.schemas import EventRegistry, EventRegistryError
from zeta.authoring.spec import (
    MASTER_AGENT_ID,
    SESSION_MESSAGE_REQUESTED,
    AgentSpec,
    load_spec,
    load_specs,
    scheduled_event_type,
)
from zeta.capabilities.executors import ToolExecutorProviderRegistry
from zeta.substrate import Object

logger = logging.getLogger(__name__)


class ResourceError(ValueError):
    """Raised when a flat authored-agent resource is invalid."""


EVENT_CONNECTOR_ENTRY_POINT_GROUP = "zeta.event_connectors"


@dataclass(frozen=True)
class SkillResource:
    name: str
    path: Path
    body: str
    object_id: str


@dataclass(frozen=True)
class SkillRegistry:
    skills: dict[str, SkillResource] = field(default_factory=dict)

    def knows(self, name: str) -> bool:
        return name in self.skills


@dataclass(frozen=True)
class AgentProject:
    specs: tuple[AgentSpec, ...]
    events: EventRegistry
    skills: SkillRegistry
    connectors: EventConnectorRegistry = field(default_factory=EventConnectorRegistry)


def resource_extensions(spec: AgentSpec) -> dict[str, object]:
    """Return non-core frontmatter extensions for resource-aware hosts."""
    return dict(spec.manifest)


def load_agent_project(
    agents_dir: Path,
    *,
    registry: EventConnectorRegistry | None = None,
    connector_names: Iterable[str] | None = None,
) -> AgentProject:
    """Load flat authored agents and their shared validation resources."""
    specs = (*load_specs(agents_dir), load_packaged_master_spec())
    connectors = registry or load_connector_registry(
        connector_names=connector_names,
    )
    events = load_event_registry(
        agents_dir,
        connectors=connectors.event_connectors(),
    )
    register_session_message_event(events)
    register_scheduled_events(events, specs)
    return AgentProject(
        specs=specs,
        events=events,
        skills=load_skill_registry(agents_dir),
        connectors=connectors,
    )


def load_packaged_master_spec() -> AgentSpec:
    """Load Zeta's default entry point through the authored-agent parser."""
    with as_file(files("zeta").joinpath("master.md")) as path:
        return replace(load_spec(path), slug=MASTER_AGENT_ID)


def register_session_message_event(events: EventRegistry) -> None:
    register_event_schema(
        events,
        SESSION_MESSAGE_REQUESTED,
        {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "agent_id": {"type": "string"},
                "session_id": {"type": "string"},
                "run_id": {"type": "string"},
            },
            "required": ["message", "agent_id", "session_id", "run_id"],
            "additionalProperties": False,
        },
        source="Zeta packaged session event",
    )


def validate_agent_project(
    project: AgentProject,
    *,
    tool_executors: ToolExecutorProviderRegistry | None = None,
) -> None:
    manifest = Manifest(
        events=project.events,
        skills=project.skills,
        connectors=project.connectors,
        tool_executors=tool_executors,
    )
    for spec in project.specs:
        manifest.validate(spec)


def register_scheduled_events(
    events: EventRegistry,
    specs: tuple[AgentSpec, ...],
) -> None:
    for spec in specs:
        if not spec.schedules:
            continue
        event_type = scheduled_event_type(spec.slug)
        if events.knows(event_type):
            continue
        events.register(event_type, empty_payload_schema())


def empty_payload_schema() -> dict[str, object]:
    return {"type": "object", "additionalProperties": False}


def load_connector_registry(
    *,
    connector_names: Iterable[str] | None = None,
    entry_points: Iterable[Any] | None = None,
) -> EventConnectorRegistry:
    """Register installed Python connectors without importing their code."""
    allowed = set(connector_names) if connector_names is not None else None
    selected = [
        entry_point
        for entry_point in event_connector_entry_points(entry_points)
        if allowed is None or entry_point.name in allowed
    ]
    names: set[str] = set()
    for entry_point in selected:
        if entry_point.name in names:
            raise ResourceError(f"duplicate connector entry point {entry_point.name!r}")
        names.add(entry_point.name)

    registry = EventConnectorRegistry()
    for entry_point in selected:
        command = connector_entry_point_command(entry_point)
        try:
            manifest = describe_connector(command, expected_id=entry_point.name)
        except Exception as exc:
            logger.warning("skipping event connector %r: %s", entry_point.name, exc)
            continue
        register_event_connector(registry, manifest)
    return registry


DESCRIBE_TIMEOUT_SECONDS = 10.0


def event_connector_entry_points(
    entry_points: Iterable[Any] | None = None,
) -> tuple[Any, ...]:
    """Return connector metadata in deterministic launch order."""
    discovered = (
        importlib_metadata.entry_points(group=EVENT_CONNECTOR_ENTRY_POINT_GROUP)
        if entry_points is None
        else entry_points
    )
    return tuple(
        sorted(
            (
                entry_point
                for entry_point in discovered
                if entry_point.group == EVENT_CONNECTOR_ENTRY_POINT_GROUP
            ),
            key=lambda entry_point: (entry_point.name, entry_point.value),
        )
    )


def connector_entry_point_command(entry_point: Any) -> tuple[str, ...]:
    """Turn Python package metadata into an isolated child launch command."""
    if not isinstance(entry_point.name, str) or not entry_point.name:
        raise ResourceError("connector entry point must have a non-empty name")
    if not isinstance(entry_point.value, str) or not entry_point.value:
        raise ResourceError(
            f"connector entry point {entry_point.name!r} must have a target"
        )
    return (
        sys.executable,
        "-m",
        "zeta.ipc.client",
        EVENT_CONNECTOR_ENTRY_POINT_GROUP,
        entry_point.name,
        entry_point.value,
    )


def describe_connector(
    command: tuple[str, ...],
    *,
    expected_id: str | None = None,
) -> ConnectorManifest:
    """Read one connector manifest from an isolated child process."""
    completed = subprocess.run(
        [*command, "--describe"],
        capture_output=True,
        timeout=DESCRIBE_TIMEOUT_SECONDS,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()
        raise ResourceError(
            f"connector {command[0]} --describe failed"
            + (f": {detail[-1]}" if detail else "")
        )
    try:
        raw = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ResourceError(
            f"connector {command[0]} --describe printed invalid JSON: {exc}"
        ) from exc
    try:
        manifest = connector_manifest_from_describe(
            raw, command=command, expected_id=expected_id
        )
    except ConnectorManifestError as exc:
        raise ResourceError(f"connector {command[0]}: {exc}") from exc
    return manifest


def register_event_connector(
    registry: EventConnectorRegistry, connector: ConnectorManifest
) -> None:
    try:
        registry.register(connector)
    except ValueError as exc:
        raise ResourceError(str(exc)) from exc


def load_skill_registry(agents_dir: Path) -> SkillRegistry:
    """Load flat Markdown skills from ``agents/skills``."""
    skills_dir = agents_dir / "skills"
    if not skills_dir.exists():
        return SkillRegistry()
    skills: dict[str, SkillResource] = {}
    for path in sorted(skills_dir.iterdir()):
        if path.suffix != ".md" or not path.is_file() or path.is_symlink():
            continue
        name = path.stem
        if name in skills:
            raise ResourceError(f"duplicate skill {name!r}")
        try:
            body = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ResourceError(f"I/O error reading {path}: {exc}") from exc
        skill_object = Object(
            kind="skill",
            schema="zeta.skill.v1",
            data={"body": body},
        )
        skills[name] = SkillResource(name, path, body, skill_object.content_address())
    return SkillRegistry(skills)


def load_event_registry(
    agents_dir: Path,
    *,
    connectors: Iterable[ConnectorManifest] = (),
) -> EventRegistry:
    """Load flat event payload JSON Schemas from ``agents/events``."""
    events_dir = agents_dir / "events"
    registry = EventRegistry()
    for connector in connectors:
        for event_type, schema in connector.events.items():
            register_event_schema(
                registry,
                event_type,
                schema,
                source=f"connector {connector.id!r}",
            )
    if not events_dir.exists():
        return registry
    for path in sorted(events_dir.iterdir()):
        if path.suffix != ".json":
            continue
        if not path.is_file() or path.is_symlink():
            continue
        event_type = path.stem
        schema = load_event_schema(path)
        register_event_schema(registry, event_type, schema, source=str(path))
    return registry


def register_event_schema(
    registry: EventRegistry,
    event_type: str,
    schema: Mapping[str, Any] | None,
    *,
    source: str,
) -> None:
    if registry.knows(event_type):
        if registry.schema(event_type) == (
            dict(schema) if schema is not None else None
        ):
            return
        raise ResourceError(f"event resource {source} conflicts for {event_type!r}")
    try:
        registry.register(event_type, schema)
    except EventRegistryError as exc:
        raise ResourceError(f"invalid event resource {source}: {exc}") from exc


def load_event_schema(path: Path) -> Mapping[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ResourceError(f"invalid JSON in {path}: {exc}") from exc
    except OSError as exc:
        raise ResourceError(f"I/O error reading {path}: {exc}") from exc
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ResourceError(f"invalid event resource {path}: expected object")
    schema = raw.get("schema")
    if schema is None:
        return raw
    if not isinstance(schema, Mapping):
        raise ResourceError(f"invalid event resource {path}: schema must be an object")
    return schema
