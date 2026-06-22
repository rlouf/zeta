"""Dispatch queue item domain shapes."""

from dataclasses import dataclass
from typing import Literal

QueueItemId = str

QueueItemStatus = Literal[
    "pending",
    "available",
    "claimed",
    "completed",
    "failed",
    "cancelled",
    "retry_scheduled",
    "unhandled",
]


@dataclass(frozen=True)
class QueueItem:
    """The durable assignment of one event to one target agent.

    Queue items describe routing state, not execution. Runtime lifecycle events
    record their status changes while the original event envelope carries
    session and run correlation.
    """

    queue_item_id: QueueItemId
    event_id: str
    target_agent: str
    status: QueueItemStatus
