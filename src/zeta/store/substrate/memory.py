"""In-memory substrate store for tests and ephemeral traces."""

from collections.abc import Iterator
from contextlib import contextmanager

from zeta.store.substrate.base import StoreBase, TraceStats, canonical_json
from zeta.substrate import Derivation, Object, ObjectId, Ref, RefUpdate


class InMemoryStore(StoreBase):
    """Process-local store with the same substrate semantics as SQLite.

    Objects and derivations are kept in dictionaries, and refs are mutable
    process-local pointers. This store is useful for tests and short-lived
    traces where durability is not required.
    """

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
