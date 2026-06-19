"""Store protocol and shared substrate helpers.

Stores persist immutable objects, mutable refs, and derivations. Object ids
identify stable values. Refs identify moving logical sources. Derivations link
outputs back to immutable inputs for replay, graph traversal, and cache
reasoning.
"""

import logging
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol, cast

from zeta.kernel.objects import Derivation, Object, ObjectId, Ref, RefUpdate

LOGGER = logging.getLogger("zeta.substrate")
_WARNED_FAILURES: set[str] = set()


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


class UnknownSessionError(LookupError):
    """A session id named no recorded trace store."""

    def __init__(self, session_id: str, available: list[str]) -> None:
        super().__init__(session_id)
        self.session_id = session_id
        self.available = available


class IncompatibleSchemaError(Exception):
    """The local trace store schema is incompatible with the runtime."""

    pass


@dataclass(frozen=True)
class TraceStats:
    """Basic trace store size statistics."""

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
    def prompt_object_ids(self) -> list[ObjectId]: ...
    def stats(self) -> TraceStats: ...


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


def warn_trace_failure_once(operation: str, exc: BaseException) -> None:
    """Log one warning per operation before fail-open degradation."""
    if operation in _WARNED_FAILURES:
        return
    _WARNED_FAILURES.add(operation)
    LOGGER.warning("trace disabled for %s after failure: %s", operation, exc)


class StoreBase:
    """Shared graph helpers for concrete stores.

    Graph traversal follows structural object links. It does not infer
    execution order or freshness; those concepts live in derivations and refs.
    """

    def prompt_object_ids(self) -> list[ObjectId]:
        store = cast(Store, self)
        return [object_id_value for object_id_value, _ in store.objects(kind="prompt")]

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
