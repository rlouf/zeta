"""Render authored templates against a durable event.

An agent author names a field of an event and the runtime fills it in. The same
rendering serves an idempotency key and a session scope, so both read the same
way and fail the same way.

This module sits below routing and sessions, so both may use it.
"""

from __future__ import annotations

from zeta import ids
from zeta.events import DraftEvent, Event

SHARED_SESSION = "shared"
PER_EVENT_SESSION = "per-event"
SESSION_LITERALS = frozenset({SHARED_SESSION, PER_EVENT_SESSION})


def render_template(
    template: str,
    event: DraftEvent | Event,
    *,
    what: str = "template",
) -> str:
    """Fill a template from an event payload, or raise when a field is absent.

    Payload fields appear as top-level names, and the event itself is available
    as `event`, so `{chat_id}` and `{event.id}` both resolve.
    """
    try:
        return template.format(event=event, **dict(event.payload))
    except (KeyError, IndexError) as exc:
        raise RuntimeError(
            f"{what} {template!r} references a missing field: {exc}"
        ) from exc


def session_suffix(session: str, event: Event) -> str | None:
    """Return what identifies this invocation's session.

    `None` means the agent identifies it. Any other value scopes the timeline
    to that value.
    """
    if session == SHARED_SESSION:
        return None
    if session == PER_EVENT_SESSION:
        return event.id
    return render_template(session, event, what="session template")


def agent_session_id(agent_id: str, session: str, event: Event) -> str:
    """Return the durable session id for one authored agent invocation."""
    return ids.agent_session_id(agent_id, session_suffix(session, event))
