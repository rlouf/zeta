import pytest
from zetad.state import (
    InvalidRuntimeTransition,
    validate_attempt_transition,
    validate_queue_transition,
)


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
