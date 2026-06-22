"""Dispatch attempt domain shapes."""

from dataclasses import dataclass
from typing import Literal

AttemptId = str

AttemptStatus = Literal[
    "running",
    "completed",
    "failed",
    "cancelled",
]


@dataclass(frozen=True)
class Attempt:
    """One execution try for a queue item.

    Attempts carry session and run context so lifecycle events can be queried
    without decoding the triggering event, while retries remain tied to the
    same queue item through `attempt_number`.
    """

    attempt_id: AttemptId
    queue_item_id: str
    event_id: str
    attempt_number: int
    target_agent: str
    status: AttemptStatus
    started_at: str
    finished_at: str | None = None
    error: str | None = None
    session_id: str | None = None
    run_id: str | None = None
