"""Authored-agent resource loading hooks."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
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
        agents_dir,
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
    agents_dir: Path,
    *,
    connector_names: Iterable[str] | None = None,
    executables: Iterable[tuple[str, tuple[str, ...]]] | None = None,
) -> EventConnectorRegistry:
    """Register every connector executable the shell can see.

    Discovery is the shell's (spec/wire-v0.md §13): `zeta-connector-<id>`
    on PATH, plus executable files under ``agents/connectors/`` — a
    project-local executable overrides a PATH connector with the same
    id. `connector_names` is the process-level allowlist
    (``zeta serve --connectors``); `executables` injects commands
    directly for tests.
    """
    allowed = set(connector_names) if connector_names is not None else None
    registry = EventConnectorRegistry()
    for connector_id, command in discover_connector_commands(
        agents_dir, executables=executables
    ):
        if allowed is not None and connector_id not in allowed:
            continue
        try:
            manifest = describe_connector(command, expected_id=connector_id)
        except Exception as exc:
            # A connector that cannot describe itself must not poison
            # unrelated projects; the warning names the casualty.
            logger.warning("skipping event connector %r: %s", connector_id, exc)
            continue
        register_event_connector(registry, manifest)
    return registry


CONNECTOR_COMMAND_PREFIX = "zeta-connector-"
DESCRIBE_TIMEOUT_SECONDS = 10.0

_describe_cache: dict[tuple[tuple[str, ...], float], ConnectorManifest] = {}


def discover_connector_commands(
    agents_dir: Path,
    *,
    executables: Iterable[tuple[str, tuple[str, ...]]] | None = None,
) -> list[tuple[str, tuple[str, ...]]]:
    if executables is not None:
        return list(executables)
    commands: dict[str, tuple[str, ...]] = {}
    # The running interpreter's script directory comes first: connectors
    # installed beside zeta-os (one venv, one uv tool) must be found even
    # when `zeta` was invoked by absolute path with a bare PATH.
    for directory in (str(Path(sys.executable).parent), *os.get_exec_path()):
        try:
            entries = sorted(Path(directory).iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.name.startswith(CONNECTOR_COMMAND_PREFIX):
                continue
            connector_id = entry.name.removeprefix(CONNECTOR_COMMAND_PREFIX)
            if connector_id in commands:
                continue  # first PATH hit wins, like the shell
            if entry.is_file() and os.access(entry, os.X_OK):
                commands[connector_id] = (str(entry),)
    connectors_dir = agents_dir / "connectors"
    if connectors_dir.is_dir():
        for entry in sorted(connectors_dir.iterdir()):
            if not entry.is_file() or not os.access(entry, os.X_OK):
                continue
            commands[entry.stem] = (str(entry),)
    return sorted(commands.items())


def describe_connector(
    command: tuple[str, ...],
    *,
    expected_id: str | None = None,
) -> ConnectorManifest:
    """Run one `--describe` invocation, cached by executable mtime."""
    try:
        mtime = Path(command[0]).stat().st_mtime
    except OSError:
        mtime = 0.0
    cache_key = (command, mtime)
    cached = _describe_cache.get(cache_key)
    if cached is not None:
        return cached
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
    _describe_cache[cache_key] = manifest
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
