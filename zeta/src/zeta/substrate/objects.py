"""Core object vocabulary for the content-addressed trace substrate."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

from zeta.addresses import derivation_address, object_address

ObjectId = str
RefName = str

_MIN_IDENTITY_INTEGER = -(2**63)
_MAX_IDENTITY_INTEGER = 2**64 - 1


def canonical_json_bytes(value: Any) -> bytes:
    """Encode a value with the canonical substrate identity rules.

    The explicit value check keeps Python's broader numeric and object model
    from minting identities that another implementation cannot reproduce.
    """
    _validate_identity_value(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _validate_identity_value(value: Any) -> None:
    if value is None or isinstance(value, (bool, str)):
        return
    if isinstance(value, int):
        if not _MIN_IDENTITY_INTEGER <= value <= _MAX_IDENTITY_INTEGER:
            raise ValueError("identity-bearing integers must fit i64 or u64")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("identity-bearing floats must be finite")
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_identity_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("identity-bearing object keys must be strings")
            _validate_identity_value(item)
        return
    raise TypeError(f"unsupported identity-bearing value: {type(value).__name__}")


@dataclass(frozen=True)
class Object:
    """An immutable value in the content-addressed trace substrate.

    Objects represent prompts, messages, tool calls, tool results, effects, and
    other traceable artifacts. Stores address them by hashing their kind,
    schema, payload, and structural links.
    """

    kind: str
    schema: str
    data: dict[str, Any] = field(default_factory=dict)
    links: tuple[ObjectId, ...] = ()

    def content_address(self) -> ObjectId:
        """Return the hash of identity-bearing object fields."""
        payload: dict[str, Any] = {
            "kind": self.kind,
            "schema": self.schema,
            "data": self.data,
            "links": self.links,
        }
        return object_address(canonical_json_bytes(payload))


@dataclass(frozen=True)
class Derivation:
    """A graph edge explaining how one trace object was produced.

    Derivations connect an output object to its input objects, producer name,
    and stable parameters. Trace replay and graph queries use them to explain
    prompt assembly, model responses, and tool-result construction.
    """

    producer: str
    output_id: ObjectId
    input_ids: tuple[ObjectId, ...] = ()
    params: dict[str, Any] = field(default_factory=dict)

    def content_address(self) -> str:
        """Return the hash of identity-bearing derivation fields."""
        payload: dict[str, Any] = {
            "producer": self.producer,
            "output_id": self.output_id,
            "input_ids": self.input_ids,
            "params": self.params,
        }
        return derivation_address(canonical_json_bytes(payload))


@dataclass(frozen=True)
class Ref:
    """A named pointer to an object in the trace substrate.

    Stores resolve refs when callers need a stable name for a moving object,
    such as a session head or latest projection, while keeping the pointed-to
    objects immutable.
    """

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
