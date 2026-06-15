"""Core runtime for Sigil."""

from __future__ import annotations


def zeta_context_for_sigil():
    configure_zeta_for_sigil()

    from zeta.context import ZetaContext
    from zeta.events import EVENT_STORE_NAME, SqliteEventStore
    from zeta.tools.registry import registry
    from zeta.trace import default_store

    from .session import session_id
    from .state import session_dir, state_dir

    active_session = session_id()
    return ZetaContext(
        session_id=active_session,
        event_sink=SqliteEventStore(state_dir() / EVENT_STORE_NAME),
        trace_store=default_store(),
        tool_registry=registry,
        state_dir=state_dir(),
        session_dir=session_dir(active_session),
    )


def configure_zeta_for_sigil(*, responses: bool = False) -> None:
    from zeta.timeline import set_session_id_factory
    from zeta.trace import set_trace_path_factories, trace_state_dir

    from .session import session_id as current_session_id

    def trace_session_dir(session_id: str | None = None):
        return trace_state_dir() / "sessions" / (session_id or current_session_id())

    set_session_id_factory(current_session_id)
    set_trace_path_factories(session_dir_factory=trace_session_dir)
    if responses:
        from zeta.models import set_responses_session_id_factory

        set_responses_session_id_factory(current_session_id)
