"""Producer-facing event sink protocol.

Sinks are the narrow boundary for code that emits runtime facts. Producers only
need to submit drafts; storage, idempotency, and ordering remain store concerns.
"""

from __future__ import annotations

from typing import Protocol

from .event import AppendOutcome, DraftEvent


class EventSink(Protocol):
    """Accepts draft events from runtime producers."""

    def accept(self, draft: DraftEvent) -> AppendOutcome:
        """Accept one draft event and return the durable append outcome."""


def publish_event(draft: DraftEvent, *, sink: EventSink) -> AppendOutcome:
    return sink.accept(draft)
