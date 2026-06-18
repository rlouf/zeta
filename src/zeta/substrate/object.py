"""Content-addressed substrate objects."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

ObjectId = str


@dataclass(frozen=True)
class Object:
    """Content-addressed object with ordered links to other objects."""

    kind: str
    schema: str
    data: dict[str, Any] = field(default_factory=dict)
    links: tuple[ObjectId, ...] = ()

    def content_address(self) -> ObjectId:
        """Return the deterministic content address for this object."""
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
