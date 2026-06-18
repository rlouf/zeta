"""Substrate ref records."""

from __future__ import annotations

from dataclasses import dataclass

from .object import ObjectId

RefName = str


@dataclass(frozen=True)
class Ref:
    """Resolved mutable substrate ref."""

    name: RefName
    object_id: ObjectId


@dataclass(frozen=True)
class RefUpdate:
    """Result of moving a mutable ref."""

    name: RefName
    old_object_id: ObjectId | None
    new_object_id: ObjectId
    updated: bool
