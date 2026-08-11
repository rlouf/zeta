"""External encoding for durable events.

IPC peers and other external readers exchange events as JSON objects. These
helpers convert between that object and the durable `Event` record.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from zeta.events import Event, json_native_payload


def event_to_wire(event: Event) -> dict[str, Any]:
    """Serialize a durable event for IPC and other external readers."""

    return {
        "id": event.id,
        "type": event.event_type,
        "source": event.source,
        "payload": json_native_payload(event.payload),
        "idempotency_key": event.idempotency_key,
        "caused_by": event.caused_by,
        "session_id": event.session_id,
        "run_id": event.run_id,
        "turn_id": event.turn_id,
        "timestamp_ms": event.timestamp_ms,
        "cursor": event.cursor,
    }


def event_from_wire(value: Mapping[str, Any]) -> Event:
    """Parse the authoritative external durable-event object."""

    payload = value.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("payload must be an object")
    return Event(
        id=required_wire_string(value, "id"),
        event_type=required_wire_string(value, "type"),
        source=required_wire_string(value, "source"),
        payload=json_native_payload(payload),
        idempotency_key=optional_wire_string(value, "idempotency_key"),
        caused_by=optional_wire_string(value, "caused_by"),
        session_id=optional_wire_string(value, "session_id"),
        run_id=optional_wire_string(value, "run_id"),
        turn_id=optional_wire_string(value, "turn_id"),
        timestamp_ms=required_wire_int(value, "timestamp_ms"),
        cursor=optional_wire_int(value, "cursor"),
    )


def required_wire_string(value: Mapping[str, Any], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item:
        raise ValueError(f"{field} must be a non-empty string")
    return item


def optional_wire_string(value: Mapping[str, Any], field: str) -> str | None:
    item = value.get(field)
    if item is None:
        return None
    if not isinstance(item, str) or not item:
        raise ValueError(f"{field} must be null or a non-empty string")
    return item


def required_wire_int(value: Mapping[str, Any], field: str) -> int:
    item = value.get(field)
    if not isinstance(item, int) or isinstance(item, bool):
        raise ValueError(f"{field} must be an integer")
    return item


def optional_wire_int(value: Mapping[str, Any], field: str) -> int | None:
    item = value.get(field)
    if item is None:
        return None
    if not isinstance(item, int) or isinstance(item, bool):
        raise ValueError(f"{field} must be null or an integer")
    return item
