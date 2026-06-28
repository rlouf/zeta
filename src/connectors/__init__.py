"""Event connector interfaces for ingress and egress integrations."""

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from zeta.events import DraftEvent, Event


@dataclass(frozen=True)
class IngressBinding:
    """External event binding parsed from an ingress manifest section."""

    event: str
    filter: Mapping[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None


@dataclass(frozen=True)
class EgressBinding:
    """External event binding parsed from an egress manifest section."""

    event: str
    filter: Mapping[str, Any] = field(default_factory=dict)
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
class EventConnector:
    """Event ingress and egress contributed by an installed connector."""

    id: str
    events: Mapping[str, Mapping[str, Any] | None] = field(default_factory=dict)
    ingress: Mapping[str, IngressHandler] = field(default_factory=dict)
    egress: Mapping[str, EgressHandler] = field(default_factory=dict)
    filters: Mapping[str, Mapping[str, Any] | None] = field(default_factory=dict)


@runtime_checkable
class EventConnectorResolver(Protocol):
    """Anything that can resolve installed event connectors."""

    def resolve(self, connector_id: str) -> EventConnector | None: ...

    def names_for_section(self, key: str, value: Any) -> Iterable[str]: ...
