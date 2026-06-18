"""Substrate store protocols and in-memory implementation."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Any, Protocol, cast

from .derivation import Derivation
from .object import (
    Object,
    ObjectId,
)
from .ref import (
    Ref,
    RefUpdate,
)

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


@dataclass(frozen=True)
class TraceStats:
    """Basic trace store size statistics."""

    object_count: int
    total_bytes: int


def escape_like(text: str) -> str:
    """Escape SQLite LIKE wildcards so they match literally."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def canonical_json(value: Any) -> str:
    """Serialize JSON data deterministically for content hashing."""
    return json.dumps(
        normalize_json(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def normalize_json(value: Any) -> Any:
    """Normalize Python-native JSON values before deterministic serialization."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, tuple | list):
        return [normalize_json(item) for item in value]
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("canonical JSON object keys must be strings")
            normalized[key] = normalize_json(item)
        return normalized
    raise TypeError(f"value is not JSON serializable: {type(value).__name__}")


class Store(Protocol):
    """Storage API shared by in-memory and SQLite stores."""

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
        object_id_value = obj.content_address()
        self._objects.setdefault(object_id_value, obj)
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

    def move_ref(
        self,
        name: str,
        expected: ObjectId | None,
        new: ObjectId,
    ) -> RefUpdate:
        old_object_id = self._refs.get(name)
        updated = old_object_id == expected
        if updated:
            self._refs[name] = new
        return RefUpdate(
            name=name,
            old_object_id=old_object_id,
            new_object_id=new,
            updated=updated,
        )

    def get_ref(self, name: str) -> Ref | None:
        object_id_value = self._refs.get(name)
        if object_id_value is None:
            return None
        return Ref(name=name, object_id=object_id_value)

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

    def refs(self) -> list[Ref]:
        return [
            Ref(name=name, object_id=object_id_value)
            for name, object_id_value in sorted(self._refs.items())
        ]

    def stats(self) -> TraceStats:
        return TraceStats(
            object_count=len(self._objects),
            total_bytes=sum(
                len(
                    canonical_json(
                        {
                            "kind": obj.kind,
                            "schema": obj.schema,
                            "data": obj.data,
                            "links": list(obj.links),
                        }
                    ).encode("utf-8")
                )
                for obj in self._objects.values()
            ),
        )
