"""Event connector interfaces for ingress and egress integrations."""

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from zeta.events import DraftEvent, Event


@dataclass(frozen=True)
class IngressBinding:
    """External event binding parsed from an ingress manifest section."""

    event: str
    filter: Mapping[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None


@dataclass(frozen=True)
class EgressBinding:
    """External event binding parsed from a returned event declaration."""

    event: str
    options: Mapping[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None


IngressInput = Mapping[str, Any] | None
IngressHandler = Callable[
    [IngressBinding, IngressInput],
    Iterable[DraftEvent] | Awaitable[Iterable[DraftEvent]],
]
EgressHandler = Callable[
    [Event, EgressBinding, str],
    Mapping[str, Any] | None | Awaitable[Mapping[str, Any] | None],
]


@dataclass(frozen=True)
class InboundRequest:
    method: str
    path: str
    headers: Mapping[str, str]
    query: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class InboundResponse:
    status_code: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)


PushIngressHandler = Callable[
    [InboundRequest],
    tuple[InboundResponse, Iterable[DraftEvent]]
    | Awaitable[tuple[InboundResponse, Iterable[DraftEvent]]],
]


@dataclass(frozen=True)
class EventConnector:
    """Event ingress and egress contributed by an installed connector."""

    id: str
    events: Mapping[str, Mapping[str, Any] | None] = field(default_factory=dict)
    ingress: Mapping[str, IngressHandler] = field(default_factory=dict)
    push_ingress: PushIngressHandler | None = None
    egress: Mapping[str, EgressHandler] = field(default_factory=dict)
    filters: Mapping[str, Mapping[str, Any] | None] = field(default_factory=dict)


class EventConnectorRegistry:
    """Mutable registration boundary for installed event connectors."""

    def __init__(self) -> None:
        self._connectors: dict[str, EventConnector] = {}

    @property
    def connectors(self) -> Mapping[str, EventConnector]:
        return MappingProxyType(self._connectors)

    def register(self, connector: EventConnector) -> None:
        if connector.id in self._connectors:
            raise ValueError(f"duplicate event connector {connector.id!r}")
        self._connectors[connector.id] = connector

    def resolve(self, connector_id: str) -> EventConnector | None:
        return self._connectors.get(connector_id)

    def connector_for_event(self, event_type: str) -> EventConnector | None:
        for connector in self._connectors.values():
            if event_type in connector.events:
                return connector
        return None

    def event_connectors(self) -> tuple[EventConnector, ...]:
        return tuple(self._connectors.values())

    def has_ingress_connectors(self) -> bool:
        return any(connector.ingress for connector in self._connectors.values())

    def push_ingress_connectors(self) -> Mapping[str, EventConnector]:
        return MappingProxyType(
            {
                connector_id: connector
                for connector_id, connector in self._connectors.items()
                if connector.push_ingress is not None
            }
        )
