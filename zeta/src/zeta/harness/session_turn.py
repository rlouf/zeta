"""Event-triggered session-turn agent."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from zeta.events import Event
from zeta.harness.dispatch import QueueingDispatcher
from zeta.harness.queue import terminal_queue_item_result
from zeta.harness.routing import (
    AgentDefinition,
    AgentInvocation,
    EventPattern,
    ExecutableAgent,
)
from zeta.journal.store import EventReader, Filter
from zeta.loop.runtime import empty_session_trace_result
from zeta.loop.runtime_context import RuntimeContext
from zeta.loop.thread_run import (
    run_session_request,
    session_run_id,
    session_turn_requested_draft,
)

RuntimePublishedEvent = Event
SESSION_TURN_AGENT_ID = "zeta.session.turn"


def session_turn_agent(
    runtime_context: RuntimeContext,
    *,
    publish_event: Callable[[RuntimePublishedEvent], None],
) -> ExecutableAgent:
    async def run_agent(invocation: AgentInvocation) -> dict[str, Any]:
        params = dict(invocation.triggering_event.payload)
        run_id = invocation.triggering_event.run_id or optional_string(
            params.get("run_id")
        )
        if run_id is None:
            run_id = session_run_id()
        return await run_session_request(
            params,
            run_id=run_id,
            caused_by=invocation.triggering_event.id,
            publish_event=publish_event,
            runtime_context=runtime_context,
            cancellation_event=invocation.cancellation_event,
        )

    return ExecutableAgent(
        AgentDefinition(
            SESSION_TURN_AGENT_ID,
            (EventPattern("session.turn.requested"),),
        ),
        run=run_agent,
    )


async def submit_session_turn(
    params: dict[str, Any],
    *,
    run_id: str | None = None,
    runtime_context: RuntimeContext,
    event_dispatcher: QueueingDispatcher,
) -> dict[str, Any]:
    run_id = run_id or session_run_id()
    draft = session_turn_requested_draft(
        params,
        run_id=run_id,
        runtime_context=runtime_context,
    )
    outcome = await event_dispatcher.publish_event(draft)
    lifecycle_events = await event_dispatcher.drain()
    result = terminal_queue_item_result(
        lifecycle_events,
        event_id=outcome.event.id,
        target_agent=SESSION_TURN_AGENT_ID,
    )
    if result is None:
        result = terminal_session_turn_result(
            outcome.event,
            runtime_context=runtime_context,
        )
    if result is not None:
        return result
    return {
        "run_id": run_id,
        "outcome": "duplicate" if not outcome.inserted else "unhandled",
        "final_answer": "",
        "trace": empty_session_trace_result(),
    }


def terminal_session_turn_result(
    requested_event: Event,
    *,
    runtime_context: RuntimeContext,
) -> dict[str, Any] | None:
    """Return a previously recorded terminal result for one requested session turn."""

    if not isinstance(runtime_context.event_sink, EventReader):
        return None
    return terminal_queue_item_result(
        runtime_context.event_sink.list_events(
            Filter(event_type_prefix="runtime.queue_item.")
        ),
        event_id=requested_event.id,
        target_agent=SESSION_TURN_AGENT_ID,
    )


def optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
