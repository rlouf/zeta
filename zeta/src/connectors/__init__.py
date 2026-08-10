"""Connector vocabulary: bindings and self-described manifests.

A connector is an executable that speaks wire-v0 (spec §13). The
runtime never imports connector code; it reads the executable's
`--describe` manifest for schemas and delivery semantics, spawns it
for ingress, and calls it for egress. This module holds the shapes
that flow between authoring, validation, and the harness.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from zeta.effects import DELIVERY_SEMANTICS, DeliverySemantics


@dataclass(frozen=True)
class IngressBinding:
    """External event binding parsed from an ingress manifest section."""

    event: str
    filter: Mapping[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None


@dataclass(frozen=True)
class EgressBinding:
    """Preserve connector delivery options from a `publishes` declaration."""

    event: str
    options: Mapping[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None


@dataclass(frozen=True)
class OperationSpec:
    """One operation a connector serves via wire-v0 calls."""

    name: str
    semantics: DeliverySemantics
    options_schema: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ConnectorManifest:
    """One connector executable's self-description plus how to spawn it.

    `filters` maps ingress event types to their binding-filter schemas
    and operation names to their binding-options schemas, which is the
    single shape authoring validation consumes.
    """

    id: str
    command: tuple[str, ...]
    events: Mapping[str, Mapping[str, Any] | None] = field(default_factory=dict)
    filters: Mapping[str, Mapping[str, Any] | None] = field(default_factory=dict)
    operations: Mapping[str, OperationSpec] = field(default_factory=dict)
    settings: tuple[str, ...] = ()

    @property
    def ingress_event_types(self) -> tuple[str, ...]:
        return tuple(
            event_type
            for event_type in self.events
            if event_type not in self.operations
        )


class ConnectorManifestError(ValueError):
    """Raised when a connector's describe output is not a valid manifest."""


def connector_manifest_from_describe(
    raw: Any,
    *,
    command: tuple[str, ...],
    expected_id: str | None = None,
) -> ConnectorManifest:
    """Parse and validate one `--describe` JSON document (spec §13.1)."""
    if not isinstance(raw, Mapping):
        raise ConnectorManifestError("describe output must be a JSON object")
    connector_id = raw.get("id")
    if not isinstance(connector_id, str) or not connector_id:
        raise ConnectorManifestError("describe output must carry a non-empty id")
    if expected_id is not None and connector_id != expected_id:
        raise ConnectorManifestError(
            f"describe output id {connector_id!r} does not match "
            f"the executable name {expected_id!r}"
        )
    versions = raw.get("protocol_versions")
    if (
        not isinstance(versions, list)
        or not versions
        or not all(isinstance(item, int) for item in versions)
    ):
        raise ConnectorManifestError("describe output must carry protocol_versions")
    if 0 not in versions:
        raise ConnectorManifestError(
            f"connector {connector_id!r} does not speak wire protocol 0"
        )
    events = _schema_map(raw.get("events"), "events")
    filters = dict(_schema_map(raw.get("filters", {}), "filters"))
    operations = _operation_specs(raw.get("operations", []), events)
    for operation in operations.values():
        if operation.options_schema is not None:
            filters[operation.name] = dict(operation.options_schema)
    raw_settings = raw.get("settings", [])
    if not isinstance(raw_settings, list) or not all(
        isinstance(item, str) for item in raw_settings
    ):
        raise ConnectorManifestError("describe settings must be an array of strings")
    return ConnectorManifest(
        id=connector_id,
        command=command,
        events=events,
        filters=filters,
        operations=operations,
        settings=tuple(raw_settings),
    )


def _operation_specs(
    raw_operations: Any,
    events: Mapping[str, Any],
) -> dict[str, OperationSpec]:
    if not isinstance(raw_operations, list):
        raise ConnectorManifestError("describe operations must be an array")
    operations: dict[str, OperationSpec] = {}
    for entry in raw_operations:
        if not isinstance(entry, Mapping):
            raise ConnectorManifestError("describe operation must be an object")
        name = entry.get("name")
        semantics = entry.get("semantics")
        if not isinstance(name, str) or not name:
            raise ConnectorManifestError("describe operation must carry a name")
        if semantics not in DELIVERY_SEMANTICS:
            raise ConnectorManifestError(
                f"operation {name!r} has invalid delivery semantics {semantics!r}"
            )
        if name not in events:
            raise ConnectorManifestError(
                f"operation {name!r} has no event schema in `events`"
            )
        options_schema = entry.get("options_schema")
        if options_schema is not None and not isinstance(options_schema, Mapping):
            raise ConnectorManifestError(
                f"operation {name!r} options_schema must be an object"
            )
        operations[name] = OperationSpec(
            name=name,
            semantics=semantics,
            options_schema=options_schema,
        )
    return operations


def _schema_map(raw: Any, what: str) -> dict[str, dict[str, Any] | None]:
    if not isinstance(raw, Mapping):
        raise ConnectorManifestError(f"describe {what} must be an object")
    schemas: dict[str, dict[str, Any] | None] = {}
    for event_type, schema in raw.items():
        if not isinstance(event_type, str) or not event_type:
            raise ConnectorManifestError(f"describe {what} keys must be strings")
        if schema is not None and not isinstance(schema, Mapping):
            raise ConnectorManifestError(
                f"describe {what} schema for {event_type!r} must be an object"
            )
        schemas[event_type] = dict(schema) if schema is not None else None
    return schemas


class EventConnectorRegistry:
    """Registration boundary for discovered connector manifests."""

    def __init__(self) -> None:
        self._connectors: dict[str, ConnectorManifest] = {}
        self._event_owners: dict[str, str] = {}

    @property
    def connectors(self) -> Mapping[str, ConnectorManifest]:
        return MappingProxyType(self._connectors)

    def register(self, connector: ConnectorManifest) -> None:
        if connector.id in self._connectors:
            raise ValueError(f"duplicate event connector {connector.id!r}")
        for event_type in connector.events:
            owner = self._event_owners.get(event_type)
            if owner is not None:
                raise ValueError(
                    f"event {event_type!r} is already declared by "
                    f"event connector {owner!r}"
                )
        self._connectors[connector.id] = connector
        for event_type in connector.events:
            self._event_owners[event_type] = connector.id

    def resolve(self, connector_id: str) -> ConnectorManifest | None:
        return self._connectors.get(connector_id)

    def connector_for_event(self, event_type: str) -> ConnectorManifest | None:
        owner = self._event_owners.get(event_type)
        return self._connectors.get(owner) if owner is not None else None

    def event_connectors(self) -> tuple[ConnectorManifest, ...]:
        return tuple(self._connectors.values())

    def has_ingress_connectors(self) -> bool:
        return any(
            connector.ingress_event_types for connector in self._connectors.values()
        )
