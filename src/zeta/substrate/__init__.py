"""Content-addressed object graph substrate for Zeta."""

from __future__ import annotations

from .derivation import Derivation
from .links import (
    add_event_link,
    add_object_link,
    add_object_links,
    durable_event_object_links,
    trace_object_id,
)
from .object import (
    Object,
    ObjectId,
    PromptTrace,
    TraceStats,
    canonical_json,
    escape_like,
    latest_prompt_trace_fields,
    normalize_json,
    normalize_object,
    object_id,
    object_payload,
    prompt_trace_payload,
)
from .refs import (
    REF_EXPECTED_UNSET,
    AmbiguousIdError,
    RefConflictError,
    UnknownIdError,
    UnknownSessionError,
)
from .sqlite import (
    DEFAULT_SQLITE_NAME,
    ZETA_SQLITE_NAME,
    SqliteStore,
    available_session_ids,
    default_sqlite_path,
    export_trace_refs,
    import_trace_graph,
    open_existing_trace_store,
    open_trace_store,
    trace_state_dir,
    zeta_sqlite_path,
)
from .store import (
    InMemoryStore,
    Store,
    StoreBase,
    resolve_object_id,
    warn_trace_failure_once,
)

_REF_EXPECTED_UNSET = REF_EXPECTED_UNSET

__all__ = [
    "AmbiguousIdError",
    "DEFAULT_SQLITE_NAME",
    "Derivation",
    "InMemoryStore",
    "Object",
    "ObjectId",
    "PromptTrace",
    "REF_EXPECTED_UNSET",
    "RefConflictError",
    "SqliteStore",
    "Store",
    "StoreBase",
    "TraceStats",
    "UnknownIdError",
    "UnknownSessionError",
    "ZETA_SQLITE_NAME",
    "_REF_EXPECTED_UNSET",
    "add_event_link",
    "add_object_link",
    "add_object_links",
    "available_session_ids",
    "canonical_json",
    "default_sqlite_path",
    "durable_event_object_links",
    "escape_like",
    "export_trace_refs",
    "import_trace_graph",
    "latest_prompt_trace_fields",
    "normalize_json",
    "normalize_object",
    "object_id",
    "object_payload",
    "open_existing_trace_store",
    "open_trace_store",
    "prompt_trace_payload",
    "resolve_object_id",
    "trace_state_dir",
    "trace_object_id",
    "warn_trace_failure_once",
    "zeta_sqlite_path",
]
