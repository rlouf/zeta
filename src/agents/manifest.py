"""Deployment manifest validation for authored agents."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from agents.events import EventRegistry
from agents.prompts import validate_prompt
from agents.spec import AgentSpec

RESERVED_TOOL_NAMES = frozenset({"__return"})


class ManifestError(ValueError):
    """Raised when an authored spec does not match a deployment manifest."""


@runtime_checkable
class ToolResolver(Protocol):
    """Anything that can look up a tool by name."""

    def resolve(self, name: str) -> Any: ...


@dataclass(frozen=True)
class Manifest:
    """Deployment manifest used to validate authored agent specs."""

    tools: ToolResolver | None = None
    events: EventRegistry | None = None
    extensions: Mapping[str, type[Any]] | None = None

    def validate(self, spec: AgentSpec) -> None:
        validate_prompt(spec)
        validate_tools(spec, self.tools)
        validate_events(spec, self.events)
        validate_extensions(spec, self.extensions or {})


def validate_tools(spec: AgentSpec, registry: ToolResolver | None) -> None:
    for name in spec.tools:
        if name in RESERVED_TOOL_NAMES:
            raise ManifestError(f"agent {spec.slug!r} lists reserved tool {name!r}")
        if registry is not None and registry.resolve(name) is None:
            raise ManifestError(f"agent {spec.slug!r} lists unknown tool {name!r}")


def validate_events(spec: AgentSpec, registry: EventRegistry | None) -> None:
    if registry is None:
        return
    for event_type in spec.accepts:
        if not registry.knows(event_type):
            raise ManifestError(
                f"agent {spec.slug!r} references unknown event {event_type!r} in accepts"
            )
    for event_type in spec.returns:
        if not registry.knows(event_type):
            raise ManifestError(
                f"agent {spec.slug!r} references unknown event {event_type!r} in returns"
            )


def validate_extensions(spec: AgentSpec, extensions: Mapping[str, type[Any]]) -> None:
    for key in spec.extensions or {}:
        if key not in extensions:
            raise ManifestError(f"agent {spec.slug!r} uses unknown extension {key!r}")
