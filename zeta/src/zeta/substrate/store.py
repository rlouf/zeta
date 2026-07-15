"""Store protocol and shared substrate helpers.

Stores persist immutable objects, mutable refs, and derivations. Object ids
identify stable values. Refs identify moving logical sources. Derivations link
outputs back to immutable inputs for replay, graph traversal, and cache
reasoning.
"""

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol, cast

from zeta.substrate.objects import Derivation, Object, ObjectId, Ref, RefUpdate


class UnknownIdError(LookupError):
    """A trace id token matched no ref, object id, or prefix."""

    def __init__(self, token: str) -> None:
        super().__init__(token)
        self.token = token


class AmbiguousIdError(LookupError):
    """A trace id prefix matched more than one object."""

    def __init__(self, token: str, candidates: list[ObjectId]) -> None:
        super().__init__(token)
        self.token = token
        self.candidates = candidates


class IncompatibleSchemaError(Exception):
    """The local substrate schema is incompatible with the runtime."""

    pass


@dataclass(frozen=True)
class StoreStats:
    """Basic substrate store size statistics."""

    object_count: int
    total_bytes: int


def escape_like(text: str) -> str:
    """Escape SQLite LIKE wildcards so they match literally."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class Store(Protocol):
    """Storage API shared by in-memory and SQLite stores.

    Implementations store objects by content address, move refs conditionally,
    and record derivations that explain how outputs were built. The protocol is
    intentionally small so callers can use either ephemeral memory storage or
    durable local SQLite without changing trace-building code.
    """

    def put_object(self, obj: Object) -> ObjectId: ...
    def get_object(self, object_id: ObjectId) -> Object | None: ...
    def object_ids_with_prefix(
        self, prefix: str, limit: int = 16
    ) -> list[ObjectId]: ...
    def move_ref(
        self,
        name: str,
        expected: ObjectId | None,
        new: ObjectId,
    ) -> RefUpdate: ...
    def get_ref(self, name: str) -> Ref | None: ...
    def batch(self) -> AbstractContextManager[None]: ...
    def record_derivation(self, derivation: Derivation) -> str: ...
    def derivations_for_output(self, output_id: ObjectId) -> list[Derivation]: ...
    def derivations_for_input(self, input_id: ObjectId) -> list[Derivation]: ...
    def graph_closure(self, roots: list[ObjectId]) -> dict[ObjectId, Object]: ...
    def refs(self) -> list[Ref]: ...
    def objects(
        self, kind: str | tuple[str, ...] | None = None, limit: int | None = None
    ) -> list[tuple[ObjectId, Object]]: ...
    def search_objects(
        self,
        pattern: str,
        kind: str | tuple[str, ...] | None = None,
        limit: int | None = None,
    ) -> list[tuple[ObjectId, Object]]: ...
    def stats(self) -> StoreStats: ...


def resolve_object_id(store: Store, token: str) -> ObjectId:
    """Resolve a ref name, full object id, or unique id prefix to an object id.

    A bare hex token matches the digest part, so `sha256:` never needs
    typing. Refs win over prefixes; an ambiguous prefix raises with the
    candidate ids.
    """
    if not token:
        raise UnknownIdError(token)
    ref_target = store.get_ref(token)
    if ref_target is not None:
        return ref_target.object_id
    if store.get_object(token) is not None:
        return token
    prefix = token if token.startswith("sha256:") else f"sha256:{token}"
    candidates = store.object_ids_with_prefix(prefix)
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        raise AmbiguousIdError(token, candidates)
    raise UnknownIdError(token)


class StoreBase:
    """Shared graph helpers for concrete stores.

    Graph traversal follows structural object links. It does not infer
    execution order or freshness; those concepts live in derivations and refs.
    """

    def graph_closure(self, roots: list[ObjectId]) -> dict[ObjectId, Object]:
        store = cast(Store, self)
        closure: dict[ObjectId, Object] = {}
        pending = list(roots)
        while pending:
            object_id_value = pending.pop()
            if object_id_value in closure:
                continue
            obj = store.get_object(object_id_value)
            if obj is None:
                continue
            closure[object_id_value] = obj
            pending.extend(reversed(obj.links))
        return closure
