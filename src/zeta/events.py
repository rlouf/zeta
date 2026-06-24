"""Core event vocabulary shared across Zeta modules."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import KW_ONLY, dataclass
from typing import Any
from uuid import uuid4


@dataclass(frozen=True)
class DraftEvent:
    """A producer-authored event before the store assigns durable identity."""

    event_type: str
    source: str
    payload: Mapping[str, Any]
    idempotency_key: str | None = None
    caused_by: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    turn_id: str | None = None


@dataclass(frozen=True)
class Event:
    """A durable fact in the append-only runtime event log."""

    id: str
    event_type: str
    source: str
    payload: Mapping[str, Any]
    idempotency_key: str | None
    caused_by: str | None
    session_id: str | None
    timestamp_ms: int
    _: KW_ONLY
    turn_id: str | None = None
    run_id: str | None = None
    cursor: int | None = None

    @classmethod
    def from_draft(cls, draft: DraftEvent) -> Event:
        idempotency_key = (
            draft.idempotency_key.strip() or None
            if draft.idempotency_key is not None
            else None
        )
        return cls(
            id=f"evt_{uuid4().hex}",
            event_type=draft.event_type,
            source=draft.source,
            payload=json_native_payload(draft.payload),
            idempotency_key=idempotency_key,
            caused_by=draft.caused_by,
            session_id=draft.session_id,
            run_id=draft.run_id,
            turn_id=draft.turn_id,
            timestamp_ms=time.time_ns() // 1_000_000,
        )


def json_native_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(
        json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":"))
    )
