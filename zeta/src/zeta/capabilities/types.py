"""Capability declaration domain shapes."""

from dataclasses import dataclass
from typing import Any

from zeta.effects import DELIVERY_SEMANTICS, DeliverySemantics


@dataclass(frozen=True)
class CapabilityId:
    """The canonical identity of a tool-like capability.

    Libraries use the provider/name pair to distinguish implementations that
    may share the same model-facing alias, such as host and client tools both
    named `read`.
    """

    provider: str
    name: str

    def canonical(self) -> str:
        return f"{self.provider}.{self.name}"


@dataclass(frozen=True)
class Capability:
    """The model-facing declaration of a capability.

    Registries expose specs as tool descriptors and validate model-supplied
    arguments against `input_schema`.
    """

    id: CapabilityId
    description: str
    input_schema: dict[str, Any]
    delivery_semantics: DeliverySemantics | None = None
    mutates: bool = False

    def __post_init__(self) -> None:
        if (
            self.delivery_semantics is not None
            and self.delivery_semantics not in DELIVERY_SEMANTICS
        ):
            raise ValueError(f"invalid delivery semantics {self.delivery_semantics!r}")
