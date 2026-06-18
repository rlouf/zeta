"""Objects are the value plane of the substrate.

An object says "this value exists" and nothing more. Its id is computed from
the canonical JSON representation of its identity-bearing fields: `kind`,
`schema`, `data`, and `links`.

`links` are structural value dependencies. They mean that the linked objects
are part of this object's value. They do not mean "this run happened before
that run", "this ref was updated", or "this worker produced this value".
Provenance is represented by derivations in the store layer.

JSON object key order does not affect object identity.
"""

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

ObjectId = str


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
        )
        digest = hashlib.sha256(content.encode()).hexdigest()
        return f"sha256:{digest}"
