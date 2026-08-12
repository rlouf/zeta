import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Barrier

import pytest
from zeta.authoring.spec import AgentSpec, ScheduleEntry
from zeta.events import DraftEvent, Event
from zeta.harness.dispatch import (
    QueueingDispatcher,
    ReservedRuntimeEventError,
)
from zeta.harness.routing import AgentRoute, EventPattern
from zeta.harness.scheduling import request_due_schedules, schedule_status
from zeta.harness.sessions import submit_session_message
from zeta.harness.store import (
    InvalidCancellationHandle,
    RuntimeEventStore,
    UnauthorizedCancellation,
    UnknownCancellationHandle,
)
from zeta.journal.memory import MemoryEventStore
from zeta.journal.sqlite import SqliteEventStore
from zeta.journal.store import Filter

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


def _scheduled_event_created(
    *,
    event_id: str,
    handle: str,
    publish_at: str,
    position: int,
) -> Event:
    return Event(
        id=event_id,
        event_type="runtime.scheduled_event.created",
        source="zeta",
        payload={
            "handle": handle,
            "event_type": "report.ready",
            "payload": {"report_id": handle},
            "publish_at": publish_at,
            "source_agent_id": "reporter",
            "source_session_id": "session-1",
            "source_queue_item_id": "queue-item-1",
            "position": position,
        },
        idempotency_key=f"agent.schedule:queue-item-1:{position}",
        caused_by="attempt-completed-1",
        session_id="session-1",
        run_id="run-1",
        timestamp_ms=500 + position,
    )


def _wait_created(
    *,
    event_id: str,
    handle: str,
    session_id: str = "session-1",
    event_type: str = "github.issue.updated",
    fields: dict[str, object] | None = None,
    deadline: str | None = None,
) -> Event:
    return Event(
        id=event_id,
        event_type="runtime.wait.created",
        source="zeta",
        payload={
            "handle": handle,
            "agent_id": "issue-agent",
            "session_id": session_id,
            "event_type": event_type,
            "fields": fields or {},
            "deadline": deadline,
            "source_queue_item_id": "qi-source",
            "project_generation": "generation-1",
        },
        idempotency_key=f"agent.wait:{handle}",
        caused_by="attempt-completed-1",
        session_id=session_id,
        run_id="run-1",
        timestamp_ms=500,
    )


def test_projection_rebuild_preserves_unfinished_attempt_and_releases_claim(
    tmp_path: Path,
) -> None:
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")
    accepted = store.accept(DraftEvent("github.issue.opened", "github", {})).event
    now_ms = accepted.timestamp_ms + 100
    first_claim = store.claim_next_queue_item(
        "worker-a",
        lease_ms=1_000,
        now_ms=now_ms,
    )
    assert first_claim is not None

    queue_item_id = first_claim.queue_item_id
    store.append(
        Event(
            id="queue-claimed",
            event_type="runtime.queue_item.claimed",
            source="zeta",
            payload={
                "queue_item_id": queue_item_id,
                "event_id": accepted.id,
                "target_agent": "issue-triage",
                "session_id": "agent/issue-triage/thread-7",
                "status": "claimed",
            },
            idempotency_key=None,
            caused_by=accepted.id,
            session_id=None,
            timestamp_ms=now_ms,
        )
    )
    store.append(
        Event(
            id="attempt-started",
            event_type="runtime.attempt.started",
            source="zeta",
            payload={
                "attempt_id": f"att_{queue_item_id}_1",
                "queue_item_id": queue_item_id,
                "event_id": accepted.id,
                "attempt_number": 1,
                "target_agent": "issue-triage",
                "worker_name": "worker-a",
                "status": "running",
                "started_at": "2026-07-12T10:00:00Z",
            },
            idempotency_key=None,
            caused_by=accepted.id,
            session_id=None,
            timestamp_ms=now_ms + 1,
        )
    )

    store.rebuild_projections()

    queue_item = store.list_queue_items()[0]
    assert queue_item["queue_item_id"] == queue_item_id
    assert queue_item["status"] == "available"
    assert queue_item["session_id"] == "agent/issue-triage/thread-7"
    assert queue_item["claimed_by"] is None
    assert queue_item["claimed_until"] is None
    assert store.list_attempts()[0]["status"] == "running"
    assert store.queue_item_attempt_count(queue_item_id) == 1

    second_claim = store.claim_next_queue_item(
        "worker-b",
        lease_ms=1_000,
        now_ms=now_ms + 2,
    )
    assert second_claim is not None
    assert second_claim.queue_item_id == queue_item_id
    assert second_claim.token != first_claim.token

    store.close()


def test_queue_item_cancel_request_cancels_queued_turn_and_survives_rebuild(
    tmp_path: Path,
) -> None:
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")
    queued = submit_session_message(
        store,
        message="Stop this turn",
        agent_id="master",
        session_id="session-1",
        project_generation="generation-1",
    )

    result = store.cancel_queue_item(
        queued["queue_item_id"],
        expected_session_id="session-1",
        reason="user changed direction",
        now_ms=1_000,
    )

    assert result.status == "cancelled"
    assert result.changed is True
    assert result.queue_item_id == queued["queue_item_id"]
    assert result.run_id == queued["run_id"]
    assert result.session_id == "session-1"
    assert result.terminal_status == "cancelled"
    assert result.event is not None
    assert [event.event_type for event in store.events_for_run(queued["run_id"])][
        -2:
    ] == [
        "runtime.queue_item.cancel_requested",
        "runtime.queue_item.cancelled",
    ]
    assert not store.list_events(Filter(event_type="runtime.cancellation.applied"))
    assert store.queue_item(queued["queue_item_id"]) == {
        "queue_item_id": queued["queue_item_id"],
        "event_id": queued["event_id"],
        "target_agent": "master",
        "project_generation": "generation-1",
        "session_id": "session-1",
        "input_cursor": 1,
        "status": "cancelled",
        "cancel_requested_event_id": result.event.id,
        "cancel_requested_at": 1_000,
        "cancel_reason": "user changed direction",
    }

    repeated = store.cancel_queue_item(
        queued["queue_item_id"],
        expected_session_id="session-1",
        reason="a later reason",
        now_ms=2_000,
    )

    assert repeated.status == "already_cancelled"
    assert repeated.changed is False
    assert (
        len(store.list_events(Filter(event_type="runtime.queue_item.cancel_requested")))
        == 1
    )
    store.rebuild_projections()
    rebuilt = store.queue_item(queued["queue_item_id"])
    assert rebuilt is not None
    assert rebuilt["status"] == "cancelled"
    assert rebuilt["cancel_requested_event_id"] == result.event.id
    assert rebuilt["cancel_requested_at"] == 1_000
    assert rebuilt["cancel_reason"] == "user changed direction"
    store.close()


