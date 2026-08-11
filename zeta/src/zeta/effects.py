"""External effect delivery contracts shared by connectors and capabilities."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from zeta.addresses import chain_address

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


def effect_key(
    scope: str,
    operation: str,
    params: Mapping[str, Any],
) -> str:
    """Return an attempt-independent identity for one logical effect."""
    encoded = json.dumps(
        {"scope": scope, "operation": operation, "params": dict(params)},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return f"effect:{chain_address(encoded)}"
