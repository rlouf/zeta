"""Deployment manifest validation for authored agents."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from connectors import (
    EgressBinding,
    EventConnector,
    EventConnectorRegistry,
    IngressBinding,
)
from zeta.agents.events import EventRegistry
from zeta.agents.prompts import validate_prompt
from zeta.agents.spec import AgentSpec

RESERVED_TOOL_NAMES = frozenset({"__return"})


class ManifestError(ValueError):
    """Raised when an authored spec does not match a deployment manifest."""


@runtime_checkable
class ToolResolver(Protocol):
    """Anything that can look up a tool by name."""

    def resolve(self, name: str) -> Any: ...


@runtime_checkable
class SkillResolver(Protocol):
    """Anything that can look up a skill by name."""

    def knows(self, name: str) -> bool: ...


@dataclass(frozen=True)
class Manifest:
    """Deployment manifest used to validate authored agent specs."""

    tools: ToolResolver | None = None
    skills: SkillResolver | Mapping[str, Any] | None = None
    events: EventRegistry | None = None
    connectors: EventConnectorRegistry | None = None

    def validate(self, spec: AgentSpec) -> None:
        validate_prompt(spec)
        validate_tools(spec, self.tools)
        validate_skills(spec, self.skills)
        validate_manifest_sections(spec)
        validate_connector_bindings(spec, self.connectors)
        validate_events(spec, self.events)


def validate_tools(spec: AgentSpec, registry: ToolResolver | None) -> None:
    for name in spec.tools:
        if name in RESERVED_TOOL_NAMES:
            raise ManifestError(f"agent {spec.slug!r} lists reserved tool {name!r}")
        if registry is not None and registry.resolve(name) is None:
            raise ManifestError(f"agent {spec.slug!r} lists unknown tool {name!r}")


def validate_skills(
    spec: AgentSpec,
    registry: SkillResolver | Mapping[str, Any] | None,
) -> None:
    if registry is None:
        return
    for name in spec.skills:
        if isinstance(registry, Mapping):
            known = name in registry
        else:
            known = registry.knows(name)
        if not known:
            raise ManifestError(f"agent {spec.slug!r} lists unknown skill {name!r}")


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


def validate_connector_bindings(
    spec: AgentSpec,
    connectors: EventConnectorRegistry | None,
) -> None:
    for binding in ingress_bindings(spec):
        validate_ingress_binding(spec, binding, connectors)
    for binding in egress_bindings(spec):
        validate_egress_binding(spec, binding, connectors)


def validate_manifest_sections(spec: AgentSpec) -> None:
    for key in spec.manifest:
        raise ManifestError(
            f"agent {spec.slug!r} uses unknown manifest section {key!r}"
        )


def validate_ingress_binding(
    spec: AgentSpec,
    binding: IngressBinding,
    connectors: EventConnectorRegistry | None,
) -> None:
    connector = connector_for_event(connectors, binding.event)
    if connector is None:
        raise ManifestError(
            f"agent {spec.slug!r} references unknown ingress event {binding.event!r}"
        )
    if binding.event not in spec.accepts:
        raise ManifestError(
            f"agent {spec.slug!r} ingress event {binding.event!r} is not listed in accepts"
        )
    if binding.idempotency_key is None:
        raise ManifestError(
            f"agent {spec.slug!r} ingress event {binding.event!r} requires idempotency_key"
        )
    validate_binding_config(
        binding.filter,
        connector.filters.get(binding.event),
        f"agent {spec.slug!r} has invalid ingress filter for {binding.event!r}",
        "filter",
    )


def validate_egress_binding(
    spec: AgentSpec,
    binding: EgressBinding,
    connectors: EventConnectorRegistry | None,
) -> None:
    connector = connector_for_event(connectors, binding.event)
    if connector is None:
        raise ManifestError(
            f"agent {spec.slug!r} references unknown egress event {binding.event!r}"
        )
    if binding.event not in spec.returns:
        raise ManifestError(
            f"agent {spec.slug!r} egress event {binding.event!r} is not listed in returns"
        )
    validate_binding_config(
        binding.options,
        connector.filters.get(binding.event),
        f"agent {spec.slug!r} has invalid egress options for {binding.event!r}",
        "options",
    )


def connector_for_event(
    connectors: EventConnectorRegistry | None,
    event_type: str,
) -> EventConnector | None:
    if connectors is None:
        return None
    return connectors.connector_for_event(event_type)


def validate_binding_config(
    value: Mapping[str, Any],
    schema: Mapping[str, Any] | None,
    message: str,
    noun: str,
) -> None:
    if schema is None:
        if value:
            verb = "is" if noun == "filter" else "are"
            raise ManifestError(f"{message}: {noun} {verb} not supported")
        return
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(dict(value))
    except SchemaError as exc:
        raise ManifestError(
            f"{message}: connector {noun} schema is invalid: {exc.message}"
        ) from exc
    except ValidationError as exc:
        raise ManifestError(f"{message}: {exc.message}") from exc


def ingress_bindings(spec: AgentSpec) -> tuple[IngressBinding, ...]:
    return spec.ingress


def egress_bindings(spec: AgentSpec) -> tuple[EgressBinding, ...]:
    return spec.egress
