"""Core runtime for Sigil."""


def zeta_session_for_sigil():
    from zeta.capabilities import registry
    from zeta.session import session_for_id, zeta_state_dir

    from .sessions import session_dir, session_id

    active_session = session_id()
    zeta_dir = zeta_state_dir()
    return session_for_id(
        session_id=active_session,
        state_dir=zeta_dir,
        session_dir=session_dir(active_session),
        tool_registry=registry,
    )
