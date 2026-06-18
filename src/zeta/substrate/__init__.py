"""Core content-addressed substrate for Zeta.

The substrate separates three concerns that are easy to blur in agent
systems:

* `Object` stores immutable values by content.
* `RefName` gives mutable names to the latest object for a logical source.
* `Derivation` records how one object was built from other objects.

This split gives Zeta build-system-like behavior without making prompt
assembly or model calls special. A context sent to a model is just an object.
A model output can be stored as an object. A generated file can be stored as
an object. Refs connect those immutable values to moving names such as
`session/s1/head` or `file/REFERENCES.md`.

Object identity is based on canonical JSON. The store hashes an envelope
containing `kind`, `schema`, `data`, and structural `links`; operational facts
such as timestamps, retries, latency, or worker identity do not belong in an
object. If those facts matter, record them outside the value plane.
"""

from __future__ import annotations

from .derivation import Derivation
from .object import (
    Object,
    ObjectId,
)
from .ref import (
    Ref,
    RefName,
    RefUpdate,
)
from .store import (
    DEFAULT_SQLITE_NAME,
    ZETA_SQLITE_NAME,
    AmbiguousIdError,
    InMemoryStore,
    SqliteStore,
    Store,
    StoreBase,
    TraceStats,
    UnknownIdError,
    UnknownSessionError,
    available_session_ids,
    canonical_json,
    default_sqlite_path,
    escape_like,
    export_trace_refs,
    import_trace_graph,
    normalize_json,
    open_existing_trace_store,
    open_trace_store,
    resolve_object_id,
    trace_state_dir,
    warn_trace_failure_once,
    zeta_sqlite_path,
)

__all__ = [
    "AmbiguousIdError",
    "DEFAULT_SQLITE_NAME",
    "Derivation",
    "InMemoryStore",
    "Object",
    "ObjectId",
    "Ref",
    "RefName",
    "RefUpdate",
    "SqliteStore",
    "Store",
    "StoreBase",
    "TraceStats",
    "UnknownIdError",
    "UnknownSessionError",
    "ZETA_SQLITE_NAME",
    "available_session_ids",
    "canonical_json",
    "default_sqlite_path",
    "escape_like",
    "export_trace_refs",
    "import_trace_graph",
    "normalize_json",
    "open_existing_trace_store",
    "open_trace_store",
    "resolve_object_id",
    "trace_state_dir",
    "warn_trace_failure_once",
    "zeta_sqlite_path",
]
