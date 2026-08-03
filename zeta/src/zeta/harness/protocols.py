"""Runtime store interfaces owned by the harness.

Zeta separates durable historical facts from live coordination state, even
though one database holds both. These protocols name that separation: the
journal is append-only truth, and the coordination store is ephemeral queue
ownership that a projection rebuild discards.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from zeta.events import DraftEvent, Event
from zeta.harness.metrics import MetricAttribute
from zeta.journal.store import Filter
from zeta.journal.types import AppendOutcome


@runtime_checkable
class RuntimeJournal(Protocol):
    """Append-only historical truth used by orchestration components."""

    def accept(self, draft: DraftEvent) -> AppendOutcome: ...

    def append(self, event: Event) -> AppendOutcome: ...

    def get(self, event_id: str) -> Event | None: ...

    def list_events(self, filter: Filter) -> list[Event]: ...


@runtime_checkable
class CoordinationStore(Protocol):
    """Ephemeral queue ownership, leases, heartbeats, and mutual exclusion."""

    def queue_item(self, queue_item_id: str) -> dict[str, Any] | None: ...

    def queue_item_attempt_count(self, queue_item_id: str) -> int: ...

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
