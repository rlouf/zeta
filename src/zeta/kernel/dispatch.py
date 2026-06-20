"""Dispatch queue item and attempt domain shapes."""

from dataclasses import dataclass
from typing import Literal

QueueItemId = str
AttemptId = str

QueueItemStatus = Literal[
    "available",
    "claimed",
    "completed",
    "failed",
    "cancelled",
    "retry_scheduled",
    "unhandled",
]

AttemptStatus = Literal[
    "running",
    "completed",
    "failed",
    "cancelled",
]


@dataclass(frozen=True)
class QueueItem:
    """A routed event that should be processed by one target agent."""

    queue_item_id: QueueItemId
    event_id: str
    target_agent: str
    status: QueueItemStatus


@dataclass(frozen=True)
class Attempt:
    """One worker try at processing one queue item."""

    attempt_id: AttemptId
    queue_item_id: QueueItemId
    event_id: str
    attempt_number: int
    target_agent: str
    status: AttemptStatus
    started_at: str
    finished_at: str | None = None
    error: str | None = None
    session_id: str | None = None


__all__ = [
    "Attempt",
    "AttemptId",
    "AttemptStatus",
    "QueueItem",
    "QueueItemId",
    "QueueItemStatus",
]
