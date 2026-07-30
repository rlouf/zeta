import asyncio

from zeta.events import Event
from zeta.records.stores.memory import MemoryEventStore
from zetad.agents import AgentDefinition, EventPattern, ExecutableAgent
from zetad.coordinator import AttemptCoordinator
from zetad.lifecycle import LifecycleRecorder
from zetad.queue import RoutedQueueItem
from zetad.retry import RetryPolicy


def triggering_event() -> Event:
    return Event(
        id="evt_1",
        event_type="work.requested",
        source="test",
        payload={},
        idempotency_key=None,
        caused_by=None,
        session_id=None,
        timestamp_ms=1,
    )


async def stop_heartbeat(_task) -> None:
    return None


def coordinator(
    *,
    claim_is_current,
    retry_policy: RetryPolicy | None = None,
    blocking_unsafe_effect=None,
):
    store = MemoryEventStore()
    recorder = LifecycleRecorder(store)

    def publisher(*_args):
        async def publish(_draft):
            raise AssertionError("test agent did not expect to publish")

        return publish

    def unexpected_retry(*_args, **_kwargs):
        raise AssertionError("retry was not expected")

    return (
        AttemptCoordinator(
            recorder,
            claim_is_current=claim_is_current,
            next_attempt_number=lambda _queue_item_id: 1,
            start_heartbeat=lambda _attempt_id, _queue_item_id, _locks: None,
            stop_heartbeat=stop_heartbeat,
            event_publisher=publisher,
            retry_scheduler=unexpected_retry,
            retry_policy=retry_policy or RetryPolicy(),
            blocking_unsafe_effect=blocking_unsafe_effect,
        ),
        store,
    )


def queue_item() -> RoutedQueueItem:
    return RoutedQueueItem("qi_1", "evt_1", "worker")


def test_attempt_coordinator_records_successful_transition_sequence() -> None:
    async def run(_invocation):
        return {"final_answer": "done"}

    runtime, _store = coordinator(claim_is_current=lambda _queue_item_id: True)
    events = asyncio.run(
        runtime.run(
            ExecutableAgent(
                AgentDefinition("worker", (EventPattern("work.requested"),)),
                run,
            ),
            triggering_event(),
            queue_item(),
        )
    )

    assert [event.event_type for event in events] == [
        "runtime.queue_item.claimed",
        "runtime.attempt.started",
        "runtime.attempt.completed",
        "runtime.queue_item.completed",
    ]


def test_attempt_coordinator_dead_letters_exhausted_failure() -> None:
    async def fail(_invocation):
        raise RuntimeError("boom")

    runtime, _store = coordinator(
        claim_is_current=lambda _queue_item_id: True,
        retry_policy=RetryPolicy(max_attempts=1),
    )
    events = asyncio.run(
        runtime.run(
            ExecutableAgent(
                AgentDefinition("worker", (EventPattern("work.requested"),)),
                fail,
            ),
            triggering_event(),
            queue_item(),
        )
    )

    assert [event.event_type for event in events] == [
        "runtime.queue_item.claimed",
        "runtime.attempt.started",
        "runtime.attempt.failed",
        "runtime.queue_item.dead_lettered",
    ]


def test_attempt_coordinator_does_not_commit_after_claim_loss() -> None:
    ownership_checks = iter((True, False))

    async def run(_invocation):
        return {"final_answer": "stale"}

    runtime, _store = coordinator(
        claim_is_current=lambda _queue_item_id: next(ownership_checks)
    )
    events = asyncio.run(
        runtime.run(
            ExecutableAgent(
                AgentDefinition("worker", (EventPattern("work.requested"),)),
                run,
            ),
            triggering_event(),
            queue_item(),
        )
    )

    assert [event.event_type for event in events] == [
        "runtime.queue_item.claimed",
        "runtime.attempt.started",
    ]


def test_attempt_coordinator_does_not_execute_without_current_claim() -> None:
    executed = False

    async def run(_invocation):
        nonlocal executed
        executed = True
        return {"final_answer": "stale"}

    runtime, _store = coordinator(claim_is_current=lambda _queue_item_id: False)
    events = asyncio.run(
        runtime.run(
            ExecutableAgent(
                AgentDefinition("worker", (EventPattern("work.requested"),)),
                run,
            ),
            triggering_event(),
            queue_item(),
        )
    )

    assert events == []
    assert executed is False


def test_attempt_coordinator_does_not_record_failure_after_claim_loss() -> None:
    ownership_checks = iter((True, False))

    async def fail(_invocation):
        raise RuntimeError("stale failure")

    runtime, _store = coordinator(
        claim_is_current=lambda _queue_item_id: next(ownership_checks)
    )
    events = asyncio.run(
        runtime.run(
            ExecutableAgent(
                AgentDefinition("worker", (EventPattern("work.requested"),)),
                fail,
            ),
            triggering_event(),
            queue_item(),
        )
    )

    assert [event.event_type for event in events] == [
        "runtime.queue_item.claimed",
        "runtime.attempt.started",
    ]


def test_attempt_coordinator_dead_letters_ambiguous_unsafe_effect() -> None:
    executed = False

    async def run(_invocation):
        nonlocal executed
        executed = True
        return {"final_answer": "should not run"}

    runtime, _store = coordinator(
        claim_is_current=lambda _queue_item_id: True,
        blocking_unsafe_effect=lambda _queue_item_id: "effect:unsafe",
    )
    events = asyncio.run(
        runtime.run(
            ExecutableAgent(
                AgentDefinition("worker", (EventPattern("work.requested"),)),
                run,
            ),
            triggering_event(),
            queue_item(),
        )
    )

    assert executed is False
    assert [event.event_type for event in events] == [
        "runtime.queue_item.claimed",
        "runtime.attempt.started",
        "runtime.attempt.failed",
        "runtime.queue_item.dead_lettered",
    ]
    assert events[-1].payload["reason"] == "permanent"
