"""Run cancellation and abort signaling."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from zeta.run.outcomes import AgentRunResult


class AgentRunAborted(RuntimeError):
    """Raised when a cooperative run budget or cancellation request aborts."""

    def __init__(
        self,
        reason: str,
        *,
        result: AgentRunResult,
        event_recorded: bool,
    ) -> None:
        super().__init__(reason.replace("_", " "))
        self.reason = reason
        self.result = result
        self.event_recorded = event_recorded


class CancellationToken(Protocol):
    def is_set(self) -> bool: ...


class AbortReason(Protocol):
    def __call__(self, *, check_deadline: bool = True) -> str | None: ...


def agent_deadline(
    max_wall_seconds: float | None,
    deadline: float | None,
    *,
    clock: Callable[[], float],
) -> float | None:
    if max_wall_seconds is None:
        return deadline
    configured = clock() + max(max_wall_seconds, 0.0)
    if deadline is None:
        return configured
    return min(deadline, configured)


def agent_abort_reason(
    cancellation_event: CancellationToken | None,
    deadline: float | None,
    *,
    clock: Callable[[], float],
) -> str | None:
    if cancellation_event is not None and cancellation_event.is_set():
        return "cancelled"
    if deadline is not None and clock() >= deadline:
        return "deadline_exceeded"
    return None


def run_abort_reason(
    cancellation_event: CancellationToken | None,
    deadline: float | None,
    *,
    clock: Callable[[], float],
) -> AbortReason:
    def current_abort_reason(*, check_deadline: bool = True) -> str | None:
        return agent_abort_reason(
            cancellation_event,
            deadline if check_deadline else None,
            clock=clock,
        )

    return current_abort_reason
