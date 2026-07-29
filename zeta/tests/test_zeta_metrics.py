from __future__ import annotations

import asyncio
from pathlib import Path

from zeta.records.events import DraftEvent, Event
from zetad.agents import (
    AgentDefinition,
    AgentInvocation,
    EventPattern,
    ExecutableAgent,
)
from zetad.dispatch import QueueingDispatcher
from zetad.metrics import InMemoryRuntimeMetrics
from zetad.retry import RetryPolicy
from zetad.store import RuntimeEventStore


async def dispatch_and_drain(
    dispatcher: QueueingDispatcher,
    draft: DraftEvent,
) -> list[Event]:
    await dispatcher.publish_event(draft)
    return await dispatcher.drain()


def test_runtime_store_records_coordination_health(tmp_path: Path) -> None:
    metrics = InMemoryRuntimeMetrics()
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3", metrics=metrics)
    accepted = store.accept(DraftEvent("github.issue.opened", "github", {})).event
    now_ms = accepted.timestamp_ms + 100

    claim = store.claim_next_queue_item("worker-a", lease_ms=1_000, now_ms=now_ms)
    assert claim is not None
    assert store.acquire_locks(
        ["repo:main"],
        claim.token,
        lease_ms=1_000,
        now_ms=now_ms,
    )
    assert not store.acquire_locks(
        ["repo:main"],
        "worker-b",
        lease_ms=1_000,
        now_ms=now_ms,
    )

    attempt_id = f"att_{claim.queue_item_id}_1"
    store.append(
        Event(
            id="attempt-started",
            event_type="runtime.attempt.started",
            source="zeta",
            payload={
                "attempt_id": attempt_id,
                "queue_item_id": claim.queue_item_id,
                "event_id": accepted.id,
                "attempt_number": 1,
                "target_agent": "issue-triage",
                "worker_name": "worker-a",
                "claim_token": claim.token,
                "status": "running",
                "started_at": "2026-07-12T10:00:00Z",
            },
            idempotency_key=None,
            caused_by=accepted.id,
            session_id=None,
            timestamp_ms=now_ms,
        )
    )
    assert store.heartbeat_attempt(
        attempt_id,
        claim.queue_item_id,
        "worker-a",
        claim_token=claim.token,
        lease_ms=1_000,
        now_ms=now_ms + 10,
    )

    samples = {sample.name: sample for sample in metrics.samples}
    assert samples["runtime.queue_lag_ms"].value == 100
    assert samples["sqlite.queue_claim_ms"].attributes["claimed"] is True
    assert samples["runtime.lock_conflicts"].value == 1
    assert samples["sqlite.lock_acquire_ms"].value >= 0
    assert samples["sqlite.heartbeat_write_ms"].value >= 0
    assert samples["sqlite.event_append_ms"].value >= 0

    store.close()


def test_queueing_dispatcher_records_retry_scheduling(tmp_path: Path) -> None:
    metrics = InMemoryRuntimeMetrics()
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3", metrics=metrics)

    async def fail(_invocation: AgentInvocation) -> dict[str, object]:
        raise RuntimeError("retry me")

    dispatcher = QueueingDispatcher(
        store,
        store,
        executors=(
            ExecutableAgent(
                AgentDefinition(
                    "issue-triage",
                    (EventPattern("github.issue.opened"),),
                ),
                run=fail,
            ),
        ),
        retry_policy=RetryPolicy(max_attempts=2),
    )

    asyncio.run(
        dispatch_and_drain(
            dispatcher, DraftEvent("github.issue.opened", "github", {"title": "Retry"})
        )
    )

    retries = [
        sample
        for sample in metrics.samples
        if sample.name == "runtime.retries_scheduled"
    ]
    assert len(retries) == 1
    assert retries[0].attributes == {"target_agent": "issue-triage"}

    store.close()


def test_metrics_sink_failures_do_not_change_runtime_writes(tmp_path: Path) -> None:
    class BrokenMetrics:
        def observe(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("metrics unavailable")

    store = RuntimeEventStore.open(
        tmp_path / "runtime.sqlite3",
        metrics=BrokenMetrics(),
    )

    outcome = store.accept(DraftEvent("github.issue.opened", "github", {}))

    assert outcome.inserted
    assert store.get(outcome.event.id) == outcome.event
    store.close()
