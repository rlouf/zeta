"""Run lifecycle event vocabulary."""

from zeta.records.events import TURN_EVENT_COMPLETED, TURN_EVENT_FAILED

TURN_RECORD_SCHEMA = "zeta.turn"


def turn_event_type(outcome: str) -> str:
    """Return the durable event type for a turn outcome."""
    if outcome in {"failed", "aborted"}:
        return TURN_EVENT_FAILED
    return TURN_EVENT_COMPLETED
