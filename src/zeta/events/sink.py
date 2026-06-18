"""Event sink protocol."""

from __future__ import annotations

from typing import Protocol

from .event import AppendOutcome, DraftEvent


class EventSink(Protocol):
    """Consumer of draft events."""

    def accept(self, draft: DraftEvent) -> AppendOutcome:
        """Accept one draft event."""


def publish_event(draft: DraftEvent, *, sink: EventSink) -> AppendOutcome:
    return sink.accept(draft)
