"""Core runtime for Commas."""


def zeta_session_for_commas():
    from zeta.capabilities.registry import registry
    from zeta.run.context import session_for_id

    from commas.sessions import session_dir, session_id
    from commas.state import state_dir

    active_session = session_id()
    zeta_dir = state_dir()
    return session_for_id(
        session_id=active_session,
        state_dir=zeta_dir,
        session_dir=session_dir(active_session),
        tool_registry=registry,
    )
