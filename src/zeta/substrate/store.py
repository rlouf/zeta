"""Substrate store protocols and in-memory implementation."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Protocol, cast

from .derivation import Derivation
from .object import (
    Object,
    ObjectId,
    TraceStats,
    canonical_json,
    normalize_object,
    object_id,
    object_payload,
)
from .refs import (
    REF_EXPECTED_UNSET,
    AmbiguousIdError,
    RefConflictError,
    UnknownIdError,
)

LOGGER = logging.getLogger("zeta.substrate")
_WARNED_FAILURES: set[str] = set()


class Store(Protocol):
    """Storage API shared by in-memory and SQLite stores."""

    def put_object(self, obj: Object) -> ObjectId: ...
    def get_object(self, object_id: ObjectId) -> Object | None: ...
    def object_ids_with_prefix(
        self, prefix: str, limit: int = 16
    ) -> list[ObjectId]: ...
    def set_ref(
        self,
        name: str,
        object_id: ObjectId,
        *,
        expected: ObjectId | None | object = REF_EXPECTED_UNSET,
    ) -> None: ...
    def get_ref(self, name: str) -> ObjectId | None: ...
    def batch(self) -> AbstractContextManager[None]: ...
    def record_derivation(self, derivation: Derivation) -> str: ...
    def derivations_for_output(self, output_id: ObjectId) -> list[Derivation]: ...
    def derivations_for_input(self, input_id: ObjectId) -> list[Derivation]: ...
    def graph_closure(self, roots: list[ObjectId]) -> dict[ObjectId, Object]: ...
    def refs(self) -> dict[str, ObjectId]: ...
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
        return ref_target
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
    """Shared graph helpers for concrete stores."""

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


class InMemoryStore(StoreBase):
    """Process-local trace store for tests and short-lived traces."""

    def __init__(self) -> None:
        self._objects: dict[ObjectId, Object] = {}
        self._refs: dict[str, ObjectId] = {}
        self.derivations: dict[str, Derivation] = {}

    def put_object(self, obj: Object) -> ObjectId:
        stored = normalize_object(obj)
        object_id_value = object_id(stored)
        self._objects.setdefault(object_id_value, stored)
        return object_id_value

    def get_object(self, object_id: ObjectId) -> Object | None:
        return self._objects.get(object_id)

    def object_ids_with_prefix(self, prefix: str, limit: int = 16) -> list[ObjectId]:
        matches = sorted(
            object_id_value
            for object_id_value in self._objects
            if object_id_value.startswith(prefix)
        )
        return matches[:limit]

    def objects(
        self, kind: str | tuple[str, ...] | None = None, limit: int | None = None
    ) -> list[tuple[ObjectId, Object]]:
        kinds = (kind,) if isinstance(kind, str) else kind
        listed = [
            (object_id_value, obj)
            for object_id_value, obj in reversed(self._objects.items())
            if kinds is None or obj.kind in kinds
        ]
        return listed if limit is None else listed[:limit]

    def search_objects(
        self,
        pattern: str,
        kind: str | tuple[str, ...] | None = None,
        limit: int | None = None,
    ) -> list[tuple[ObjectId, Object]]:
        needle = pattern.lower()
        listed = [
            (object_id_value, obj)
            for object_id_value, obj in self.objects(kind=kind)
            if needle in canonical_json(obj.data).lower()
        ]
        return listed if limit is None else listed[:limit]

    def set_ref(
        self,
        name: str,
        object_id: ObjectId,
        *,
        expected: ObjectId | None | object = REF_EXPECTED_UNSET,
    ) -> None:
        if expected is not REF_EXPECTED_UNSET:
            actual = self._refs.get(name)
            if actual != expected:
                raise RefConflictError(
                    name,
                    expected=cast(ObjectId | None, expected),
                    actual=actual,
                )
        self._refs[name] = object_id

    def get_ref(self, name: str) -> ObjectId | None:
        return self._refs.get(name)

    @contextmanager
    def batch(self) -> Iterator[None]:
        yield

    def record_derivation(self, derivation: Derivation) -> str:
        id_value = derivation.content_address()
        self.derivations.setdefault(id_value, derivation)
        return id_value

    def derivations_for_output(self, output_id: ObjectId) -> list[Derivation]:
        return [
            derivation
            for derivation in self.derivations.values()
            if derivation.output_id == output_id
        ]

    def derivations_for_input(self, input_id: ObjectId) -> list[Derivation]:
        return [
            derivation
            for derivation in self.derivations.values()
            if input_id in derivation.input_ids
        ]

    def refs(self) -> dict[str, ObjectId]:
        return dict(sorted(self._refs.items()))

    def stats(self) -> TraceStats:
        return TraceStats(
            object_count=len(self._objects),
            total_bytes=sum(
                len(canonical_json(object_payload(obj)).encode("utf-8"))
                for obj in self._objects.values()
            ),
        )
