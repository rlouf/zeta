"""Core runtime for Sigil."""

from __future__ import annotations


def configure_zeta_for_sigil(*, responses: bool = False) -> None:
    from zeta.models import set_profile_session_dir_factory
    from zeta.timeline import (
        TimelineDraftEvent,
        set_durable_event_publisher,
        set_session_id_factory,
    )
    from zeta.trace import set_trace_path_factories

    from .events import DraftEvent, publish_event
    from .session import session_id
    from .state import session_dir, state_dir

    def publish_timeline_event(draft: TimelineDraftEvent) -> object:
        return publish_event(
            DraftEvent(
                event_type=draft.event_type,
                source=draft.source,
                payload=draft.payload,
                idempotency_key=draft.idempotency_key,
                caused_by=draft.caused_by,
                session_id=draft.session_id,
                turn_id=draft.turn_id,
                timestamp_micros=draft.timestamp_micros,
                event_id=draft.event_id,
            )
        )

    set_durable_event_publisher(publish_timeline_event)
    set_session_id_factory(session_id)
    set_profile_session_dir_factory(session_dir)
    set_trace_path_factories(
        state_dir_factory=state_dir,
        session_dir_factory=session_dir,
    )
    if responses:
        from zeta.models import set_responses_session_id_factory

        set_responses_session_id_factory(session_id)
