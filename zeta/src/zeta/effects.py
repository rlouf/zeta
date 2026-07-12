"""External effect delivery contracts shared by connectors and capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

DeliverySemantics = Literal[
    "idempotent_with_key",
    "connector_deduplicated",
    "at_least_once",
    "unsafe_to_retry",
]

DELIVERY_SEMANTICS = frozenset(
    {
        "idempotent_with_key",
        "connector_deduplicated",
        "at_least_once",
        "unsafe_to_retry",
    }
)


@dataclass(frozen=True)
class EffectDeliveryError(RuntimeError):
    """A connector or capability effect failed under declared semantics."""

    effect_key: str
    semantics: DeliverySemantics
    message: str

    @property
    def dispatch_error_code(self) -> str:
        if self.semantics == "unsafe_to_retry":
            return "unsafe_effect_ambiguous"
        return "effect_delivery_failed"

    def __post_init__(self) -> None:
        super().__init__(self.message)
