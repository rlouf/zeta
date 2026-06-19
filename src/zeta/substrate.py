"""Core content-addressed substrate for Zeta.

The substrate separates three concerns that are easy to blur in agent systems:
immutable objects, mutable refs, and derivations that explain how objects were
built.
"""

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

ObjectId = str
RefName = str


def trace_object_id(event: dict[str, Any], field: str) -> ObjectId | None:
    value = event.get(field)
    if isinstance(value, str) and value.startswith("sha256:"):
        return value
    return None


@dataclass(frozen=True)
class Object:
    """Immutable content-addressed value.

    `kind` is the broad object kind, such as `message` or `context`. `schema`
    identifies the payload shape. `data` is the JSON payload. `links` are
    ordered structural dependencies included in the content address.
    """

    kind: str
    schema: str
    data: dict[str, Any] = field(default_factory=dict)
    links: tuple[ObjectId, ...] = ()

    def content_address(self) -> ObjectId:
        """Return the hash of `kind`, `schema`, `data`, and structural links."""
        payload: dict[str, Any] = {
            "kind": self.kind,
            "schema": self.schema,
            "data": self.data,
            "links": self.links,
        }
        content = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        digest = hashlib.sha256(content.encode()).hexdigest()
        return f"sha256:{digest}"


@dataclass(frozen=True)
class Derivation:
    """Semantic build record for an object.

    This is not an execution event. It records the durable build relationship
    needed for replay and cache reasoning, not latency, retries, request ids,
    worker identity, or logs.
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
            allow_nan=False,
        )
        digest = hashlib.sha256(content.encode()).hexdigest()
        return f"derivation:{digest}"


@dataclass(frozen=True)
class Ref:
    """Resolved ref: stable mutable name plus current object id."""

    name: RefName
    object_id: ObjectId


@dataclass(frozen=True)
class RefUpdate:
    """Result of a conditional ref move.

    A failed move is not an error. If the ref no longer has the expected value,
    `updated` is false and `old_object_id` reports the value that was actually
    observed.
    """

    name: RefName
    old_object_id: ObjectId | None
    new_object_id: ObjectId
    updated: bool


__all__ = [
    "Derivation",
    "Object",
    "ObjectId",
    "Ref",
    "RefName",
    "RefUpdate",
    "trace_object_id",
]
