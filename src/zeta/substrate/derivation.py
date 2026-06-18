"""Substrate derivation records."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from .object import ObjectId, canonical_json, normalize_json


@dataclass(frozen=True)
class Derivation:
    """Record how a trace object was produced."""

    producer: str
    output_id: ObjectId
    input_ids: tuple[ObjectId, ...] = ()
    params: dict[str, Any] = field(default_factory=dict)


def derivation_payload(derivation: Derivation) -> dict[str, Any]:
    """Return the canonical derivation payload."""
    return {
        "producer": derivation.producer,
        "output_id": derivation.output_id,
        "input_ids": list(derivation.input_ids),
        "params": derivation.params,
    }


def derivation_id(derivation: Derivation) -> str:
    """Return the deterministic content address for a derivation record."""
    digest = hashlib.sha256(
        canonical_json(derivation_payload(derivation)).encode()
    ).hexdigest()
    return f"derivation:{digest}"


def normalize_derivation(derivation: Derivation) -> Derivation:
    """Return a derivation with normalized containers."""
    return Derivation(
        producer=derivation.producer,
        output_id=derivation.output_id,
        input_ids=tuple(str(input_id) for input_id in derivation.input_ids),
        params=normalize_json(derivation.params),
    )
