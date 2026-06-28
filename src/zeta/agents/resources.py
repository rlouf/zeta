"""Authored-agent resource loading hooks."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, cast

import yaml

from connectors import EventConnector, EventConnectorResolver
from zeta.agents.events import EventRegistry, EventRegistryError
from zeta.agents.manifest import Manifest
from zeta.agents.spec import AgentSpec, load_specs, scheduled_event_type


class ResourceError(ValueError):
    """Raised when a flat authored-agent resource is invalid."""


EVENT_CONNECTOR_ENTRY_POINT_GROUP = "zeta.event_connectors"
EVENT_CONNECTOR_CONFIG_FILE = "connectors.yaml"


@dataclass(frozen=True)
class SkillResource:
    name: str
    path: Path
    body: str


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
    connectors: dict[str, EventConnector] = field(default_factory=dict)


@dataclass(frozen=True)
class EntryPointEventConnectorResolver:
    enabled: tuple[str, ...]
    entry_points: Iterable[Any] | None = None

    def resolve(self, connector_id: str) -> EventConnector | None:
        return self._connectors().get(connector_id)

    def names_for_section(self, key: str, value: Any) -> tuple[str, ...]:
        if key not in {"ingress", "egress"} or not isinstance(value, list | tuple):
            return ()
        event_names = binding_event_names(value)
        return tuple(
            connector_id
            for connector_id, connector in self._connectors().items()
            if any(event_type in connector.events for event_type in event_names)
        )

    def _connectors(self) -> dict[str, EventConnector]:
        connectors: dict[str, EventConnector] = {}
        entry_points = event_connector_entry_points(self.entry_points)
        for entry_point in entry_points:
            if entry_point.name not in self.enabled:
                continue
            connector = load_entry_point_event_connector(entry_point)
            if connector.id != entry_point.name:
                raise ResourceError(
                    f"event connector entry point {entry_point.name!r} returned "
                    f"connector id {connector.id!r}"
                )
            connectors[connector.id] = connector
        return connectors


def resource_extensions(spec: AgentSpec) -> dict[str, object]:
    """Return non-core frontmatter extensions for resource-aware hosts."""
    return dict(spec.manifest)


def load_agent_project(
    agents_dir: Path,
    *,
    connector_resolver: EventConnectorResolver | None = None,
) -> AgentProject:
    """Load flat authored agents and their shared validation resources."""
    specs = load_specs(agents_dir)
    connector_resolver = connector_resolver or event_connector_resolver_from_project(
        agents_dir
    )
    connectors = resolve_event_connectors(specs, connector_resolver)
    events = load_event_registry(agents_dir, connectors=connectors.values())
    register_scheduled_events(events, specs)
    return AgentProject(
        specs=specs,
        events=events,
        skills=load_skill_registry(agents_dir),
        connectors=connectors,
    )


def validate_agent_project(project: AgentProject) -> None:
    manifest = Manifest(
        events=project.events,
        skills=project.skills,
        connectors=project.connectors,
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


def event_connector_resolver_from_project(
    agents_dir: Path,
    *,
    entry_points: Iterable[Any] | None = None,
) -> EventConnectorResolver | None:
    enabled = enabled_event_connector_ids(agents_dir)
    if not enabled:
        return None
    return EntryPointEventConnectorResolver(
        enabled=enabled,
        entry_points=entry_points,
    )


def enabled_event_connector_ids(agents_dir: Path) -> tuple[str, ...]:
    path = agents_dir / EVENT_CONNECTOR_CONFIG_FILE
    if not path.exists():
        return ()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ResourceError(f"invalid event connector config {path}: {exc}") from exc
    except OSError as exc:
        raise ResourceError(f"I/O error reading {path}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ResourceError(f"invalid event connector config {path}: expected object")
    unknown = sorted(set(raw) - {"event_connectors"})
    if unknown:
        raise ResourceError(
            f"invalid event connector config {path}: unsupported field {unknown[0]!r}"
        )
    connectors = raw.get("event_connectors")
    if connectors is None:
        raise ResourceError(
            f"invalid event connector config {path}: event_connectors is required"
        )
    if not isinstance(connectors, list | tuple) or not all(
        isinstance(connector, str) and connector for connector in connectors
    ):
        raise ResourceError(
            f"invalid event connector config {path}: event_connectors must be a list of strings"
        )
    return tuple(connectors)


def event_connector_entry_points(
    entry_points: Iterable[Any] | None = None,
) -> tuple[Any, ...]:
    discovered = (
        importlib_metadata.entry_points() if entry_points is None else entry_points
    )
    select = getattr(discovered, "select", None)
    if callable(select):
        return tuple(select(group=EVENT_CONNECTOR_ENTRY_POINT_GROUP))
    if isinstance(discovered, Mapping):
        grouped = cast(Mapping[str, Iterable[Any]], discovered)
        return tuple(grouped.get(EVENT_CONNECTOR_ENTRY_POINT_GROUP, ()))
    return tuple(
        entry_point
        for entry_point in discovered
        if getattr(entry_point, "group", None) == EVENT_CONNECTOR_ENTRY_POINT_GROUP
    )


def load_entry_point_event_connector(entry_point: Any) -> EventConnector:
    loaded = entry_point.load()
    connector = loaded() if callable(loaded) else loaded
    if not isinstance(connector, EventConnector):
        raise ResourceError(
            f"event connector entry point {entry_point.name!r} did not return EventConnector"
        )
    return connector


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
        skills[name] = SkillResource(name, path, body)
    return SkillRegistry(skills)


def resolve_event_connectors(
    specs: tuple[AgentSpec, ...],
    connector_resolver: EventConnectorResolver | None,
) -> dict[str, EventConnector]:
    if connector_resolver is None:
        return {}
    names = sorted(connector_names_for_specs(specs, connector_resolver))
    connectors: dict[str, EventConnector] = {}
    for name in names:
        connector = connector_resolver.resolve(name)
        if connector is not None:
            connectors[name] = connector
    return connectors


def connector_names_for_specs(
    specs: tuple[AgentSpec, ...],
    connector_resolver: EventConnectorResolver,
) -> set[str]:
    names: set[str] = set()
    for spec in specs:
        for key, value in spec.manifest.items():
            names.update(connector_names_for_section(connector_resolver, key, value))
    return names


def connector_names_for_section(
    connector_resolver: EventConnectorResolver,
    key: str,
    value: object,
) -> set[str]:
    try:
        return set(connector_resolver.names_for_section(key, value))
    except AttributeError:
        return connector_names_from_builtin_sections(key, value)


def connector_names_from_builtin_sections(key: str, value: object) -> set[str]:
    if not isinstance(value, list | tuple):
        return set()
    if key not in {"ingress", "egress"}:
        return set()
    return binding_event_names(value)


def binding_event_names(items: Iterable[object]) -> set[str]:
    events: set[str] = set()
    for item in items:
        if isinstance(item, Mapping):
            mapping = cast(Mapping[str, object], item)
            event = mapping.get("event")
            if isinstance(event, str):
                events.add(event)
    return events


def load_event_registry(
    agents_dir: Path,
    *,
    connectors: Iterable[EventConnector] = (),
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
