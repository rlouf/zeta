"""Refs are mutable logical names for substrate objects.

Objects are immutable and content-addressed; refs are the moving pointers that
connect stable names such as `session/s1/head` or `file/REFERENCES.md` to the
current object for that source.

Refs are where source-specific maintenance enters the system. A file watcher
can keep `file/REFERENCES.md` pointed at the latest object for that file; a
session runtime can keep `session/s1/head` pointed at the latest message.

Because refs are mutable, moves are conditional. A writer must say what object
it observed before moving the ref. That prevents silent overwrites when
multiple agents or tasks update the same logical state concurrently.
"""

from __future__ import annotations

from dataclasses import dataclass

from .object import ObjectId

RefName = str


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
    observed. Errors are reserved for store failures and invalid requests.
    """

    name: RefName
    old_object_id: ObjectId | None
    new_object_id: ObjectId
    updated: bool
