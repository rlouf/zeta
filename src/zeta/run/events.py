"""Run lifecycle event vocabulary."""

TURN_EVENT_COMPLETED = "zeta.turn.completed"
TURN_EVENT_FAILED = "zeta.turn.failed"
TURN_RECORD_SCHEMA = "zeta.turn"


def turn_event_type(outcome: str) -> str:
    """Return the durable event type for a turn outcome."""
    if outcome in {"failed", "aborted"}:
        return TURN_EVENT_FAILED
    return TURN_EVENT_COMPLETED
