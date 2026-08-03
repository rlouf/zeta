import asyncio
from contextlib import nullcontext

from zeta.events import Event
from zeta.harness.coordinator import AttemptCoordinator
from zeta.harness.lifecycle import LifecycleRecorder
from zeta.harness.queue import RoutedQueueItem
from zeta.harness.retry import RetryPolicy
from zeta.harness.routing import AgentDefinition, EventPattern, ExecutableAgent
from zeta.journal.memory import MemoryEventStore
from zeta.journal.store import Filter


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
    allow_publication: bool = False,
    next_attempt_number=None,
    completion_batch=None,
):
    store = MemoryEventStore()
    recorder = LifecycleRecorder(store)

    def publisher(*_args):
        async def publish(draft):
            if not allow_publication:
                raise AssertionError("test agent did not expect to publish")
            return store.accept(draft).event

        return publish

    def unexpected_retry(*_args, **_kwargs):
        raise AssertionError("retry was not expected")

    return (
        AttemptCoordinator(
            recorder,
            claim_is_current=claim_is_current,
            next_attempt_number=next_attempt_number or (lambda _queue_item_id: 1),
            start_heartbeat=lambda _attempt_id, _queue_item_id, _locks: None,
            stop_heartbeat=stop_heartbeat,
            event_publisher=publisher,
            retry_scheduler=unexpected_retry,
            retry_policy=retry_policy or RetryPolicy(),
            blocking_unsafe_effect=blocking_unsafe_effect,
            completion_batch=completion_batch or nullcontext,
        ),
        store,
    )


def queue_item() -> RoutedQueueItem:
    return RoutedQueueItem("qi_1", "evt_1", "worker")


def publish_event_request(
    event_type: str,
    position: int,
    *,
    at: str | None = None,
) -> dict[str, object]:
    return {
        "handle": f"publication_qi_1_{position}",
        "event_type": event_type,
        "payload": {"position": position},
        "at": at,
        "position": position,
    }


def stored_events(store: MemoryEventStore) -> list[Event]:
    return store.list_events(Filter())


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


def test_attempt_coordinator_checks_completion_claim_inside_batch() -> None:
    inside_batch = False
    ownership_checks = 0

    class CompletionBatch:
        def __enter__(self) -> None:
            nonlocal inside_batch
            inside_batch = True

        def __exit__(self, *_args) -> None:
            nonlocal inside_batch
            inside_batch = False

    def claim_is_current(_queue_item_id: str) -> bool:
        nonlocal ownership_checks
        ownership_checks += 1
        if ownership_checks == 2:
            assert inside_batch is True
        return True

    async def run(_invocation):
        return {"final_answer": "done"}

    runtime, _store = coordinator(
        claim_is_current=claim_is_current,
        completion_batch=CompletionBatch,
    )

    asyncio.run(
        runtime.run(
            ExecutableAgent(
                AgentDefinition("worker", (EventPattern("work.requested"),)),
                run,
            ),
            triggering_event(),
            queue_item(),
        )
    )

    assert ownership_checks == 2
    assert inside_batch is False


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


def test_attempt_coordinator_publishes_immediate_request_inside_success_barrier() -> (
    None
):
    async def run(_invocation):
        return {
            "final_answer": "done",
            "publish_event_requests": [publish_event_request("work.finished", 0)],
        }

    runtime, store = coordinator(
        claim_is_current=lambda _queue_item_id: True,
        allow_publication=True,
    )

    asyncio.run(
        runtime.run(
            ExecutableAgent(
                AgentDefinition("worker", (EventPattern("work.requested"),)),
                run,
            ),
            triggering_event(),
            queue_item(),
        )
    )

    journal = stored_events(store)
    assert [event.event_type for event in journal] == [
        "runtime.queue_item.claimed",
        "runtime.attempt.started",
        "runtime.attempt.completed",
        "work.finished",
        "runtime.queue_item.completed",
    ]
    assert journal[3].caused_by == journal[2].id


def test_attempt_coordinator_publishes_requests_in_tool_call_order() -> None:
    async def run(_invocation):
        return {
            "final_answer": "done",
            "publish_event_requests": [
                publish_event_request("work.first", 0),
                publish_event_request("work.second", 1),
                publish_event_request("work.third", 2),
            ],
        }

    runtime, store = coordinator(
        claim_is_current=lambda _queue_item_id: True,
        allow_publication=True,
    )

    asyncio.run(
        runtime.run(
            ExecutableAgent(
                AgentDefinition("worker", (EventPattern("work.requested"),)),
                run,
            ),
            triggering_event(),
            queue_item(),
        )
    )

    assert [
        event.event_type
        for event in stored_events(store)
        if event.event_type.startswith("work.")
    ] == ["work.first", "work.second", "work.third"]


