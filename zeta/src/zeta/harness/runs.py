"""Run domain shapes."""

from dataclasses import dataclass
from typing import Literal

RunId = str

RunStatus = Literal[
    "starting",
    "running",
    "cancelling",
    "completed",
    "failed",
    "cancelled",
]


@dataclass(frozen=True)
class Run:
    """A runtime operation used to control and correlate work.

    A run is the durable operation handle for cancellation, status, and event
    filtering. Runtime-owned details such as tasks and cancellation tokens stay
    outside the kernel so this shape remains serializable state.
    """

    run_id: RunId
    status: RunStatus
    session_id: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
