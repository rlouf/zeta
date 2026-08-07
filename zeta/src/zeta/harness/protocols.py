"""Runtime store interfaces owned by the harness.

Zeta separates durable historical facts from live coordination state, even
though one database holds both. These protocols name that separation: the
journal is append-only truth, and the coordination store is ephemeral queue
ownership that a projection rebuild discards.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from zeta.events import DraftEvent, Event
from zeta.harness.metrics import MetricAttribute
from zeta.journal.store import Filter
from zeta.journal.types import AppendOutcome

CancellationResourceType = Literal["wait", "scheduled_event"]
CancellationStatus = Literal["cancelled", "matched", "timed_out", "published"]
QueueItemCancellationStatus = Literal[
    "cancelling",
    "cancelled",
    "already_cancelled",
    "already_terminal",
    "unknown",
]
QueueItemTerminalStatus = Literal[
    "completed",
    "failed",
    "cancelled",
    "dead_lettered",
    "unhandled",
]


class CancellationError(ValueError):
    """A cancellation request cannot be applied safely."""

    dispatch_error_code = "malformed_event_payload"


class InvalidCancellationHandle(CancellationError):
    """The handle does not identify a cancellable resource type."""


class UnknownCancellationHandle(CancellationError):
    """No cancellable resource has this handle."""


class UnauthorizedCancellation(CancellationError):
    """The authored session does not own this resource."""


@dataclass(frozen=True)
class CancellationResult:
    """The current terminal state after a cancellation request."""

    handle: str
    resource_type: CancellationResourceType
    status: CancellationStatus
    changed: bool
    event: Event | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class QueueItemCancellationResult:
    """The durable state of one turn after a cancellation request."""

    queue_item_id: str
    run_id: str | None
    session_id: str | None
    status: QueueItemCancellationStatus
    changed: bool
    terminal_status: QueueItemTerminalStatus | None = None
    event: Event | None = field(default=None, compare=False, repr=False)


@runtime_checkable
class RuntimeJournal(Protocol):
    """Append-only historical truth used by orchestration components."""

    def accept(self, draft: DraftEvent) -> AppendOutcome: ...

    def append(self, event: Event) -> AppendOutcome: ...

    def get(self, event_id: str) -> Event | None: ...

    def list_events(self, filter: Filter) -> list[Event]: ...

    def cancel_resource(
        self,
        handle: str,
        *,
        reason: str | None = None,
        source_agent_id: str | None = None,
        source_session_id: str | None = None,
        now_ms: int | None = None,
    ) -> CancellationResult: ...

    def cancel_queue_item(
        self,
        queue_item_id: str,
        *,
        expected_session_id: str | None = None,
        reason: str | None = None,
        now_ms: int | None = None,
    ) -> QueueItemCancellationResult: ...


@runtime_checkable
class CoordinationStore(Protocol):
    """Ephemeral queue ownership, leases, heartbeats, and mutual exclusion."""

    def queue_item(self, queue_item_id: str) -> dict[str, Any] | None: ...

    def queue_item_attempt_count(self, queue_item_id: str) -> int: ...

    def queue_item_cancellation_requested(self, queue_item_id: str) -> bool: ...

    def queue_claim_is_current(
        self,
        queue_item_id: str,
        worker_name: str,
        claim_token: str,
        *,
        now_ms: int,
    ) -> bool: ...

    def heartbeat_attempt(
        self,
        attempt_id: str,
        queue_item_id: str,
        worker_name: str,
        *,
        claim_token: str,
        lease_ms: int,
        now_ms: int,
    ) -> bool: ...

    def renew_locks(
        self,
        keys: Iterable[str],
        owner: str,
        *,
        lease_ms: int,
        now_ms: int,
    ) -> bool: ...

    def observe_runtime_metric(
        self,
        name: str,
        value: float,
        **attributes: MetricAttribute,
    ) -> None: ...


@dataclass(frozen=True)
class QueueClaim:
    """Opaque ownership token for one active queue claim."""

    queue_item_id: str
    token: str
