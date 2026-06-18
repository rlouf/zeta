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

    def normalized(self) -> Derivation:
        """Return a derivation whose identity is stable across store backends."""
        return Derivation(
            producer=self.producer,
            output_id=self.output_id,
            input_ids=tuple(str(input_id) for input_id in self.input_ids),
            params=normalize_json(self.params),
        )

    def content_id(self, *, session_id: str | None = None) -> str:
        """Return the deterministic content address for this derivation."""
        payload: dict[str, Any] = {
            "producer": self.producer,
            "output_id": self.output_id,
            "input_ids": self.input_ids,
            "params": self.params,
        }
        if session_id is not None:
            payload = {"session_id": session_id, **payload}
        digest = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
        return f"derivation:{digest}"
