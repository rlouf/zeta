"""Content-addressed substrate objects."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
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


@dataclass(frozen=True)
class PromptTrace:
    """Trace ids for one prompt request and its assistant response.

    Component ids ride on the prompt object's links, not here: carrying
    them in every event payload grew the store quadratically with turns.
    """

    prompt_object_id: ObjectId
    assistant_message_object_id: ObjectId | None = None


@dataclass(frozen=True)
class TraceStats:
    """Basic trace store size statistics."""

    object_count: int
    total_bytes: int


def prompt_trace_payload(trace: PromptTrace) -> dict[str, Any]:
    """Return JSON metadata for a prompt trace."""
    payload: dict[str, Any] = {"prompt_object_id": trace.prompt_object_id}
    if trace.assistant_message_object_id is not None:
        payload["assistant_message_object_id"] = trace.assistant_message_object_id
    return payload


def latest_prompt_trace_fields(prompt_traces: Sequence[Any]) -> dict[str, Any]:
    """Return event fields for the most recent valid prompt trace."""
    if not prompt_traces:
        return {}
    trace = prompt_traces[-1]
    if not isinstance(trace, PromptTrace):
        return {}
    return {"prompt_trace": prompt_trace_payload(trace)}


def object_payload(obj: Object) -> dict[str, Any]:
    """Return the canonical payload that is hashed and stored."""
    return {
        "kind": obj.kind,
        "schema": obj.schema,
        "data": obj.data,
        "links": list(obj.links),
    }


def object_id(obj: Object) -> ObjectId:
    """Return the deterministic content address for an object."""
    digest = hashlib.sha256(canonical_json(object_payload(obj)).encode()).hexdigest()
    return f"sha256:{digest}"


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


def normalize_object(obj: Object) -> Object:
    """Return an object with normalized data and link containers."""
    return Object(
        kind=obj.kind,
        schema=obj.schema,
        data=normalize_json(obj.data),
        links=tuple(str(link) for link in obj.links),
    )