def test_queue_item_cancel_request_marks_claimed_turn_as_cancelling(
    tmp_path: Path,
) -> None:
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")
    queued = submit_session_message(
        store,
        message="Start this turn",
        agent_id="master",
        session_id="session-1",
        project_generation="generation-1",
    )
    queued_event = store.get(queued["event_id"])
    assert queued_event is not None
    claim = store.claim_next_queue_item(
        "worker-1",
        lease_ms=1_000,
        now_ms=queued_event.timestamp_ms + 1,
    )
    assert claim is not None

    result = store.cancel_queue_item(
        queued["queue_item_id"],
        expected_session_id="session-1",
        now_ms=600,
    )

    assert result.status == "cancelling"
    assert result.changed is True
    assert result.terminal_status is None
    item = store.queue_item(queued["queue_item_id"])
    assert item is not None
    assert item["status"] == "claimed"
    assert item["cancel_requested_at"] == 600
    assert (
        len(store.list_events(Filter(event_type="runtime.queue_item.cancel_requested")))
        == 1
    )
    assert not store.list_events(Filter(event_type="runtime.queue_item.cancelled"))
    store.close()


def test_worker_recovery_finalizes_a_requested_turn_before_claiming_it(
    tmp_path: Path,
) -> None:
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")
    queued = submit_session_message(
        store,
        message="Interrupted turn",
        agent_id="master",
        session_id="session-1",
        project_generation="generation-1",
    )
    requested = store.get(queued["event_id"])
    assert requested is not None
    claim = store.claim_next_queue_item(
        "worker-1",
        lease_ms=1_000,
        now_ms=requested.timestamp_ms + 1,
    )
    assert claim is not None
    attempt_id = f"att_{queued['queue_item_id']}_1"
    store.accept(
        DraftEvent(
            "runtime.attempt.started",
            "zeta",
            {
                "attempt_id": attempt_id,
                "queue_item_id": queued["queue_item_id"],
                "event_id": queued["event_id"],
                "attempt_number": 1,
                "target_agent": "master",
                "worker_name": "worker-1",
                "status": "running",
                "started_at": "2026-08-07T10:00:00Z",
                "session_id": "session-1",
                "run_id": queued["run_id"],
            },
            idempotency_key=f"attempt:{queued['queue_item_id']}:1:started",
            caused_by=queued["event_id"],
            session_id="session-1",
            run_id=queued["run_id"],
        )
    )
    cancellation = store.cancel_queue_item(
        queued["queue_item_id"],
        reason="worker stopped",
        now_ms=requested.timestamp_ms + 2,
    )
    assert cancellation.status == "cancelling"
    assert cancellation.event is not None

    store.rebuild_projections()

    recovered = store.queue_item(queued["queue_item_id"])
    assert recovered is not None
    assert recovered["status"] == "available"
    assert recovered["cancel_requested_event_id"] == cancellation.event.id
    assert (
        store.claim_next_queue_item(
            "worker-2",
            lease_ms=1_000,
            now_ms=requested.timestamp_ms + 3,
        )
        is None
    )

    finalized = store.finalize_next_cancel_requested_queue_item(
        now_ms=requested.timestamp_ms + 4
    )

    assert finalized is not None
    queue_item_id, events = finalized
    assert queue_item_id == queued["queue_item_id"]
    assert [event.event_type for event in events] == [
        "runtime.attempt.cancelled",
        "runtime.queue_item.cancelled",
    ]
    assert store.list_attempts()[0]["status"] == "cancelled"
    terminal = store.queue_item(queued["queue_item_id"])
    assert terminal is not None
    assert terminal["status"] == "cancelled"
    assert store.finalize_next_cancel_requested_queue_item() is None
    store.rebuild_projections()
    assert store.list_attempts()[0]["status"] == "cancelled"
    rebuilt = store.queue_item(queued["queue_item_id"])
    assert rebuilt is not None
    assert rebuilt["status"] == "cancelled"
    store.close()


def test_queue_item_cancel_request_rejects_a_different_session(tmp_path: Path) -> None:
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")
    queued = submit_session_message(
        store,
        message="Private turn",
        agent_id="master",
        session_id="session-owner",
        project_generation="generation-1",
    )

    with pytest.raises(UnauthorizedCancellation):
        store.cancel_queue_item(
            queued["queue_item_id"],
            expected_session_id="session-other",
        )

    assert not store.list_events(
        Filter(event_type="runtime.queue_item.cancel_requested")
    )
    store.close()


def test_queue_item_cancel_request_reports_unknown_and_terminal_items(
    tmp_path: Path,
) -> None:
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")

    unknown = store.cancel_queue_item("qi-unknown")

    assert unknown.status == "unknown"
    assert unknown.changed is False
    queued = submit_session_message(
        store,
        message="Finished turn",
        agent_id="master",
        session_id="session-1",
        project_generation="generation-1",
    )
    store.accept(
        DraftEvent(
            "runtime.queue_item.completed",
            "zeta",
            {
                "queue_item_id": queued["queue_item_id"],
                "event_id": queued["event_id"],
                "target_agent": "master",
                "project_generation": "generation-1",
                "session_id": "session-1",
                "status": "completed",
            },
            idempotency_key=f"queue_item:{queued['event_id']}:master:completed",
            caused_by=queued["event_id"],
            session_id="session-1",
            run_id=queued["run_id"],
        )
    )

    terminal = store.cancel_queue_item(queued["queue_item_id"])

    assert terminal.status == "already_terminal"
    assert terminal.terminal_status == "completed"
    assert terminal.changed is False
    assert not store.list_events(
        Filter(event_type="runtime.queue_item.cancel_requested")
    )
    store.close()


