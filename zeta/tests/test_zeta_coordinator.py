import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path

from zeta.context.transforms import ContentWorkspace
from zeta.events import DraftEvent, Event
from zeta.harness.coordinator import AttemptCoordinator
from zeta.harness.dispatch import QueueingDispatcher
from zeta.harness.lifecycle import LifecycleRecorder
from zeta.harness.queue import RoutedQueueItem
from zeta.harness.retry import RetryPolicy
from zeta.harness.routing import AgentDefinition, EventPattern, ExecutableAgent
from zeta.harness.store import RuntimeEventStore
from zeta.journal.memory import MemoryEventStore
from zeta.journal.store import Filter
from zeta.substrate import SqliteObjectStore

RUNTIME_VECTORS_PATH = (
    Path(__file__).resolve().parents[2] / "spec/vectors/dispatch/runtime.json"
)


def _dispatch_scripted_case(section: str, name: str) -> dict:
    document = json.loads(RUNTIME_VECTORS_PATH.read_text(encoding="utf-8"))
    return next(
        case for case in document["scripted_cases"][section] if case["name"] == name
    )


def _normalize_event_contract(
    events: list[Event],
    expected: list[dict],
) -> list[dict]:
    assert [event.event_type for event in events] == [item["type"] for item in expected]
    aliases = {
        event.id: item["alias"] for event, item in zip(events, expected, strict=True)
    }

    def normalize(value):
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, str):
            for event_id, alias in sorted(
                aliases.items(), key=lambda item: len(item[0]), reverse=True
            ):
                value = value.replace(event_id, alias)
        return value

    contracts = []
    for event, item in zip(events, expected, strict=True):
        contract = {
            "alias": item["alias"],
            "type": event.event_type,
            "idempotency_key": normalize(event.idempotency_key),
            "caused_by": normalize(event.caused_by),
        }
        expected_payload = item.get("payload")
        if expected_payload is not None:
            contract["payload"] = normalize(
                {key: event.payload[key] for key in expected_payload}
            )
        contracts.append(contract)
    return contracts


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


async def stop_attempt_control(_task) -> None:
    return None


def coordinator(
    *,
    claim_is_current,
    retry_policy: RetryPolicy | None = None,
    blocking_unsafe_effect=None,
    allow_publication: bool = False,
    next_attempt_number=None,
    completion_batch=None,
    cancellation_requested=None,
    start_attempt_control=None,
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
            start_attempt_control=start_attempt_control
            or (lambda _attempt_id, _queue_item_id, _locks, _cancel: None),
            stop_attempt_control=stop_attempt_control,
            cancellation_requested=cancellation_requested
            or (lambda _queue_item_id: False),
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
    handle: str | None = None,
) -> dict[str, object]:
    return {
        "handle": handle or f"publication_qi_1_{position}",
        "event_type": event_type,
        "payload": {"position": position},
        "at": at,
        "position": position,
    }


def wait_request(
    *,
    handle: str = "wait_qi_1_0",
    event_type: str = "issue.updated",
    fields: dict[str, object] | None = None,
    deadline: str | None = None,
    position: int = 0,
) -> dict[str, object]:
    return {
        "handle": handle,
        "event_type": event_type,
        "fields": fields or {},
        "deadline": deadline,
        "position": position,
    }


def cancel_request(
    handle: str,
    position: int,
    *,
    reason: str | None = None,
) -> dict[str, object]:
    return {
        "handle": handle,
        "reason": reason,
        "source_agent_id": "worker",
        "source_session_id": "agent/worker/evt_1",
        "position": position,
    }


def durable_coordinator(store: RuntimeEventStore) -> AttemptCoordinator:
    def publisher(*_args):
        async def publish(draft):
            return store.accept(draft).event

        return publish

    def unexpected_retry(*_args, **_kwargs):
        raise AssertionError("a cancellation request error must be permanent")

    return AttemptCoordinator(
        LifecycleRecorder(store.journal),
        claim_is_current=lambda _queue_item_id: True,
        next_attempt_number=lambda _queue_item_id: 1,
        start_attempt_control=lambda _attempt_id, _queue_item_id, _locks, _cancel: None,
        stop_attempt_control=stop_attempt_control,
        cancellation_requested=lambda _queue_item_id: False,
        event_publisher=publisher,
        retry_scheduler=unexpected_retry,
        retry_policy=RetryPolicy(),
        completion_batch=store.transaction,
        resource_canceller=store.cancel_resource,
    )


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


