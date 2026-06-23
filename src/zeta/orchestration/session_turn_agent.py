"""Event-triggered session-turn agent."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from zeta.orchestration.agents import (
    AgentDefinition,
    AgentInvocation,
    EventPattern,
    ExecutableAgent,
)
from zeta.orchestration.dispatch import (
    EventDispatcher,
)
from zeta.orchestration.queue import terminal_queue_item_result
from zeta.records.events import Event
from zeta.run.cancellation import CancellationToken
from zeta.run.context import RuntimeContext
from zeta.run.thread_run import (
    empty_session_trace_result,
    run_session_turn,
    session_run_id,
    session_turn_requested_draft,
)

RuntimePublishedEvent = Event
CancellationEventForRun = Callable[[str], CancellationToken | None]
SESSION_TURN_AGENT_ID = "zeta.session.turn"


def session_turn_agent(
    runtime_context: RuntimeContext,
    *,
    publish_event: Callable[[RuntimePublishedEvent], None],
    cancellation_event_for_run: CancellationEventForRun | None = None,
) -> ExecutableAgent:
    async def run_agent(invocation: AgentInvocation) -> dict[str, Any]:
        params = dict(invocation.triggering_event.payload)
        run_id = invocation.triggering_event.run_id or optional_string(
            params.get("run_id")
        )
        if run_id is None:
            run_id = session_run_id()
        cancellation_event = (
            cancellation_event_for_run(run_id)
            if cancellation_event_for_run is not None
            else None
        )
        return await run_session_turn(
            params,
            run_id=run_id,
            caused_by=invocation.triggering_event.id,
            publish_event=publish_event,
            runtime_context=runtime_context,
            cancellation_event=cancellation_event,
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
    event_dispatcher: EventDispatcher,
) -> dict[str, Any]:
    run_id = run_id or session_run_id()
    draft = session_turn_requested_draft(
        params,
        run_id=run_id,
        runtime_context=runtime_context,
    )
    outcome = await event_dispatcher.publish_and_run(draft)
    result = terminal_queue_item_result(
        outcome.lifecycle_events,
        event_id=outcome.event.id,
        target_agent=SESSION_TURN_AGENT_ID,
    )
    if result is not None:
        return result
    return {
        "run_id": run_id,
        "outcome": "duplicate" if not outcome.inserted else "unhandled",
        "final_answer": "",
        "trace": empty_session_trace_result(),
    }


def optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