def test_scheduled_event_created_projection_survives_close_and_reopen(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.sqlite3"
    store = RuntimeEventStore.open(path)
    created = _scheduled_event_created(
        event_id="schedule-created-1",
        handle="publication-1",
        publish_at="2026-08-04T10:00:00+00:00",
        position=0,
    )
    store.append(created)

    scheduled = store.list_scheduled_events()

    assert len(scheduled) == 1
    assert scheduled[0]["handle"] == "publication-1"
    assert scheduled[0]["event_type"] == "report.ready"
    assert scheduled[0]["payload"] == {"report_id": "publication-1"}
    assert scheduled[0]["publish_at_ms"] == 1_785_837_600_000
    assert scheduled[0]["source_queue_item_id"] == "queue-item-1"
    assert scheduled[0]["position"] == 0
    assert scheduled[0]["created_event_id"] == created.id
    assert scheduled[0]["status"] == "pending"
    store.close()

    reopened = RuntimeEventStore.open(path)

    assert reopened.list_scheduled_events() == scheduled
    reopened.close()


def test_wait_created_projection_survives_reopen_and_rebuild(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite3"
    store = RuntimeEventStore.open(path)
    created = _wait_created(
        event_id="wait-created-1",
        handle="wait-1",
        fields={"repository": "zeta"},
        deadline="2030-01-02T03:04:05+00:00",
    )
    store.append(created)

    waits = store.list_waits()

    assert waits == [
        {
            "handle": "wait-1",
            "agent_id": "issue-agent",
            "session_id": "session-1",
            "event_type": "github.issue.updated",
            "fields": {"repository": "zeta"},
            "deadline_ms": 1_893_553_445_000,
            "source_queue_item_id": "qi-source",
            "project_generation": "generation-1",
            "created_event_id": created.id,
            "status": "active",
            "matched_event_id": None,
            "terminal_event_id": None,
            "updated_at": 500,
        }
    ]
    store.close()

    reopened = RuntimeEventStore.open(path)
    assert reopened.list_waits() == waits
    reopened.rebuild_projections()
    assert reopened.list_waits() == waits
    reopened.close()


def test_direct_message_cancels_an_active_wait_in_the_same_transaction(
    tmp_path: Path,
) -> None:
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")
    created = _wait_created(
        event_id="wait-created-1",
        handle="wait_0123456789abcdef01234567",
        session_id="session-1",
    )
    store.append(created)

    submission = submit_session_message(
        store,
        message="Use this answer instead.",
        agent_id="issue-agent",
        session_id="session-1",
        project_generation="generation-2",
        idempotency_key="message-1",
    )
    repeated = submit_session_message(
        store,
        message="Use this answer instead.",
        agent_id="issue-agent",
        session_id="session-1",
        project_generation="generation-2",
        idempotency_key="message-1",
    )

    assert repeated == submission
    wait = store.list_waits()[0]
    assert wait["status"] == "cancelled"
    queue_item = store.list_queue_items()[0]
    assert queue_item["session_id"] == "session-1"
    assert queue_item["project_generation"] == "generation-2"
    cancelled = store.list_events(Filter(event_type="runtime.wait.cancelled"))[0]
    requested = store.get(submission["event_id"])
    available = store.list_events(Filter(event_type="runtime.queue_item.available"))[0]
    assert requested is not None
    assert cancelled.cursor is not None
    assert requested.cursor is not None
    assert available.cursor is not None
    assert cancelled.cursor < requested.cursor < available.cursor
    assert len(store.list_events(Filter(event_type="session.message.requested"))) == 1
    assert (
        len(store.list_events(Filter(event_type="runtime.queue_item.available"))) == 1
    )


def test_wait_projection_ignores_malformed_created_event(tmp_path: Path) -> None:
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")
    store.accept(
        DraftEvent(
            "runtime.wait.created",
            "zeta",
            {"handle": "wait-malformed"},
        )
    )

    assert store.list_waits() == []
    store.close()


def test_wait_projection_rejects_a_second_active_wait_for_one_session(
    tmp_path: Path,
) -> None:
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")
    store.append(_wait_created(event_id="wait-created-1", handle="wait-1"))

    with pytest.raises(ValueError, match="session-1.*active wait"):
        store.append(_wait_created(event_id="wait-created-2", handle="wait-2"))

    assert [wait["handle"] for wait in store.list_waits()] == ["wait-1"]
    store.close()


def test_wait_matches_exact_top_level_fields_only(tmp_path: Path) -> None:
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")
    store.append(
        _wait_created(
            event_id="wait-created-1",
            handle="wait-1",
            fields={"repository": {"name": "zeta"}},
        )
    )

    store.accept(
        DraftEvent(
            "github.issue.updated",
            "github",
            {"repository": {"name": "zeta", "owner": "rlouf"}},
        )
    )
    store.accept(
        DraftEvent(
            "github.issue.opened",
            "github",
            {"repository": {"name": "zeta"}},
        )
    )

    assert store.list_waits()[0]["status"] == "active"
    assert store.list_events(Filter(event_type="runtime.wait.matched")) == []
    store.close()


def test_matching_event_consumes_wait_and_creates_continuation(
    tmp_path: Path,
) -> None:
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")
    store.append(
        _wait_created(
            event_id="wait-created-1",
            handle="wait-1",
            fields={"repository": "zeta"},
        )
    )

    matched_event = store.accept(
        DraftEvent(
            "github.issue.updated",
            "github",
            {"repository": "zeta", "issue": 5},
            idempotency_key="github-delivery-1",
        )
    ).event

    matched_facts = store.list_events(Filter(event_type="runtime.wait.matched"))
    assert len(matched_facts) == 1
    matched = matched_facts[0]
    assert matched.caused_by == matched_event.id
    assert matched.session_id == "session-1"
    assert matched.payload == {
        "handle": "wait-1",
        "agent_id": "issue-agent",
        "session_id": "session-1",
        "matched_event_id": matched_event.id,
        "event_type": "github.issue.updated",
        "payload": {"repository": "zeta", "issue": 5},
        "project_generation": "generation-1",
    }

    wait = store.list_waits()[0]
    assert wait["status"] == "matched"
    assert wait["matched_event_id"] == matched_event.id
    assert wait["terminal_event_id"] == matched.id

    continuation_facts = store.list_events(
        Filter(event_type="runtime.queue_item.available")
    )
    assert len(continuation_facts) == 1
    continuation = continuation_facts[0]
    assert continuation.caused_by == matched.id
    assert continuation.session_id == "session-1"
    assert continuation.payload["event_id"] == matched.id
    assert continuation.payload["target_agent"] == "issue-agent"
    assert continuation.payload["project_generation"] == "generation-1"
    assert any(
        item["event_id"] == matched_event.id and item["target_agent"] == ""
        for item in store.list_queue_items()
    )

    expected_wait = store.list_waits()
    store.rebuild_projections()
    assert store.list_waits() == expected_wait
    assert store.queue_item(str(continuation.payload["queue_item_id"])) is not None
    store.close()


def test_duplicate_matching_event_does_not_resume_wait_twice(tmp_path: Path) -> None:
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")
    store.append(_wait_created(event_id="wait-created-1", handle="wait-1"))
    draft = DraftEvent(
        "github.issue.updated",
        "github",
        {"issue": 5},
        idempotency_key="github-delivery-1",
    )

    first = store.accept(draft)
    second = store.accept(draft)

    assert first.inserted
    assert not second.inserted
    assert len(store.list_events(Filter(event_type="runtime.wait.matched"))) == 1
    assert (
        len(store.list_events(Filter(event_type="runtime.queue_item.available"))) == 1
    )
    store.close()


def test_concurrent_matching_events_consume_wait_once(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite3"
    first_store = RuntimeEventStore.open(path)
    second_store = RuntimeEventStore.open(path)
    first_store.append(_wait_created(event_id="wait-created-1", handle="wait-1"))
    ready = Barrier(2)

    def append_match(store: RuntimeEventStore, delivery: str) -> None:
        ready.wait()
        store.accept(
            DraftEvent(
                "github.issue.updated",
                "github",
                {"issue": 5},
                idempotency_key=delivery,
            )
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = (
                executor.submit(append_match, first_store, "github-delivery-1"),
                executor.submit(append_match, second_store, "github-delivery-2"),
            )
            for future in futures:
                future.result()

        assert (
            len(first_store.list_events(Filter(event_type="runtime.wait.matched"))) == 1
        )
        assert (
            len(
                first_store.list_events(
                    Filter(event_type="runtime.queue_item.available")
                )
            )
            == 1
        )
        assert first_store.list_waits()[0]["status"] == "matched"
    finally:
        first_store.close()
        second_store.close()


def test_due_scheduled_event_can_match_an_active_wait(tmp_path: Path) -> None:
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")
    store.append(
        _wait_created(
            event_id="wait-created-1",
            handle="wait-1",
            event_type="report.ready",
            fields={"report_id": "publication-1"},
        )
    )
    store.append(
        _scheduled_event_created(
            event_id="schedule-created-1",
            handle="publication-1",
            publish_at="1970-01-01T00:00:01+00:00",
            position=0,
        )
    )

    requested = store.publish_next_due_scheduled_event(now_ms=2_000)

    assert requested is not None
    assert store.list_waits()[0]["status"] == "matched"
    matched = store.list_events(Filter(event_type="runtime.wait.matched"))[0]
    assert matched.payload["matched_event_id"] == requested.id
    store.close()


def test_wait_does_not_time_out_before_deadline(tmp_path: Path) -> None:
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")
    store.append(
        _wait_created(
            event_id="wait-created-1",
            handle="wait-1",
            deadline="1970-01-01T00:00:02+00:00",
        )
    )

    assert store.timeout_next_due_wait(now_ms=1_999) is None
    assert store.list_waits()[0]["status"] == "active"
    store.close()


def test_due_wait_times_out_and_creates_continuation(tmp_path: Path) -> None:
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")
    store.append(
        _wait_created(
            event_id="wait-created-1",
            handle="wait-1",
            deadline="1970-01-01T00:00:02+00:00",
        )
    )

    timed_out = store.timeout_next_due_wait(now_ms=2_000)

    assert timed_out is not None
    assert timed_out.event_type == "runtime.wait.timed_out"
    assert timed_out.caused_by == "wait-created-1"
    assert timed_out.session_id == "session-1"
    assert timed_out.payload == {
        "handle": "wait-1",
        "agent_id": "issue-agent",
        "session_id": "session-1",
        "deadline": "1970-01-01T00:00:02+00:00",
        "project_generation": "generation-1",
    }
    wait = store.list_waits()[0]
    assert wait["status"] == "timed_out"
    assert wait["matched_event_id"] is None
    assert wait["terminal_event_id"] == timed_out.id
    continuation = store.list_events(Filter(event_type="runtime.queue_item.available"))[
        0
    ]
    assert continuation.payload["event_id"] == timed_out.id
    assert continuation.payload["target_agent"] == "issue-agent"
    assert continuation.payload["project_generation"] == "generation-1"

    expected_wait = store.list_waits()
    store.rebuild_projections()
    assert store.list_waits() == expected_wait
    store.close()


def test_match_and_timeout_race_consumes_wait_once(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite3"
    match_store = RuntimeEventStore.open(path)
    timeout_store = RuntimeEventStore.open(path)
    match_store.append(
        _wait_created(
            event_id="wait-created-1",
            handle="wait-1",
            deadline="1970-01-01T00:00:02+00:00",
        )
    )
    ready = Barrier(2)

    def match() -> None:
        ready.wait()
        match_store.accept(DraftEvent("github.issue.updated", "github", {"issue": 5}))

    def time_out() -> None:
        ready.wait()
        timeout_store.timeout_next_due_wait(now_ms=2_000)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = (executor.submit(match), executor.submit(time_out))
            for future in futures:
                future.result()

        matched = match_store.list_events(Filter(event_type="runtime.wait.matched"))
        timed_out = match_store.list_events(Filter(event_type="runtime.wait.timed_out"))
        assert len(matched) + len(timed_out) == 1
        assert (
            len(
                match_store.list_events(
                    Filter(event_type="runtime.queue_item.available")
                )
            )
            == 1
        )
        assert match_store.list_waits()[0]["status"] in {"matched", "timed_out"}
    finally:
        match_store.close()
        timeout_store.close()


def test_cancel_active_wait_records_one_terminal_fact_and_survives_rebuild(
    tmp_path: Path,
) -> None:
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")
    handle = "wait_0123456789abcdef01234567"
    store.append(_wait_created(event_id="wait-created-1", handle=handle))

    first = store.cancel_resource(
        handle,
        reason="The issue was closed",
        source_agent_id="issue-agent",
        source_session_id="session-1",
        now_ms=1_000,
    )
    second = store.cancel_resource(
        handle,
        source_agent_id="issue-agent",
        source_session_id="session-1",
        now_ms=1_001,
    )

    assert (first.resource_type, first.status, first.changed) == (
        "wait",
        "cancelled",
        True,
    )
    assert (second.resource_type, second.status, second.changed) == (
        "wait",
        "cancelled",
        False,
    )
    cancelled = store.list_events(Filter(event_type="runtime.wait.cancelled"))
    assert len(cancelled) == 1
    assert cancelled[0].payload == {
        "handle": handle,
        "agent_id": "issue-agent",
        "session_id": "session-1",
        "reason": "The issue was closed",
        "cancelled_by_agent_id": "issue-agent",
        "cancelled_by_session_id": "session-1",
    }
    assert store.list_waits()[0]["status"] == "cancelled"
    assert not store.list_events(Filter(event_type="runtime.queue_item.available"))

    expected = store.list_waits()
    store.rebuild_projections()
    assert store.list_waits() == expected
    store.close()


def test_cancel_pending_scheduled_event_records_terminal_state(tmp_path: Path) -> None:
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")
    handle = "pub_0123456789abcdef01234567"
    store.append(
        _scheduled_event_created(
            event_id="schedule-created-1",
            handle=handle,
            publish_at="2030-01-01T00:00:00+00:00",
            position=0,
        )
    )

    first = store.cancel_resource(
        handle,
        reason="Superseded",
        source_agent_id="reporter",
        source_session_id="session-1",
        now_ms=1_000,
    )
    second = store.cancel_resource(handle, now_ms=1_001)

    assert (first.resource_type, first.status, first.changed) == (
        "scheduled_event",
        "cancelled",
        True,
    )
    assert (second.resource_type, second.status, second.changed) == (
        "scheduled_event",
        "cancelled",
        False,
    )
    cancelled = store.list_events(
        Filter(event_type="runtime.scheduled_event.cancelled")
    )
    assert len(cancelled) == 1
    assert cancelled[0].payload["reason"] == "Superseded"
    assert cancelled[0].payload["cancelled_by_agent_id"] == "reporter"
    assert cancelled[0].payload["cancelled_by_session_id"] == "session-1"
    assert store.list_scheduled_events()[0]["status"] == "cancelled"
    store.close()


def test_cancel_returns_the_existing_terminal_state(tmp_path: Path) -> None:
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")
    matched_handle = "wait_111111111111111111111111"
    timed_out_handle = "wait_222222222222222222222222"
    published_handle = "pub_333333333333333333333333"
    store.append(_wait_created(event_id="wait-created-matched", handle=matched_handle))
    store.accept(DraftEvent("github.issue.updated", "github", {}))
    store.append(
        _wait_created(
            event_id="wait-created-timeout",
            handle=timed_out_handle,
            session_id="session-2",
            deadline="1970-01-01T00:00:01+00:00",
        )
    )
    store.timeout_next_due_wait(now_ms=2_000)
    store.append(
        _scheduled_event_created(
            event_id="schedule-created-published",
            handle=published_handle,
            publish_at="1970-01-01T00:00:01+00:00",
            position=0,
        )
    )
    store.publish_next_due_scheduled_event(now_ms=2_000)

    assert store.cancel_resource(matched_handle).status == "matched"
    assert store.cancel_resource(timed_out_handle).status == "timed_out"
    assert store.cancel_resource(published_handle).status == "published"
    assert not store.list_events(Filter(event_type="runtime.wait.cancelled"))
    store.close()


def test_cancel_validates_handle_and_session_ownership(tmp_path: Path) -> None:
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")
    handle = "wait_0123456789abcdef01234567"
    store.append(_wait_created(event_id="wait-created-1", handle=handle))

    with pytest.raises(InvalidCancellationHandle):
        store.cancel_resource("queue_012345")
    with pytest.raises(UnknownCancellationHandle):
        store.cancel_resource("wait_999999999999999999999999")
    with pytest.raises(UnauthorizedCancellation):
        store.cancel_resource(
            handle,
            source_agent_id="issue-agent",
            source_session_id="session-2",
        )

    assert store.list_waits()[0]["status"] == "active"
    assert store.cancel_resource(handle).status == "cancelled"
    store.close()


@pytest.mark.parametrize("operation", ["match", "timeout"])
def test_cancel_and_wait_completion_race_has_one_terminal_fact(
    tmp_path: Path,
    operation: str,
) -> None:
    path = tmp_path / "runtime.sqlite3"
    cancel_store = RuntimeEventStore.open(path)
    completion_store = RuntimeEventStore.open(path)
    handle = "wait_0123456789abcdef01234567"
    cancel_store.append(
        _wait_created(
            event_id="wait-created-race",
            handle=handle,
            deadline="1970-01-01T00:00:01+00:00",
        )
    )
    ready = Barrier(2)

    def cancel() -> object:
        ready.wait()
        return cancel_store.cancel_resource(
            handle,
            source_agent_id="issue-agent",
            source_session_id="session-1",
            now_ms=2_000,
        )

    def complete() -> object:
        ready.wait()
        if operation == "match":
            return completion_store.accept(
                DraftEvent("github.issue.updated", "github", {})
            )
        return completion_store.timeout_next_due_wait(now_ms=2_000)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = (executor.submit(cancel), executor.submit(complete))
            for future in futures:
                future.result()

        terminal = sum(
            len(cancel_store.list_events(Filter(event_type=event_type)))
            for event_type in (
                "runtime.wait.cancelled",
                "runtime.wait.matched",
                "runtime.wait.timed_out",
            )
        )
        assert terminal == 1
        assert cancel_store.list_waits()[0]["status"] in {
            "cancelled",
            "matched",
            "timed_out",
        }
    finally:
        cancel_store.close()
        completion_store.close()


def test_projection_rebuild_restores_scheduled_event_terminal_states(
    tmp_path: Path,
) -> None:
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")
    pending = _scheduled_event_created(
        event_id="schedule-created-pending",
        handle="publication-pending",
        publish_at="2026-08-04T10:00:00+00:00",
        position=0,
    )
    published = _scheduled_event_created(
        event_id="schedule-created-published",
        handle="publication-published",
        publish_at="2026-08-04T10:01:00+00:00",
        position=1,
    )
    cancelled = _scheduled_event_created(
        event_id="schedule-created-cancelled",
        handle="publication-cancelled",
        publish_at="2026-08-04T10:02:00+00:00",
        position=2,
    )
    for event in (pending, published, cancelled):
        store.append(event)
    requested = Event(
        id="requested-event-published",
        event_type="report.ready",
        source="zeta",
        payload={"report_id": "publication-published"},
        idempotency_key="agent.publish:queue-item-1:1",
        caused_by=published.id,
        session_id="session-1",
        run_id="run-1",
        timestamp_ms=1_000,
    )
    store.append(requested)
    published_fact = Event(
        id="schedule-published-1",
        event_type="runtime.scheduled_event.published",
        source="zeta",
        payload={
            "handle": "publication-published",
            "published_event_id": requested.id,
        },
        idempotency_key="scheduled_event.published:publication-published",
        caused_by=requested.id,
        session_id="session-1",
        run_id="run-1",
        timestamp_ms=1_001,
    )
    cancelled_fact = Event(
        id="schedule-cancelled-1",
        event_type="runtime.scheduled_event.cancelled",
        source="zeta",
        payload={"handle": "publication-cancelled"},
        idempotency_key="scheduled_event.cancelled:publication-cancelled",
        caused_by=cancelled.id,
        session_id="session-1",
        run_id="run-1",
        timestamp_ms=1_002,
    )
    store.append(published_fact)
    store.append(cancelled_fact)

    store.rebuild_projections()

    scheduled_by_handle = {row["handle"]: row for row in store.list_scheduled_events()}
    assert scheduled_by_handle["publication-pending"]["status"] == "pending"
    assert scheduled_by_handle["publication-published"]["status"] == "published"
    assert (
        scheduled_by_handle["publication-published"]["published_event_id"]
        == requested.id
    )
    assert (
        scheduled_by_handle["publication-published"]["terminal_event_id"]
        == published_fact.id
    )
    assert scheduled_by_handle["publication-cancelled"]["status"] == "cancelled"
    assert (
        scheduled_by_handle["publication-cancelled"]["terminal_event_id"]
        == cancelled_fact.id
    )
    store.close()


def test_two_connections_publish_one_due_scheduled_event_exactly_once(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.sqlite3"
    setup_store = RuntimeEventStore.open(path)
    created = _scheduled_event_created(
        event_id="schedule-created-due",
        handle="publication-due",
        publish_at="1970-01-01T00:00:01+00:00",
        position=0,
    )
    setup_store.append(created)
    setup_store.close()
    first = RuntimeEventStore.open(path)
    second = RuntimeEventStore.open(path)
    ready = Barrier(2)

    def publish(store: RuntimeEventStore) -> object:
        ready.wait()
        return store.publish_next_due_scheduled_event(now_ms=2_000)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(publish, (first, second)))

        assert sum(bool(outcome) for outcome in outcomes) == 1
        requested = first.list_events(Filter(event_type="report.ready"))
        published_facts = first.list_events(
            Filter(event_type="runtime.scheduled_event.published")
        )
        assert len(requested) == 1
        assert len(published_facts) == 1
        assert requested[0].payload == {"report_id": "publication-due"}
        assert requested[0].idempotency_key == "agent.publish:queue-item-1:0"
        assert requested[0].caused_by == created.id
        assert published_facts[0].caused_by == requested[0].id
        assert published_facts[0].payload["handle"] == "publication-due"
        assert published_facts[0].payload["published_event_id"] == requested[0].id
        assert first.list_scheduled_events()[0]["status"] == "published"
    finally:
        first.close()
        second.close()


def test_cancel_and_publish_race_has_one_terminal_scheduled_event(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.sqlite3"
    setup_store = RuntimeEventStore.open(path)
    created = _scheduled_event_created(
        event_id="schedule-created-race",
        handle="pub_0123456789abcdef01234567",
        publish_at="1970-01-01T00:00:01+00:00",
        position=0,
    )
    setup_store.append(created)
    setup_store.close()
    cancel_store = RuntimeEventStore.open(path)
    publish_store = RuntimeEventStore.open(path)
    ready = Barrier(2)

    def cancel() -> object:
        ready.wait()
        return cancel_store.cancel_resource(
            "pub_0123456789abcdef01234567",
            source_agent_id="reporter",
            source_session_id="session-1",
            now_ms=2_000,
        )

    def publish() -> object:
        ready.wait()
        return publish_store.publish_next_due_scheduled_event(now_ms=2_000)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = (executor.submit(cancel), executor.submit(publish))
            for future in futures:
                future.result()

        published_facts = cancel_store.list_events(
            Filter(event_type="runtime.scheduled_event.published")
        )
        cancelled_facts = cancel_store.list_events(
            Filter(event_type="runtime.scheduled_event.cancelled")
        )
        requested = cancel_store.list_events(Filter(event_type="report.ready"))
        assert len(published_facts) + len(cancelled_facts) == 1
        assert len(requested) == len(published_facts)
        expected_status = "published" if published_facts else "cancelled"
        assert cancel_store.list_scheduled_events()[0]["status"] == expected_status
    finally:
        cancel_store.close()
        publish_store.close()


def test_runtime_transaction_rolls_back_nested_event_appends(tmp_path: Path) -> None:
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")
    event = Event(
        id="requested-event-rollback",
        event_type="work.requested",
        source="test",
        payload={"work_id": "one"},
        idempotency_key=None,
        caused_by=None,
        session_id="session-1",
        timestamp_ms=1_000,
    )

    with pytest.raises(RuntimeError, match="stop the batch"):
        with store.transaction():
            store.append(event)
            store.ensure_pending_queue_item(event)
            store.accept(DraftEvent("work.accepted", "test", {"work_id": "two"}))
            raise RuntimeError("stop the batch")

    assert store.list_events(Filter()) == []
    assert store.list_queue_items() == []
    store.close()


def test_runtime_transaction_rolls_back_on_cancellation(tmp_path: Path) -> None:
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")

    with pytest.raises(asyncio.CancelledError):
        with store.transaction():
            store.accept(DraftEvent("work.accepted", "test", {}))
            raise asyncio.CancelledError

    assert not store.connection.in_transaction
    assert store.list_events(Filter()) == []
    store.close()


def test_due_publication_rolls_back_when_terminal_fact_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")
    created = _scheduled_event_created(
        event_id="schedule-created-rollback",
        handle="publication-rollback",
        publish_at="1970-01-01T00:00:01+00:00",
        position=0,
    )
    store.append(created)
    append_in_transaction = SqliteEventStore.append_in_transaction

    def fail_terminal_fact(event_store: SqliteEventStore, event: Event) -> object:
        if event.event_type == "runtime.scheduled_event.published":
            raise RuntimeError("terminal append failed")
        return append_in_transaction(event_store, event)

    monkeypatch.setattr(
        SqliteEventStore,
        "append_in_transaction",
        fail_terminal_fact,
    )

    with pytest.raises(RuntimeError, match="terminal append failed"):
        store.publish_next_due_scheduled_event(now_ms=2_000)

    assert store.list_events(Filter(event_type="report.ready")) == []
    assert store.list_queue_items() == []
    assert store.list_scheduled_events()[0]["status"] == "pending"
    store.close()


def test_dispatch_ingress_script_is_idempotent_and_reserves_runtime_namespace(
    tmp_path: Path,
) -> None:
    case = _dispatch_scripted_case(
        "ingress",
        "idempotent_external_event_and_reserved_namespace",
    )
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")
    dispatcher = QueueingDispatcher(store, store)
    draft = DraftEvent(**case["draft"])

    first = asyncio.run(dispatcher.publish_event(draft))
    repeated = asyncio.run(dispatcher.publish_event(draft))

    assert [first.inserted, repeated.inserted] == case["expected"]["inserted"]
    assert repeated.event.id == first.event.id
    assert len(store.list_events(Filter())) == case["expected"]["event_count"]
    queue_item = store.list_queue_items()[0]
    assert {
        "queue_item_id": queue_item["queue_item_id"].replace(first.event.id, "$input"),
        "event_id": "$input",
        "target_agent": queue_item["target_agent"],
        "status": queue_item["status"],
    } == case["expected"]["queue_item"]
    with pytest.raises(ReservedRuntimeEventError):
        asyncio.run(
            dispatcher.publish_event(DraftEvent(case["reserved_type"], "external", {}))
        )
    assert len(store.list_events(Filter())) == case["expected"]["event_count"]
    store.close()


@pytest.mark.parametrize(
    "case_name",
    ["fan_out_closes_unbound_barrier", "unhandled_closes_unbound_barrier"],
)
def test_dispatch_routing_scripts_freeze_lifecycle_order(
    tmp_path: Path,
    case_name: str,
) -> None:
    case = _dispatch_scripted_case("routing", case_name)
    store = RuntimeEventStore.open(tmp_path / f"{case_name}.sqlite3")
    trigger = Event(**case["event"])
    store.append(trigger)
    routes = tuple(
        AgentRoute(
            route["agent_id"],
            tuple(EventPattern(pattern) for pattern in route["accepts"]),
            session=route["session"],
            project_generation=route.get("project_generation"),
        )
        for route in case["routes"]
    )
    dispatcher = QueueingDispatcher(store, store, routes=routes)
    pending = store.list_queue_items()[0]

    lifecycle = asyncio.run(dispatcher.run_queue_item(pending["queue_item_id"]))

    assert (
        _normalize_event_contract(lifecycle, case["expected"]["events"])
        == case["expected"]["events"]
    )
    actual_queue_items = sorted(
        (
            {
                "queue_item_id": item["queue_item_id"],
                "target_agent": item["target_agent"],
                "session_id": item.get("session_id"),
                "project_generation": item.get("project_generation"),
                "status": item["status"],
            }
            for item in store.list_queue_items()
        ),
        key=lambda item: item["queue_item_id"],
    )
    assert actual_queue_items == case["expected"]["queue_items"]
    store.close()


def test_dispatch_claim_fencing_script_rejects_stale_ownership(
    tmp_path: Path,
) -> None:
    case = _dispatch_scripted_case("claim_fencing", "released_token_stays_stale")
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")
    store.append(Event(**case["event"]))
    now_ms = case["now_ms"]

    first = store.claim_next_queue_item(
        case["workers"][0],
        lease_ms=case["lease_ms"],
        now_ms=now_ms,
    )
    assert first is not None
    wrong_token_current = store.queue_claim_is_current(
        first.queue_item_id,
        case["workers"][0],
        "stale-token",
        now_ms=now_ms,
    )
    released = store.release_queue_claim(
        first.queue_item_id,
        case["workers"][0],
        claim_token=first.token,
        now_ms=now_ms + 1,
    )
    second = store.claim_next_queue_item(
        case["workers"][1],
        lease_ms=case["lease_ms"],
        now_ms=now_ms + 2,
    )
    assert second is not None
    queue_record = store.queue_item(second.queue_item_id)
    assert queue_record is not None

    actual = {
        "tokens_differ": first.token != second.token,
        "wrong_token_current": wrong_token_current,
        "released": released,
        "released_token_current": store.queue_claim_is_current(
            first.queue_item_id,
            case["workers"][0],
            first.token,
            now_ms=now_ms + 2,
        ),
        "new_token_current": store.queue_claim_is_current(
            second.queue_item_id,
            case["workers"][1],
            second.token,
            now_ms=now_ms + 2,
        ),
        "queue_status": queue_record["status"],
    }
    assert actual == case["expected"]
    store.close()


def test_dispatch_cancellation_script_is_durable_and_idempotent(
    tmp_path: Path,
) -> None:
    case = _dispatch_scripted_case("cancellation", "queued_turn_cancels_once")
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")
    queued = submit_session_message(store, **case["message"])

    first = store.cancel_queue_item(
        queued["queue_item_id"],
        expected_session_id=case["message"]["session_id"],
        reason=case["reason"],
        now_ms=case["now_ms"],
    )
    repeated = store.cancel_queue_item(
        queued["queue_item_id"],
        expected_session_id=case["message"]["session_id"],
        reason="ignored repeat",
        now_ms=case["now_ms"] + 1,
    )
    journal = store.list_events(Filter())

    assert (
        _normalize_event_contract(journal, case["expected"]["events"])
        == case["expected"]["events"]
    )
    assert [first.status, repeated.status] == case["expected"]["results"]
    assert [first.changed, repeated.changed] == case["expected"]["changed"]
    queue_record = store.queue_item(queued["queue_item_id"])
    assert queue_record is not None
    assert queue_record["status"] == case["expected"]["queue_status"]
    store.rebuild_projections()
    queue_record = store.queue_item(queued["queue_item_id"])
    assert queue_record is not None
    assert queue_record["status"] == case["expected"]["queue_status"]
    store.close()


def test_dispatch_wait_script_matches_once_and_keeps_input_routable(
    tmp_path: Path,
) -> None:
    case = _dispatch_scripted_case("waits", "matching_event_resumes_once")
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")
    store.append(Event(**case["created_event"]))
    draft = DraftEvent(**case["matching_draft"])

    first = store.accept(draft)
    repeated = store.accept(draft)
    journal = store.list_events(Filter())

    assert [first.inserted, repeated.inserted] == case["expected"]["inserted"]
    assert (
        _normalize_event_contract(journal, case["expected"]["events"])
        == case["expected"]["events"]
    )
    wait = store.list_waits()[0]
    assert {
        "status": wait["status"],
        "matched_event_id": "$matching_input"
        if wait["matched_event_id"] == first.event.id
        else wait["matched_event_id"],
    } == case["expected"]["wait"]
    assert (
        sorted(item["status"] for item in store.list_queue_items())
        == case["expected"]["queue_statuses"]
    )
    store.close()


def test_dispatch_one_shot_schedule_script_publishes_exactly_once(
    tmp_path: Path,
) -> None:
    case = _dispatch_scripted_case(
        "scheduled_events",
        "due_publication_consumes_pending_schedule",
    )
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")
    store.append(Event(**case["created_event"]))

    first = store.publish_next_due_scheduled_event(now_ms=case["now_ms"])
    repeated = store.publish_next_due_scheduled_event(now_ms=case["now_ms"])
    journal = store.list_events(Filter())

    assert first is not None
    assert repeated is None
    assert (
        _normalize_event_contract(journal, case["expected"]["events"])
        == case["expected"]["events"]
    )
    assert (
        store.list_scheduled_events()[0]["status"]
        == case["expected"]["schedule_status"]
    )
    store.close()


def test_dispatch_recurring_schedule_script_records_activation_and_decision() -> None:
    case = _dispatch_scripted_case(
        "recurring_schedules",
        "latest_catchup_activates_before_publishing",
    )
    event_store = MemoryEventStore()
    schedule = ScheduleEntry(**case["schedule"])
    spec = AgentSpec(
        slug=case["agent_id"],
        name="Reporter",
        description="Reports on schedule.",
        instructions="Report.",
        path=Path("agents/reporter.md"),
        content_address="b3:fixture",
        schedules=(schedule,),
    )

    first = request_due_schedules(
        event_store,
        (spec,),
        now=datetime.fromisoformat(case["ticks"][0]),
    )
    second = request_due_schedules(
        event_store,
        (spec,),
        now=datetime.fromisoformat(case["ticks"][1]),
    )
    journal = event_store.list_events(Filter())

    assert [len(first), len(second)] == case["expected"]["published_per_tick"]
    assert (
        _normalize_event_contract(journal, case["expected"]["events"])
        == case["expected"]["events"]
    )
    assert [
        row.as_record()
        for row in schedule_status(
            event_store,
            (spec,),
            now=datetime.fromisoformat(case["ticks"][1]),
        )
    ] == case["expected"]["read_model"]


def test_dispatch_projection_recovery_script_discards_live_ownership(
    tmp_path: Path,
) -> None:
    case = _dispatch_scripted_case(
        "projection_recovery",
        "rebuild_preserves_history_and_releases_claim",
    )
    store = RuntimeEventStore.open(tmp_path / "runtime.sqlite3")
    for value in case["events"]:
        store.append(Event(**value))
    claim = store.claim_next_queue_item(
        case["worker"],
        lease_ms=case["lease_ms"],
        now_ms=case["now_ms"],
    )
    assert claim is not None
    assert store.acquire_locks(
        case["lock_keys"],
        claim.token,
        lease_ms=case["lease_ms"],
        now_ms=case["now_ms"],
    )
    ownership_before_rebuild = store.queue_claim_is_current(
        claim.queue_item_id,
        case["worker"],
        claim.token,
        now_ms=case["now_ms"],
    )
    for value in case["lifecycle_events"]:
        store.append(Event(**value))

    store.rebuild_projections()
    after = store.queue_item(claim.queue_item_id)

    assert after is not None
    assert {
        "ownership_before_rebuild": ownership_before_rebuild,
        "after": {
            "status": after["status"],
            "claimed_by": after.get("claimed_by"),
            "claimed_until": after.get("claimed_until"),
        },
        "attempt_statuses": [attempt["status"] for attempt in store.list_attempts()],
        "attempt_count": store.queue_item_attempt_count(claim.queue_item_id),
        "locks": store.list_locks(),
        "journal_event_ids": [event.id for event in store.list_events(Filter())],
    } == case["expected"]
    store.close()
