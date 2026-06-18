"""Deployment manifest validation for authored agents."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..capabilities import CapabilityRegistry
from .events import EventRegistry
from .prompts import validate_prompt
from .spec import AgentSpec

RESERVED_TOOL_NAMES = frozenset({"__return"})


class ManifestError(ValueError):
    """Raised when an authored spec does not match a deployment manifest."""


@dataclass(frozen=True)
class Manifest:
    """Deployment manifest used to validate authored agent specs."""

    tools: CapabilityRegistry | None = None
    events: EventRegistry | None = None
    extensions: Mapping[str, type[Any]] | None = None

    def validate(self, spec: AgentSpec) -> None:
        validate_prompt(spec)
        validate_tools(spec, self.tools)
        validate_events(spec, self.events)
        validate_extensions(spec, self.extensions or {})


def validate_tools(spec: AgentSpec, registry: CapabilityRegistry | None) -> None:
    if registry is None:
        if spec.tools:
            raise ManifestError(
                f"agent {spec.slug!r} lists unknown tool {spec.tools[0]!r}"
            )
        return
    for name in spec.tools:
        if name in RESERVED_TOOL_NAMES:
            raise ManifestError(f"agent {spec.slug!r} lists reserved tool {name!r}")
        if registry.resolve(name) is None:
            raise ManifestError(f"agent {spec.slug!r} lists unknown tool {name!r}")


def validate_events(spec: AgentSpec, registry: EventRegistry | None) -> None:
    if registry is None:
        if spec.accepts:
            raise ManifestError(
                f"agent {spec.slug!r} references unknown event {spec.accepts[0]!r}"
            )
        if spec.returns:
            raise ManifestError(
                f"agent {spec.slug!r} references unknown event {spec.returns[0]!r}"
            )
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
    for key, value in (spec.extensions or {}).items():
        expected = extensions.get(key)
        if expected is None:
            raise ManifestError(f"agent {spec.slug!r} uses unknown extension {key!r}")
        if not isinstance(value, expected):
            raise ManifestError(
                f"agent {spec.slug!r} extension {key!r} has invalid shape"
            )
