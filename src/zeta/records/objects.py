"""Content-addressed substrate for Zeta.

The substrate separates three concerns: immutable objects, mutable refs, and
derivations that explain how objects were built.

"""

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

ObjectId = str
RefName = str


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