def test_attempt_coordinator_promotes_content_inside_the_success_barrier(
    tmp_path,
) -> None:
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")
    content = SqliteObjectStore(store.path)
    workspace = ContentWorkspace(
        content,
        run_id="run-content",
        session_id="agent/worker/evt_1",
        owner="worker",
    )
    transformed = workspace.transform(
        {
            "expected_head": workspace.initialize(),
            "reason": "Keep this procedure for later runs.",
            "inputs": {},
            "transformation": {"type": "literal", "value": "Check the release."},
            "destination": {
                "key": "release/check",
                "kind": "procedure",
                "scope": "agent",
                "expected_object_id": None,
            },
        }
    )
    content.close()

    async def run(_invocation):
        return {"content_promotions": [asdict(transformed.promotions[0])]}

    def reject_publication(
        _agent: ExecutableAgent,
        _event: Event,
        _queue_item_id: str,
        _attempt_id: str,
        _session_id: str | None,
        _run_id: str | None,
    ) -> Callable[[DraftEvent], Awaitable[Event]]:
        async def publish(_draft: DraftEvent) -> Event:
            raise AssertionError("content promotion must not publish an event")

        return publish

    def reject_retry(*_args: object, **_kwargs: object) -> Event:
        raise AssertionError("content promotion must not schedule a retry")

    runtime = AttemptCoordinator(
        LifecycleRecorder(store.journal),
        claim_is_current=lambda _queue_item_id: True,
        next_attempt_number=lambda _queue_item_id: 1,
        start_attempt_control=lambda _attempt_id, _queue_item_id, _locks, _cancel: None,
        stop_attempt_control=stop_attempt_control,
        cancellation_requested=lambda _queue_item_id: False,
        event_publisher=reject_publication,
        retry_scheduler=reject_retry,
        retry_policy=RetryPolicy(),
        completion_batch=store.transaction,
        content_promoter=store.promote_content,
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
        "runtime.attempt.completed",
        "runtime.content.promoted",
        "runtime.queue_item.completed",
    ]
    content = SqliteObjectStore(store.path)
    agent_ref = content.get_ref("agent/worker/content/head")
    assert agent_ref is not None
    promoted = content.get_object(agent_ref.object_id)
    assert promoted is not None
    assert promoted.data["nodes"]["release/check"] == transformed.output_ids[0]
    content.close()
    store.close()


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


