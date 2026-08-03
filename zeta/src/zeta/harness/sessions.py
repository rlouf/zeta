"""Session scope for one agent invocation.

A session is the timeline scope, so it decides what an agent remembers. One
rule answers what identifies a session: the agent, the triggering event, or a
value the event carries. The agent declares which.

The string derivations live in `zeta.ids`, and the rendering in
`zeta.harness.templates`. This module names which rule applies to an
invocation.
"""

from __future__ import annotations

from zeta import ids
from zeta.events import Event
from zeta.harness.routing import (
    AgentDefinition,
    ExecutableAgent,
    is_wait_continuation_for,
)
from zeta.harness.templates import agent_session_id as session_id_for


def agent_session_id(definition: AgentDefinition, event: Event) -> str:
    """Return the durable runtime session id for an authored agent invocation."""
    return session_id_for(definition.agent_id, definition.session, event)


def agent_run_id(attempt_id: str) -> str:
    """Return the run id derived from an attempt."""
    return ids.derived_run_id(attempt_id)


def invocation_session_id(agent: ExecutableAgent, event: Event) -> str | None:
    """Return the session an invocation joins.

    A session turn carries its own session, because the caller owns the
    timeline. Every other event uses the agent's own session rule.
    """
    if (
        event.event_type == "session.turn.requested"
        or is_wait_continuation_for(event, agent.agent_id)
    ) and event.session_id is not None:
        return event.session_id
    return agent_session_id(agent.definition, event)
