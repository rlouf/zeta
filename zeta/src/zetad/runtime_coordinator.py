"""Canonical claimed-queue execution for local and daemon workers."""

from __future__ import annotations

import time
from dataclasses import dataclass

from zeta.records.events import Event

from zetad.agents import ExecutableAgent
from zetad.dispatch import QueueingDispatcher
from zetad.retry import RetryPolicy
from zetad.store import QueueClaim, RuntimeEventStore


@dataclass(frozen=True)
class CoordinatedRun:
    queue_item_id: str
    lifecycle_events: list[Event]


class RuntimeCoordinator:
    """Claim, lock, and execute work through one durable attempt coordinator."""

    def __init__(
        self,
        events: RuntimeEventStore,
        executors: tuple[ExecutableAgent, ...],
        *,
        worker_name: str,
        lease_ms: int,
        heartbeat_interval_seconds: float,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.events = events
        self.executors = executors
        self.worker_name = worker_name
        self.lease_ms = lease_ms
        self.dispatcher = QueueingDispatcher(
            events.journal,
            events.coordination,
            executors=executors,
            worker_name=worker_name,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            lease_ms=lease_ms,
            retry_policy=retry_policy,
        )

    async def run_next(
        self,
        *,
        skipped_queue_items: set[str] | None = None,
    ) -> CoordinatedRun | None:
        skipped = skipped_queue_items or set()
        while True:
            claim = self.claim_next(skipped_queue_items=skipped)
            if claim is None:
                return None
            lock_keys = queue_item_lock_keys(
                self.events,
                self.executors,
                claim.queue_item_id,
            )
            owner = claim.token
            now_ms = runtime_time_ms()
            if not self.events.acquire_locks(
                lock_keys,
                owner,
                lease_ms=self.lease_ms,
                now_ms=now_ms,
            ):
                self.events.release_queue_claim(
                    claim.queue_item_id,
                    self.worker_name,
                    claim_token=claim.token,
                    now_ms=now_ms,
                )
                skipped.add(claim.queue_item_id)
                continue
            self.dispatcher.claim_token = claim.token
            try:
                lifecycle_events = await self.dispatcher.run_queue_item(
                    claim.queue_item_id
                )
                return CoordinatedRun(claim.queue_item_id, lifecycle_events)
            finally:
                self.events.release_locks(lock_keys, owner)

    def claim_next(
        self,
        *,
        skipped_queue_items: set[str],
    ) -> QueueClaim | None:
        now_ms = runtime_time_ms()
        self.events.reconcile_expired_queue_claims(now_ms=now_ms)
        self.events.reconcile_expired_locks(now_ms=now_ms)
        return self.events.claim_next_queue_item(
            self.worker_name,
            lease_ms=self.lease_ms,
            now_ms=now_ms,
            exclude_queue_item_ids=skipped_queue_items,
        )


def queue_item_lock_keys(
    events: RuntimeEventStore,
    executors: tuple[ExecutableAgent, ...],
    queue_item_id: str,
) -> tuple[str, ...]:
    row = events.queue_item(queue_item_id)
    if row is None:
        return ()
    target_agent = str(row["target_agent"])
    if target_agent:
        return agent_lock_keys(executors, target_agent)
    event = events.get(str(row["event_id"]))
    if event is None:
        return ()
    matching_executors = [
        agent for agent in executors if agent.definition.accepts(event)
    ]
    if len(matching_executors) != 1:
        return ()
    return matching_executors[0].definition.lock_keys


def agent_lock_keys(
    executors: tuple[ExecutableAgent, ...],
    agent_id: str,
) -> tuple[str, ...]:
    for agent in executors:
        if agent.definition.agent_id == agent_id:
            return agent.definition.lock_keys
    return ()


def runtime_time_ms() -> int:
    return time.time_ns() // 1_000_000