def test_durable_cancel_request_overrides_a_successful_result() -> None:
    def start_control(_attempt_id, _queue_item_id, _locks, cancellation_event):
        cancellation_event.set()
        return None

    async def finish_after_request(invocation):
        assert invocation.cancellation_event is not None
        assert invocation.cancellation_event.is_set()
        return {
            "final_answer": "must not win",
            "publish_event_requests": [publish_event_request("work.finished", 0)],
        }

    runtime, store = coordinator(
        claim_is_current=lambda _queue_item_id: True,
        allow_publication=True,
        cancellation_requested=lambda _queue_item_id: True,
        start_attempt_control=start_control,
    )

    asyncio.run(
        runtime.run(
            ExecutableAgent(
                AgentDefinition("worker", (EventPattern("work.requested"),)),
                finish_after_request,
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


def test_durable_cancel_request_prevents_retry_after_an_error() -> None:
    async def fail(_invocation):
        raise RuntimeError("model connection closed")

    runtime, store = coordinator(
        claim_is_current=lambda _queue_item_id: True,
        cancellation_requested=lambda _queue_item_id: True,
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


def test_attempt_coordinator_records_wait_inside_success_barrier() -> None:
    async def run(_invocation):
        return {
            "final_answer": "",
            "stop_reason": "tool_stop",
            "wait_requests": [
                wait_request(
                    fields={"repository": "zeta"},
                    deadline="2030-01-02T03:04:05+00:00",
                )
            ],
        }

    runtime, store = coordinator(claim_is_current=lambda _queue_item_id: True)
    agent = ExecutableAgent(
        AgentDefinition(
            "worker",
            (EventPattern("work.requested"),),
            project_generation="generation-1",
        ),
        run,
    )

    asyncio.run(runtime.run(agent, triggering_event(), queue_item()))

    journal = stored_events(store)
    assert [event.event_type for event in journal] == [
        "runtime.queue_item.claimed",
        "runtime.attempt.started",
        "runtime.attempt.completed",
        "runtime.wait.created",
        "runtime.queue_item.completed",
    ]
    assert journal[3].caused_by == journal[2].id
    assert journal[3].session_id == "agent/worker/evt_1"
    assert journal[3].payload == {
        "handle": "wait_qi_1_0",
        "agent_id": "worker",
        "session_id": "agent/worker/evt_1",
        "event_type": "issue.updated",
        "fields": {"repository": "zeta"},
        "deadline": "2030-01-02T03:04:05+00:00",
        "source_queue_item_id": "qi_1",
        "project_generation": "generation-1",
    }


def test_attempt_coordinator_does_not_record_wait_from_cancelled_attempt() -> None:
    async def cancel(_invocation):
        return {
            "outcome": "cancelled",
            "wait_requests": [wait_request()],
        }

    runtime, store = coordinator(claim_is_current=lambda _queue_item_id: True)

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


def test_attempt_coordinator_applies_cancel_before_a_later_wait(tmp_path) -> None:
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")
    old_handle = "wait_0123456789abcdef01234567"
    new_handle = "wait_111111111111111111111111"
    store.accept(
        DraftEvent(
            "runtime.wait.created",
            "zeta",
            {
                "handle": old_handle,
                "agent_id": "worker",
                "session_id": "agent/worker/evt_1",
                "event_type": "issue.updated",
                "fields": {},
                "deadline": None,
                "source_queue_item_id": "qi-existing",
                "project_generation": None,
            },
            session_id="agent/worker/evt_1",
        )
    )

    async def run(_invocation):
        return {
            "cancel_requests": [
                cancel_request(old_handle, 0, reason="Replace the wait")
            ],
            "wait_requests": [wait_request(handle=new_handle, position=1)],
        }

    events = asyncio.run(
        durable_coordinator(store).run(
            ExecutableAgent(
                AgentDefinition("worker", (EventPattern("work.requested"),)),
                run,
            ),
            triggering_event(),
            queue_item(),
        )
    )

    assert [event.event_type for event in events[-4:]] == [
        "runtime.attempt.completed",
        "runtime.wait.cancelled",
        "runtime.wait.created",
        "runtime.queue_item.completed",
    ]
    waits = {row["handle"]: row for row in store.list_waits()}
    assert waits[old_handle]["status"] == "cancelled"
    assert waits[new_handle]["status"] == "active"
    store.close()


def test_attempt_coordinator_can_cancel_a_schedule_created_earlier_in_the_run(
    tmp_path,
) -> None:
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")
    handle = "pub_0123456789abcdef01234567"

    async def run(_invocation):
        return {
            "publish_event_requests": [
                publish_event_request(
                    "work.finished",
                    0,
                    at="2999-01-01T00:00:00Z",
                    handle=handle,
                )
            ],
            "cancel_requests": [cancel_request(handle, 1)],
        }

    events = asyncio.run(
        durable_coordinator(store).run(
            ExecutableAgent(
                AgentDefinition("worker", (EventPattern("work.requested"),)),
                run,
            ),
            triggering_event(),
            queue_item(),
        )
    )

    assert [event.event_type for event in events[-4:]] == [
        "runtime.attempt.completed",
        "runtime.scheduled_event.created",
        "runtime.scheduled_event.cancelled",
        "runtime.queue_item.completed",
    ]
    assert store.list_scheduled_events()[0]["status"] == "cancelled"
    store.close()


def test_attempt_coordinator_does_not_apply_cancel_from_cancelled_attempt(
    tmp_path,
) -> None:
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")
    handle = "wait_0123456789abcdef01234567"
    store.accept(
        DraftEvent(
            "runtime.wait.created",
            "zeta",
            {
                "handle": handle,
                "agent_id": "worker",
                "session_id": "agent/worker/evt_1",
                "event_type": "issue.updated",
                "fields": {},
                "deadline": None,
                "source_queue_item_id": "qi-existing",
                "project_generation": None,
            },
            session_id="agent/worker/evt_1",
        )
    )

    async def run(_invocation):
        return {
            "outcome": "cancelled",
            "cancel_requests": [cancel_request(handle, 0)],
        }

    asyncio.run(
        durable_coordinator(store).run(
            ExecutableAgent(
                AgentDefinition("worker", (EventPattern("work.requested"),)),
                run,
            ),
            triggering_event(),
            queue_item(),
        )
    )

    assert store.list_waits()[0]["status"] == "active"
    assert not store.list_events(Filter(event_type="runtime.wait.cancelled"))
    store.close()


def test_attempt_coordinator_dead_letters_unknown_cancel_and_rolls_back_completion(
    tmp_path,
) -> None:
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")

    async def run(_invocation):
        return {"cancel_requests": [cancel_request("wait_999999999999999999999999", 0)]}

    events = asyncio.run(
        durable_coordinator(store).run(
            ExecutableAgent(
                AgentDefinition("worker", (EventPattern("work.requested"),)),
                run,
            ),
            triggering_event(),
            queue_item(),
        )
    )

    assert [event.event_type for event in events[-2:]] == [
        "runtime.attempt.failed",
        "runtime.queue_item.dead_lettered",
    ]
    assert not store.list_events(Filter(event_type="runtime.attempt.completed"))
    store.close()


def test_attempt_coordinator_rejects_a_second_active_wait(tmp_path) -> None:
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")
    store.accept(
        DraftEvent(
            "runtime.wait.created",
            "zeta",
            {
                "handle": "wait-existing",
                "agent_id": "worker",
                "session_id": "agent/worker/evt_1",
                "event_type": "issue.updated",
                "fields": {},
                "deadline": None,
                "source_queue_item_id": "qi-existing",
                "project_generation": None,
            },
            session_id="agent/worker/evt_1",
        )
    )

    def publisher(*_args):
        async def publish(_draft):
            raise AssertionError("wait creation must not publish an agent event")

        return publish

    def unexpected_retry(*_args, **_kwargs):
        raise AssertionError("an active-wait conflict must be permanent")

    runtime = AttemptCoordinator(
        LifecycleRecorder(store.journal),
        claim_is_current=lambda _queue_item_id: True,
        next_attempt_number=lambda _queue_item_id: 1,
        start_attempt_control=lambda _attempt_id, _queue_item_id, _locks, _cancel: None,
        stop_attempt_control=stop_attempt_control,
        cancellation_requested=lambda _queue_item_id: False,
        event_publisher=publisher,
        retry_scheduler=unexpected_retry,
        retry_policy=RetryPolicy(max_attempts=1),
        completion_batch=store.transaction,
    )

    async def run(_invocation):
        return {"wait_requests": [wait_request(handle="wait-conflict")]}

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
        "runtime.attempt.failed",
        "runtime.queue_item.dead_lettered",
    ]
    assert [row["handle"] for row in store.list_waits()] == ["wait-existing"]
    assert (
        store.list_events(Filter(event_type="runtime.wait.created"))[0].payload[
            "handle"
        ]
        == "wait-existing"
    )
    store.close()


def test_dispatch_completion_script_preserves_result_and_control_order(
    tmp_path: Path,
) -> None:
    case = _dispatch_scripted_case("completion", "ordered_atomic_success")
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")

    async def run(_invocation):
        return case["result"]

    agent = ExecutableAgent(
        AgentDefinition(
            case["agent_id"],
            (EventPattern(case["triggering_event"]["event_type"]),),
            project_generation=case["project_generation"],
        ),
        run,
    )
    trigger = Event(**case["triggering_event"])
    item = RoutedQueueItem(**case["queue_item"])

    returned = asyncio.run(durable_coordinator(store).run(agent, trigger, item))
    journal = store.list_events(Filter())

    assert (
        _normalize_event_contract(journal, case["expected"]["events"])
        == case["expected"]["events"]
    )
    assert [event.id for event in returned] == [event.id for event in journal]
    completed = next(
        event for event in journal if event.event_type == "runtime.attempt.completed"
    )
    assert completed.payload["events"] == case["result"]["events"]
    assert completed.payload["result"]["events"] == case["result"]["events"]
    assert [
        event.event_type
        for event in journal
        if event.event_type
        in {
            "work.first",
            "runtime.wait.created",
            "runtime.scheduled_event.created",
        }
    ] == case["expected"]["ordered_controls"]
    queue_record = store.queue_item(item.queue_item_id)
    assert queue_record is not None
    assert queue_record["status"] == case["expected"]["queue_status"]
    assert store.list_attempts()[0]["status"] == case["expected"]["attempt_status"]
    store.close()


def test_dispatch_completion_script_rolls_back_invalid_proposals(
    tmp_path: Path,
) -> None:
    case = _dispatch_scripted_case("completion", "invalid_proposal_is_atomic_failure")
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")

    async def run(_invocation):
        return case["result"]

    returned = asyncio.run(
        durable_coordinator(store).run(
            ExecutableAgent(
                AgentDefinition(
                    case["agent_id"],
                    (EventPattern(case["triggering_event"]["event_type"]),),
                ),
                run,
            ),
            Event(**case["triggering_event"]),
            RoutedQueueItem(**case["queue_item"]),
        )
    )
    journal = store.list_events(Filter())

    assert (
        _normalize_event_contract(journal, case["expected"]["events"])
        == case["expected"]["events"]
    )
    assert [event.id for event in returned] == [event.id for event in journal]
    assert not store.list_events(Filter(event_type="runtime.attempt.completed"))
    assert store.list_waits() == []
    assert store.list_scheduled_events() == []
    queue_record = store.queue_item(case["queue_item"]["queue_item_id"])
    assert queue_record is not None
    assert queue_record["status"] == case["expected"]["queue_status"]
    assert store.list_attempts()[0]["status"] == case["expected"]["attempt_status"]
    store.close()


def test_dispatch_attempt_outcome_script_retries_then_dead_letters(
    tmp_path: Path,
) -> None:
    case = _dispatch_scripted_case(
        "attempt_outcomes",
        "retry_then_dead_letter",
    )
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")
    trigger = Event(**case["triggering_event"])
    store.append(trigger)

    async def fail(_invocation):
        raise RuntimeError(case["failure_message"])

    policy = RetryPolicy(**case["retry_policy"])
    agent = ExecutableAgent(
        AgentDefinition(
            case["agent_id"],
            (EventPattern(trigger.event_type),),
            retry_policy=policy,
        ),
        fail,
    )
    dispatcher = QueueingDispatcher(
        store,
        store,
        executors=(agent,),
        retry_policy=policy,
    )
    item = RoutedQueueItem(**case["queue_item"])

    asyncio.run(dispatcher.run_queue_item(item))
    asyncio.run(dispatcher.run_queue_item(item))
    lifecycle = store.list_events(Filter())[1:]

    assert (
        _normalize_event_contract(lifecycle, case["expected"]["events"])
        == case["expected"]["events"]
    )
    queue_record = store.queue_item(item.queue_item_id)
    assert queue_record is not None
    assert queue_record["status"] == case["expected"]["queue_status"]
    assert [attempt["status"] for attempt in store.list_attempts()] == case["expected"][
        "attempt_statuses"
    ]
    assert (
        store.queue_item_attempt_count(item.queue_item_id)
        == case["expected"]["attempt_count"]
    )
    store.close()