def test_attempt_coordinator_does_not_publish_request_from_failed_attempt() -> None:
    async def fail(_invocation):
        raise RuntimeError("boom")

    runtime, store = coordinator(
        claim_is_current=lambda _queue_item_id: True,
        retry_policy=RetryPolicy(max_attempts=1),
        allow_publication=True,
    )

    asyncio.run(
        runtime.run(
            ExecutableAgent(
                AgentDefinition("worker", (EventPattern("work.requested"),)),
                fail,
            ),
            triggering_event(),
            queue_item(),
        )
    )

    assert all(
        event.event_type.startswith("runtime.") for event in stored_events(store)
    )


def test_attempt_coordinator_does_not_publish_request_from_cancelled_attempt() -> None:
    async def cancel(_invocation):
        return {
            "outcome": "cancelled",
            "publish_event_requests": [publish_event_request("work.finished", 0)],
        }

    runtime, store = coordinator(
        claim_is_current=lambda _queue_item_id: True,
        allow_publication=True,
    )

    asyncio.run(
        runtime.run(
            ExecutableAgent(
                AgentDefinition("worker", (EventPattern("work.requested"),)),
                cancel,
            ),
            triggering_event(),
            queue_item(),
        )
    )

    assert [event.event_type for event in stored_events(store)] == [
        "runtime.queue_item.claimed",
        "runtime.attempt.started",
        "runtime.attempt.cancelled",
        "runtime.queue_item.cancelled",
    ]


def test_attempt_coordinator_does_not_publish_request_after_claim_loss() -> None:
    ownership_checks = iter((True, False))

    async def run(_invocation):
        return {
            "final_answer": "stale",
            "publish_event_requests": [publish_event_request("work.finished", 0)],
        }

    runtime, store = coordinator(
        claim_is_current=lambda _queue_item_id: next(ownership_checks),
        allow_publication=True,
    )

    asyncio.run(
        runtime.run(
            ExecutableAgent(
                AgentDefinition("worker", (EventPattern("work.requested"),)),
                run,
            ),
            triggering_event(),
            queue_item(),
        )
    )

    assert [event.event_type for event in stored_events(store)] == [
        "runtime.queue_item.claimed",
        "runtime.attempt.started",
    ]


def test_attempt_coordinator_does_not_duplicate_request_after_retry() -> None:
    attempt_numbers = iter((1, 2))

    async def run(_invocation):
        return {
            "final_answer": "done",
            "publish_event_requests": [publish_event_request("work.finished", 4)],
        }

    runtime, store = coordinator(
        claim_is_current=lambda _queue_item_id: True,
        allow_publication=True,
        next_attempt_number=lambda _queue_item_id: next(attempt_numbers),
    )
    agent = ExecutableAgent(
        AgentDefinition("worker", (EventPattern("work.requested"),)),
        run,
    )

    asyncio.run(runtime.run(agent, triggering_event(), queue_item()))
    asyncio.run(runtime.run(agent, triggering_event(), queue_item()))

    published = [
        event for event in stored_events(store) if event.event_type == "work.finished"
    ]
    assert len(published) == 1
    assert published[0].idempotency_key is not None


def test_attempt_coordinator_treats_past_request_as_immediate() -> None:
    async def run(_invocation):
        return {
            "final_answer": "done",
            "publish_event_requests": [
                publish_event_request(
                    "work.finished",
                    0,
                    at="2000-01-01T00:00:00Z",
                )
            ],
        }

    runtime, store = coordinator(
        claim_is_current=lambda _queue_item_id: True,
        allow_publication=True,
    )

    asyncio.run(
        runtime.run(
            ExecutableAgent(
                AgentDefinition("worker", (EventPattern("work.requested"),)),
                run,
            ),
            triggering_event(),
            queue_item(),
        )
    )

    event_types = [event.event_type for event in stored_events(store)]
    assert "work.finished" in event_types
    assert "runtime.scheduled_event.created" not in event_types


def test_attempt_coordinator_records_future_request_inside_success_barrier() -> None:
    async def run(_invocation):
        return {
            "final_answer": "done",
            "publish_event_requests": [
                publish_event_request(
                    "work.finished",
                    3,
                    at="2999-01-01T00:00:00Z",
                )
            ],
        }

    runtime, store = coordinator(
        claim_is_current=lambda _queue_item_id: True,
        allow_publication=True,
    )

    asyncio.run(
        runtime.run(
            ExecutableAgent(
                AgentDefinition("worker", (EventPattern("work.requested"),)),
                run,
            ),
            triggering_event(),
            queue_item(),
        )
    )

    journal = stored_events(store)
    assert [event.event_type for event in journal] == [
        "runtime.queue_item.claimed",
        "runtime.attempt.started",
        "runtime.attempt.completed",
        "runtime.scheduled_event.created",
        "runtime.queue_item.completed",
    ]
    assert journal[3].caused_by == journal[2].id
    assert journal[3].payload == {
        "handle": "publication_qi_1_3",
        "event_type": "work.finished",
        "payload": {"position": 3},
        "publish_at": "2999-01-01T00:00:00Z",
        "source_agent_id": "worker",
        "source_session_id": "agent/worker/evt_1",
        "source_queue_item_id": "qi_1",
        "position": 3,
    }
