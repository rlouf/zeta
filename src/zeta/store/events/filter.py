"""Event store query shapes."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Filter:
    """Criteria for selecting events from the append-only log."""

    event_type: str | None = None
    event_type_prefix: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    caused_by: str | None = None
    after_cursor: int | None = None
    limit: int | None = None
