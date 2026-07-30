"""Legal runtime queue and attempt state transitions."""

from __future__ import annotations

from dataclasses import dataclass

from zeta.harness.attempts import AttemptStatus
from zeta.harness.queue import QueueItemStatus


@dataclass(frozen=True)
class InvalidRuntimeTransition(ValueError):
    """Raised when a runtime state machine receives an illegal transition."""

    resource: str
    previous: str | None
    current: str

    def __post_init__(self) -> None:
        super().__init__(
            f"invalid {self.resource} transition: {self.previous!r} -> {self.current!r}"
        )


QUEUE_TRANSITIONS: dict[QueueItemStatus | None, frozenset[QueueItemStatus]] = {
    None: frozenset({"pending", "available", "unhandled"}),
    "pending": frozenset({"available", "claimed", "unhandled"}),
    "available": frozenset({"claimed", "cancelled"}),
    "claimed": frozenset(
        {"available", "completed", "failed", "cancelled", "dead_lettered"}
    ),
    "retry_scheduled": frozenset({"available", "cancelled"}),
    "failed": frozenset({"retry_scheduled", "dead_lettered"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
    "dead_lettered": frozenset(),
    "unhandled": frozenset(),
}

ATTEMPT_TRANSITIONS: dict[AttemptStatus | None, frozenset[AttemptStatus]] = {
    None: frozenset({"running"}),
    "running": frozenset({"completed", "failed", "cancelled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}


def validate_queue_transition(
    previous: QueueItemStatus | None,
    current: QueueItemStatus,
) -> None:
    if current not in QUEUE_TRANSITIONS[previous]:
        raise InvalidRuntimeTransition("queue item", previous, current)


def validate_attempt_transition(
    previous: AttemptStatus | None,
    current: AttemptStatus,
) -> None:
    if current not in ATTEMPT_TRANSITIONS[previous]:
        raise InvalidRuntimeTransition("attempt", previous, current)
