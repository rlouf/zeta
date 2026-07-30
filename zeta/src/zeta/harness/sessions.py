"""Session scope for one agent invocation.

A session is the timeline scope. A session-scoped agent keeps one session
across events, so its timeline accumulates. A one-shot agent gets a session per
event, so unrelated events do not share timeline.

The string derivations live in `zeta.ids`. This module names which rule applies
to a given invocation.
"""

from __future__ import annotations

from zeta import ids
from zeta.events import Event
from zeta.harness.routing import AgentDefinition, ExecutableAgent


def agent_session_id(definition: AgentDefinition, event: Event) -> str:
    """Return the durable runtime session id for an authored agent invocation."""
    return ids.agent_session_id(
        definition.agent_id,
        event.id,
        session_scoped=definition.dispatch_mode == "session_scoped",
    )


def agent_run_id(attempt_id: str) -> str:
    """Return the run id derived from an attempt."""
    return ids.derived_run_id(attempt_id)


def invocation_session_id(agent: ExecutableAgent, event: Event) -> str | None:
    """Return the session an invocation joins.

    A session turn carries its own session, because the caller owns the
    timeline. Every other event uses the agent's own session rule.
    """
    if event.event_type == "session.turn.requested" and event.session_id is not None:
        return event.session_id
    return agent_session_id(agent.definition, event)
