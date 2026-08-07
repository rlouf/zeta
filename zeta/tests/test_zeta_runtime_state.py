import pytest
from zeta.harness.sessions import (
    SessionNotFound,
    SessionOwnerConflict,
    project_sessions,
    session_record,
)
from zeta.harness.state import (
    ATTEMPT_TRANSITIONS,
    QUEUE_TRANSITIONS,
    InvalidRuntimeTransition,
    validate_attempt_transition,
    validate_queue_transition,
)


def test_queue_transition_table_is_exhaustively_enforced() -> None:
    statuses = tuple(status for status in QUEUE_TRANSITIONS if status is not None)
    for previous, allowed in QUEUE_TRANSITIONS.items():
        for current in statuses:
            if current in allowed:
                validate_queue_transition(previous, current)
            else:
                with pytest.raises(InvalidRuntimeTransition):
                    validate_queue_transition(previous, current)


def test_attempt_transition_table_is_exhaustively_enforced() -> None:
    statuses = tuple(status for status in ATTEMPT_TRANSITIONS if status is not None)
    for previous, allowed in ATTEMPT_TRANSITIONS.items():
        for current in statuses:
            if current in allowed:
                validate_attempt_transition(previous, current)
            else:
                with pytest.raises(InvalidRuntimeTransition):
                    validate_attempt_transition(previous, current)


@pytest.mark.parametrize("transitions", [QUEUE_TRANSITIONS, ATTEMPT_TRANSITIONS])
def test_runtime_transition_tables_have_no_unreachable_states(transitions) -> None:
    reachable = set(transitions[None])
    frontier = list(reachable)
    while frontier:
        state = frontier.pop()
        for next_state in transitions[state] - reachable:
            reachable.add(next_state)
            frontier.append(next_state)

    assert reachable == set(transitions) - {None}


@pytest.mark.parametrize(
    ("previous", "current"),
    [
        (None, "pending"),
        (None, "available"),
        ("pending", "claimed"),
        ("available", "claimed"),
        ("claimed", "available"),
        ("claimed", "completed"),
        ("claimed", "dead_lettered"),
    ],
)
def test_queue_transition_contract_accepts_legal_transitions(
    previous,
    current,
) -> None:
    validate_queue_transition(previous, current)


@pytest.mark.parametrize(
    ("previous", "current"),
    [
        (None, "completed"),
        ("available", "completed"),
        ("completed", "available"),
        ("dead_lettered", "claimed"),
    ],
)
def test_queue_transition_contract_rejects_illegal_transitions(
    previous,
    current,
) -> None:
    with pytest.raises(InvalidRuntimeTransition):
        validate_queue_transition(previous, current)


@pytest.mark.parametrize(
    ("previous", "current"),
    [
        (None, "running"),
        ("running", "completed"),
        ("running", "failed"),
        ("running", "cancelled"),
    ],
)
def test_attempt_transition_contract_accepts_legal_transitions(
    previous,
    current,
) -> None:
    validate_attempt_transition(previous, current)


def test_attempt_transition_contract_rejects_terminal_restart() -> None:
    with pytest.raises(InvalidRuntimeTransition):
        validate_attempt_transition("failed", "running")


def test_session_projection_uses_activity_priority_and_counts_queued_turns() -> None:
    queue_items = [
        {
            "queue_item_id": "qi_running",
            "target_agent": "worker",
            "session_id": "session-running",
            "status": "claimed",
            "cancel_requested_event_id": "evt_cancel_request",
            "cancel_requested_at": 45,
            "updated_at": 40,
        },
        {
            "queue_item_id": "qi_behind",
            "target_agent": "worker",
            "session_id": "session-running",
            "status": "available",
            "updated_at": 50,
        },
        {
            "queue_item_id": "qi_queued",
            "target_agent": "worker",
            "session_id": "session-queued",
            "status": "retry_scheduled",
            "updated_at": 30,
        },
        {
            "queue_item_id": "qi_waiting",
            "target_agent": "worker",
            "session_id": "session-waiting",
            "status": "completed",
            "updated_at": 20,
        },
        {
            "queue_item_id": "qi_idle",
            "target_agent": "worker",
            "session_id": "session-idle",
            "status": "completed",
            "updated_at": 10,
        },
    ]
    attempts = [
        {
            "attempt_id": "att_running",
            "queue_item_id": "qi_running",
            "target_agent": "worker",
            "session_id": "session-running",
            "run_id": "run_running",
            "status": "running",
            "started_at": "2026-08-06T10:00:00+00:00",
        }
    ]
    waits = [
        {
            "handle": "wait_active",
            "agent_id": "worker",
            "session_id": "session-waiting",
            "event_type": "work.ready",
            "fields": {"work_id": 7},
            "deadline_ms": None,
            "status": "active",
            "updated_at": 25,
        }
    ]

    records = project_sessions(queue_items, attempts, waits)

    assert [record["session_id"] for record in records] == [
        "session-running",
        "session-queued",
        "session-waiting",
        "session-idle",
    ]
    running = session_record(records, "session-running")
    assert running["status"] == "running"
    assert running["active_run_id"] == "run_running"
    assert running["queued_turns"] == 1
    assert running["cancellation_requested"] is True
    assert session_record(records, "session-queued")["status"] == "queued"
    assert session_record(records, "session-queued")["cancellation_requested"] is False
    waiting = session_record(records, "session-waiting")
    assert waiting["status"] == "waiting"
    assert waiting["active_wait"] == {
        "handle": "wait_active",
        "event_type": "work.ready",
        "fields": {"work_id": 7},
        "deadline_ms": None,
    }
    assert session_record(records, "session-idle")["status"] == "idle"


def test_session_projection_reports_conflicting_owners() -> None:
    records = project_sessions(
        [
            {
                "queue_item_id": "qi_1",
                "target_agent": "agent-a",
                "session_id": "session-1",
                "status": "completed",
                "updated_at": 10,
            }
        ],
        [
            {
                "attempt_id": "att_1",
                "queue_item_id": "qi_1",
                "target_agent": "agent-b",
                "session_id": "session-1",
                "run_id": "run_1",
                "status": "completed",
                "started_at": "2026-08-06T10:00:00+00:00",
            }
        ],
        [],
    )

    assert records[0]["agent_id"] is None
    assert records[0]["conflicting_agent_ids"] == ["agent-a", "agent-b"]
    with pytest.raises(SessionOwnerConflict, match="agent-a.*agent-b"):
        session_record(records, "session-1")


def test_session_projection_rejects_an_unknown_session() -> None:
    with pytest.raises(SessionNotFound, match="unknown session"):
        session_record([], "session-missing")
