from pathlib import Path

from zeta.records.events import DraftEvent, Event
from zetad.store import RuntimeEventStore


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
