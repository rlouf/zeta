import json
from pathlib import Path
from typing import get_args

import pytest
from zeta.harness.retry import (
    DispatchErrorCode,
    RetryPolicy,
    classify_error_code,
)
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

from zeta import ids

RUNTIME_VECTORS_PATH = (
    Path(__file__).resolve().parents[2] / "spec/vectors/dispatch/runtime.json"
)


def _runtime_vectors() -> dict:
    return json.loads(RUNTIME_VECTORS_PATH.read_text(encoding="utf-8"))


def test_runtime_identity_derivations_match_dispatch_vectors() -> None:
    document = _runtime_vectors()

    assert document["format"] == "zeta-dispatch-runtime-v0"
    for case in document["identity_cases"]:
        values = case["input"]
        queue_item_id = ids.queue_item_id(values["event_id"], values["agent_id"])
        attempt_id = ids.attempt_id(queue_item_id, values["attempt_number"])
        actual = {
            "safe_agent_id": ids.safe_agent_id(values["agent_id"]),
            "pending_queue_item_id": ids.pending_queue_item_id(values["event_id"]),
            "queue_item_id": queue_item_id,
            "unhandled_queue_item_id": ids.unhandled_queue_item_id(values["event_id"]),
            "attempt_id": attempt_id,
            "derived_run_id": ids.derived_run_id(attempt_id),
            "selected_run_id": ids.run_id_for_attempt(
                values["claimed_run_id"], attempt_id
            ),
            "publish_event_handle": ids.publish_event_handle(
                queue_item_id, values["request_position"]
            ),
            "wait_handle": ids.wait_handle(queue_item_id, values["request_position"]),
            "queue_item_idempotency_key": ids.queue_item_idempotency_key(
                values["event_id"],
                values["agent_id"],
                values["queue_status"],
            ),
            "queue_item_attempt_idempotency_key": ids.queue_item_idempotency_key(
                values["event_id"],
                values["agent_id"],
                values["queue_status"],
                attempt_number=values["attempt_number"],
            ),
            "unhandled_queue_item_idempotency_key": (
                ids.unhandled_queue_item_idempotency_key(values["event_id"])
            ),
            "attempt_idempotency_key": ids.attempt_idempotency_key(
                queue_item_id,
                values["attempt_number"],
                values["attempt_status"],
            ),
        }

        assert actual == case["expected"], case["name"]


@pytest.mark.parametrize(
    ("vector_name", "transitions", "validator"),
    [
        ("queue", QUEUE_TRANSITIONS, validate_queue_transition),
        ("attempt", ATTEMPT_TRANSITIONS, validate_attempt_transition),
    ],
)
def test_runtime_transition_tables_match_dispatch_vectors_exhaustively(
    vector_name,
    transitions,
    validator,
) -> None:
    vector = _runtime_vectors()["transitions"][vector_name]
    states = vector["states"]
    rows = vector["rows"]

    assert set(states) == set(transitions) - {None}
    assert [row["previous"] for row in rows] == [None, *states]
    for row in rows:
        previous = row["previous"]
        expected_allowed = row["allowed"]
        assert expected_allowed == [
            state for state in states if state in transitions[previous]
        ]
        for current in states:
            if current in expected_allowed:
                validator(previous, current)
            else:
                with pytest.raises(InvalidRuntimeTransition):
                    validator(previous, current)


def test_retry_delays_match_dispatch_vectors() -> None:
    for case in _runtime_vectors()["retry_policies"]:
        policy = RetryPolicy(**case["policy"])
        actual = [
            {
                "attempt_number": attempt["attempt_number"],
                "delay_seconds": policy.delay_seconds(attempt["attempt_number"]),
                "delay_ms": policy.delay_ms(attempt["attempt_number"]),
            }
            for attempt in case["attempts"]
        ]

        assert actual == case["attempts"], case["name"]


def test_retry_classification_matches_dispatch_vectors_exhaustively() -> None:
    vectors = _runtime_vectors()["failure_classification"]
    error_codes = set(get_args(DispatchErrorCode))

    assert {vector["error_code"] for vector in vectors} == error_codes
    policy = RetryPolicy()
    for vector in vectors:
        assert classify_error_code(vector["error_code"]) == vector["failure_class"]
        assert policy.classify(vector["error_code"]) == vector["failure_class"]


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


def test_dispatch_session_read_script_freezes_activity_priority() -> None:
    case = next(
        case
        for case in _runtime_vectors()["scripted_cases"]["session_reads"]
        if case["name"] == "running_queued_waiting_idle_priority"
    )

    actual = project_sessions(
        case["queue_items"],
        case["attempts"],
        case["waits"],
    )

    assert actual == case["expected"]
