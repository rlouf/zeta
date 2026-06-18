"""Substrate derivation records."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .object import ObjectId


@dataclass(frozen=True)
class Derivation:
    """Record how an object was produced."""

    producer: str
    output_id: ObjectId
    input_ids: tuple[ObjectId, ...] = ()
    params: dict[str, Any] = field(default_factory=dict)

    def content_address(self) -> str:
        """Return the deterministic content address for this derivation."""
        payload: dict[str, Any] = {
            "producer": self.producer,
            "output_id": self.output_id,
            "input_ids": self.input_ids,
            "params": self.params,
        }
        content = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(content.encode()).hexdigest()
        return f"derivation:{digest}"
