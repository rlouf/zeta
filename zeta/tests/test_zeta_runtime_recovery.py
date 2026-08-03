import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from zeta.events import DraftEvent, Event
from zeta.harness.store import RuntimeEventStore
from zeta.journal.sqlite import SqliteEventStore
from zeta.journal.store import Filter


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
        handle="publication-race",
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
        return cancel_store.cancel_scheduled_event(
            "publication-race",
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
