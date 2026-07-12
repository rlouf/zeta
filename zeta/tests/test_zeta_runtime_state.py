import pytest
from zetad.state import (
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
