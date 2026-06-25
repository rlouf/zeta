"""Authored-agent resource loading hooks."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zeta.agents.events import EventRegistry, EventRegistryError
from zeta.agents.manifest import AgentPlugin, Manifest, PluginResolver
from zeta.agents.spec import AgentSpec, load_specs, scheduled_event_type


class ResourceError(ValueError):
    """Raised when a flat authored-agent resource is invalid."""


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
    plugins: dict[str, AgentPlugin] = field(default_factory=dict)


def resource_extensions(spec: AgentSpec) -> dict[str, object]:
    """Return non-core frontmatter extensions for resource-aware hosts."""
    return dict(spec.extensions or {})


def load_agent_project(
    agents_dir: Path,
    *,
    plugin_resolver: PluginResolver | None = None,
) -> AgentProject:
    """Load flat authored agents and their shared validation resources."""
    specs = load_specs(agents_dir)
    plugins = resolve_agent_plugins(specs, plugin_resolver)
    events = load_event_registry(agents_dir, plugins=plugins.values())
    register_scheduled_events(events, specs)
    return AgentProject(
        specs=specs,
        events=events,
        skills=load_skill_registry(agents_dir),
        plugins=plugins,
    )


def validate_agent_project(project: AgentProject) -> None:
    manifest = Manifest(
        events=project.events,
        skills=project.skills,
        plugins=project.plugins,
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


def resolve_agent_plugins(
    specs: tuple[AgentSpec, ...],
    plugin_resolver: PluginResolver | None,
) -> dict[str, AgentPlugin]:
    if plugin_resolver is None:
        return {}
    names = sorted(
        {binding.source for spec in specs for binding in spec.ingress}
        | {binding.sink for spec in specs for binding in spec.egress}
    )
    plugins: dict[str, AgentPlugin] = {}
    for name in names:
        plugin = plugin_resolver.resolve(name)
        if plugin is not None:
            plugins[name] = plugin
    return plugins


def load_event_registry(
    agents_dir: Path,
    *,
    plugins: Iterable[AgentPlugin] = (),
) -> EventRegistry:
    """Load flat event payload JSON Schemas from ``agents/events``."""
    events_dir = agents_dir / "events"
    registry = EventRegistry()
    for plugin in plugins:
        for event_type, schema in plugin.events.items():
            register_event_schema(
                registry,
                event_type,
                schema,
                source=f"plugin {plugin.name!r}",
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
