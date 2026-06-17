"""Core runtime for Sigil."""

from __future__ import annotations


def zeta_context_for_sigil():
    from zeta.context import context_for_session, zeta_state_dir
    from zeta.tools.registry import registry

    from .sessions import session_dir, session_id

    active_session = session_id()
    zeta_dir = zeta_state_dir()
    return context_for_session(
        session_id=active_session,
        state_dir=zeta_dir,
        session_dir=session_dir(active_session),
        tool_registry=registry,
    )
