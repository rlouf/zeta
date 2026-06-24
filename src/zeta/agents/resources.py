"""Authored-agent resource loading hooks."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zeta.agents.events import EventRegistry, EventRegistryError
from zeta.agents.spec import AgentSpec


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


def resource_extensions(spec: AgentSpec) -> dict[str, object]:
    """Return non-core frontmatter extensions for resource-aware hosts."""
    return dict(spec.extensions or {})


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


def load_event_registry(agents_dir: Path) -> EventRegistry:
    """Load flat event payload JSON Schemas from ``agents/events``."""
    events_dir = agents_dir / "events"
    registry = EventRegistry()
    if not events_dir.exists():
        return registry
    for path in sorted(events_dir.iterdir()):
        if path.suffix != ".json":
            continue
        if not path.is_file() or path.is_symlink():
            continue
        event_type = path.stem
        schema = load_event_schema(path)
        try:
            registry.register(event_type, schema)
        except EventRegistryError as exc:
            raise ResourceError(f"invalid event resource {path}: {exc}") from exc
    return registry


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
