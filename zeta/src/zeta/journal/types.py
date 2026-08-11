"""Durable record vocabulary and the producer sink protocol.

Events are the append-only record of runtime activity. Producers submit drafts
through an event sink, and stores assign durable ordering. This module holds
the constants that classify those records and the protocol that accepts them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from zeta import addresses
from zeta.events import DraftEvent, Event

TURN_EVENT_COMPLETED = "zeta.turn.completed"
TURN_EVENT_FAILED = "zeta.turn.failed"
EVENT_IDEMPOTENT_TYPES = frozenset(
    {
        "zeta.model_call.completed",
        "zeta.tool_call.started",
        "zeta.tool_call.completed",
        "zeta.tool_call.failed",
        "zeta.user_message",
    }
)
TURN_IDEMPOTENT_TYPES = frozenset(
    {
        "zeta.prompt.submitted",
        TURN_EVENT_COMPLETED,
        TURN_EVENT_FAILED,
    }
)
RUNTIME_DURABLE_EXCLUDED_KEYS = {
    "id",
    "type",
    "time",
    "session",
    "source",
    "caused_by",
}
REFUSED_TOOL_ERROR_CODES = {
    "direct-execution-disallowed",
    "disallowed-tool",
    "invalid-json-args",
    "invalid-tool-call",
    "schema-mismatch",
    "staging-unsupported",
    "unknown-tool",
}


@dataclass(frozen=True)
class AppendOutcome:
    """Append result that preserves idempotent producer semantics.

    Stores return the existing event on duplicate input so callers can treat
    retries as successful acknowledgements without guessing whether persistence
    happened.
    """

    event: Event
    inserted: bool


@dataclass(frozen=True)
class JournalEntry:
    """Carries one event together with its backend-independent identity proof."""

    event: Event
    payload_bytes: bytes
    payload_address: str
    previous_address: str | None
    entry_address: str


@dataclass(frozen=True)
class VerificationReport:
    """Reports the retained head so callers can compare an external anchor."""

    entries_checked: int
    head_address: str | None


class VerificationReason(StrEnum):
    """Names stable journal-v0 divergence classes."""

    CURSOR_ORDER = "cursor_order"
    DUPLICATE_ID = "duplicate_id"
    DUPLICATE_IDEMPOTENCY_KEY = "duplicate_idempotency_key"
    PAYLOAD_ENCODING = "payload_encoding"
    PAYLOAD_ADDRESS = "payload_address"
    PREVIOUS_ADDRESS = "previous_address"
    ENTRY_ADDRESS = "entry_address"
    EXPECTED_HEAD = "expected_head"


class VerificationError(ValueError):
    """Preserves the first divergence as machine-readable review evidence."""

    def __init__(
        self,
        reason: VerificationReason,
        *,
        event_id: str | None,
        cursor: int | None,
        entries_checked: int,
    ) -> None:
        self.reason = reason
        self.event_id = event_id
        self.cursor = cursor
        self.entries_checked = entries_checked
        location = event_id if event_id is not None else "journal head"
        super().__init__(f"{reason.value} at {location}")


def validate_event(event: Event) -> None:
    """Reject fields that cannot form the language-neutral journal identity."""
    for name, value in (
        ("id", event.id),
        ("type", event.event_type),
        ("source", event.source),
    ):
        if not isinstance(value, str) or not value:
            raise ValueError(f"event {name} must be a non-empty string")
    if (
        isinstance(event.timestamp_ms, bool)
        or not isinstance(event.timestamp_ms, int)
        or not -(2**63) <= event.timestamp_ms <= 2**63 - 1
    ):
        raise ValueError("event timestamp_ms must fit i64")
    for name, value in (
        ("idempotency_key", event.idempotency_key),
        ("caused_by", event.caused_by),
        ("session_id", event.session_id),
        ("run_id", event.run_id),
        ("turn_id", event.turn_id),
    ):
        if value is not None and not isinstance(value, str):
            raise ValueError(f"event {name} must be a string or null")


def canonical_payload_bytes(payload: Mapping[str, Any]) -> bytes:
    """Return the exact content bytes shared by every storage backend."""
    if not isinstance(payload, Mapping):
        raise TypeError("event payload must be a mapping")
    return addresses.canonical_json_bytes(dict(payload))


def canonical_chain_bytes(
    event: Event,
    payload_address: str,
    previous_address: str | None,
) -> bytes:
    """Return the versioned bytes that commit every semantic event field."""
    validate_event(event)
    value = [
        0,
        event.id,
        event.event_type,
        event.source,
        payload_address,
        event.idempotency_key,
        event.caused_by,
        event.session_id,
        event.run_id,
        event.turn_id,
        event.timestamp_ms,
        previous_address,
    ]
    return addresses.canonical_json_bytes(value)


def journal_entry(event: Event, previous_address: str | None) -> JournalEntry:
    """Mint one complete logical entry without choosing its physical storage."""
    validate_event(event)
    payload_bytes = canonical_payload_bytes(event.payload)
    payload_address = addresses.content_address(payload_bytes)
    chain_bytes = canonical_chain_bytes(event, payload_address, previous_address)
    return JournalEntry(
        event=event,
        payload_bytes=payload_bytes,
        payload_address=payload_address,
        previous_address=previous_address,
        entry_address=addresses.chain_address(chain_bytes),
    )


def verify_entries(
    entries: Iterable[JournalEntry],
    *,
    expected_head: str | None = None,
) -> VerificationReport:
    """Verify a retained chain while distinguishing it from an external anchor."""
    previous_address = None
    previous_cursor = None
    seen_ids: set[str] = set()
    seen_idempotency_keys: set[str] = set()
    entries_checked = 0
    last_entry = None
    for entry in entries:
        event = entry.event
        cursor = event.cursor
        if (
            isinstance(cursor, bool)
            or not isinstance(cursor, int)
            or cursor <= 0
            or (previous_cursor is not None and cursor <= previous_cursor)
        ):
            raise _verification_error(
                VerificationReason.CURSOR_ORDER,
                entry,
                entries_checked,
            )
        if event.id in seen_ids:
            raise _verification_error(
                VerificationReason.DUPLICATE_ID,
                entry,
                entries_checked,
            )
        if (
            event.idempotency_key is not None
            and event.idempotency_key in seen_idempotency_keys
        ):
            raise _verification_error(
                VerificationReason.DUPLICATE_IDEMPOTENCY_KEY,
                entry,
                entries_checked,
            )
        try:
            payload_bytes = canonical_payload_bytes(event.payload)
        except (TypeError, ValueError) as error:
            raise _verification_error(
                VerificationReason.PAYLOAD_ENCODING,
                entry,
                entries_checked,
            ) from error
        if entry.payload_bytes != payload_bytes:
            raise _verification_error(
                VerificationReason.PAYLOAD_ENCODING,
                entry,
                entries_checked,
            )
        payload_address = addresses.content_address(payload_bytes)
        if entry.payload_address != payload_address:
            raise _verification_error(
                VerificationReason.PAYLOAD_ADDRESS,
                entry,
                entries_checked,
            )
        if entry.previous_address != previous_address:
            raise _verification_error(
                VerificationReason.PREVIOUS_ADDRESS,
                entry,
                entries_checked,
            )
        try:
            chain_bytes = canonical_chain_bytes(
                event, payload_address, previous_address
            )
        except (TypeError, ValueError) as error:
            raise _verification_error(
                VerificationReason.ENTRY_ADDRESS,
                entry,
                entries_checked,
            ) from error
        entry_address = addresses.chain_address(chain_bytes)
        if entry.entry_address != entry_address:
            raise _verification_error(
                VerificationReason.ENTRY_ADDRESS,
                entry,
                entries_checked,
            )
        seen_ids.add(event.id)
        if event.idempotency_key is not None:
            seen_idempotency_keys.add(event.idempotency_key)
        entries_checked += 1
        previous_cursor = cursor
        previous_address = entry_address
        last_entry = entry
    if expected_head is not None and previous_address != expected_head:
        raise VerificationError(
            VerificationReason.EXPECTED_HEAD,
            event_id=last_entry.event.id if last_entry is not None else None,
            cursor=last_entry.event.cursor if last_entry is not None else None,
            entries_checked=entries_checked,
        )
    return VerificationReport(
        entries_checked=entries_checked,
        head_address=previous_address,
    )


def _verification_error(
    reason: VerificationReason,
    entry: JournalEntry,
    entries_checked: int,
) -> VerificationError:
    return VerificationError(
        reason,
        event_id=entry.event.id,
        cursor=entry.event.cursor,
        entries_checked=entries_checked,
    )


class EventSink(Protocol):
    """Accepts draft events from runtime producers."""

    def accept(self, draft: DraftEvent) -> AppendOutcome:
        """Accept one draft event and return the durable append outcome."""


def publish_event(draft: DraftEvent, *, sink: EventSink) -> AppendOutcome:
    return sink.accept(draft)
