"""Deployment manifest validation for authored agents."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast, runtime_checkable

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from connectors import (
    EgressBinding,
    EventConnector,
    EventConnectorResolver,
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
    connectors: EventConnectorResolver | Mapping[str, EventConnector] | None = None

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
    connectors: EventConnectorResolver | Mapping[str, EventConnector] | None,
) -> None:
    connector_map = resolved_connectors_for_spec(spec, connectors)
    for binding in ingress_bindings(spec):
        validate_ingress_binding(spec, binding, connector_map)
    for binding in egress_bindings(spec):
        validate_egress_binding(spec, binding, connector_map)


def validate_manifest_sections(spec: AgentSpec) -> None:
    for key in spec.manifest:
        if key in {"ingress", "egress"}:
            continue
        raise ManifestError(
            f"agent {spec.slug!r} uses unknown manifest section {key!r}"
        )


def validate_ingress_binding(
    spec: AgentSpec,
    binding: IngressBinding,
    connectors: Mapping[str, EventConnector],
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
    validate_binding_filter(
        binding.filter,
        connector.filters.get(binding.event),
        f"agent {spec.slug!r} has invalid ingress filter for {binding.event!r}",
    )


def validate_egress_binding(
    spec: AgentSpec,
    binding: EgressBinding,
    connectors: Mapping[str, EventConnector],
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
    validate_binding_filter(
        binding.filter,
        connector.filters.get(binding.event),
        f"agent {spec.slug!r} has invalid egress filter for {binding.event!r}",
    )


def resolved_connectors_for_spec(
    spec: AgentSpec,
    connectors: EventConnectorResolver | Mapping[str, EventConnector] | None,
) -> Mapping[str, EventConnector]:
    if isinstance(connectors, Mapping):
        return cast(Mapping[str, EventConnector], connectors)
    if connectors is None:
        return {}
    resolved: dict[str, EventConnector] = {}
    for key, value in spec.manifest.items():
        for connector_id in connectors.names_for_section(key, value):
            connector = connectors.resolve(connector_id)
            if connector is not None:
                resolved[connector_id] = connector
    return resolved


def connector_for_event(
    connectors: Mapping[str, EventConnector],
    event_type: str,
) -> EventConnector | None:
    for connector in connectors.values():
        if event_type in connector.events:
            return connector
    return None


def validate_binding_filter(
    value: Mapping[str, Any],
    schema: Mapping[str, Any] | None,
    message: str,
) -> None:
    if schema is None:
        if value:
            raise ManifestError(f"{message}: filter is not supported")
        return
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(dict(value))
    except SchemaError as exc:
        raise ManifestError(
            f"{message}: connector filter schema is invalid: {exc.message}"
        ) from exc
    except ValidationError as exc:
        raise ManifestError(f"{message}: {exc.message}") from exc


def ingress_bindings(spec: AgentSpec) -> tuple[IngressBinding, ...]:
    section = spec.manifest.get("ingress", ())
    return tuple(
        IngressBinding(
            event=required_binding_string(item, "ingress", "event", spec),
            filter=binding_filter(item.get("filter", {}), "ingress", spec),
            idempotency_key=optional_binding_string(
                item.get("idempotency_key"),
                "ingress",
                "idempotency_key",
                spec,
            ),
        )
        for item in binding_items(section, "ingress", spec)
    )


def egress_bindings(spec: AgentSpec) -> tuple[EgressBinding, ...]:
    section = spec.manifest.get("egress", ())
    return tuple(
        EgressBinding(
            event=required_binding_string(item, "egress", "event", spec),
            filter=binding_filter(item.get("filter", {}), "egress", spec),
            idempotency_key=optional_binding_string(
                item.get("idempotency_key"),
                "egress",
                "idempotency_key",
                spec,
            ),
        )
        for item in binding_items(section, "egress", spec)
    )


def binding_items(
    section: Any,
    key: str,
    spec: AgentSpec,
) -> tuple[Mapping[str, Any], ...]:
    if section is None or section == ():
        return ()
    if not isinstance(section, list | tuple):
        raise ManifestError(
            f"agent {spec.slug!r} has invalid {key!r} section: expected list"
        )
    return tuple(binding_item(item, key, spec) for item in section)


def binding_item(value: Any, key: str, spec: AgentSpec) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ManifestError(
            f"agent {spec.slug!r} has invalid {key!r} section: expected object"
        )
    supported = {"event", "filter", "idempotency_key"}
    unknown = sorted(set(value) - supported)
    if unknown:
        raise ManifestError(
            f"agent {spec.slug!r} has invalid {key!r} section: "
            f"unsupported field {unknown[0]!r}"
        )
    return value


def required_binding_string(
    value: Mapping[str, Any],
    key: str,
    name: str,
    spec: AgentSpec,
) -> str:
    item = value.get(name)
    if not isinstance(item, str) or item == "":
        raise ManifestError(
            f"agent {spec.slug!r} has invalid {key!r} section: {name} is required"
        )
    return item


def optional_binding_string(
    value: Any,
    key: str,
    name: str,
    spec: AgentSpec,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value == "":
        raise ManifestError(
            f"agent {spec.slug!r} has invalid {key!r} section: {name} must be a string"
        )
    return value


def binding_filter(value: Any, key: str, spec: AgentSpec) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ManifestError(
            f"agent {spec.slug!r} has invalid {key!r} section: filter must be an object"
        )
    return dict(value)
