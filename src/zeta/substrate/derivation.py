"""Derivation records explain how substrate objects were built."""

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .object import ObjectId


@dataclass(frozen=True)
class Derivation:
    """Semantic build record for an object.

    This is not an execution event. It intentionally does not record latency,
    retries, request ids, worker identity, or logs. It records the durable
    build relationship needed for replay and cache reasoning.

    `input_ids` are immutable object inputs. `producer` should name the
    producer and version. `params` are the producer parameters that affect the
    produced object.
    """

    producer: str
    output_id: ObjectId
    input_ids: tuple[ObjectId, ...] = ()
    params: dict[str, Any] = field(default_factory=dict)

    def content_address(self) -> str:
        """Return the hash of the identity-bearing derivation fields."""
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
