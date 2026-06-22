"""Session-turn execution for Zeta runtime calls."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from zeta.context.builder import event_timeline_type, project_trace_events
from zeta.dispatch import EventDispatcher, RegisteredAgent, terminal_queue_item_result
from zeta.events import user_message_draft
from zeta.kernel.agents import AgentDefinition, AgentInvocation, EventPattern
from zeta.kernel.capabilities import ExecutionMode
from zeta.kernel.events import DraftEvent, Event
from zeta.loop import (
    AgentTurnAborted,
    CancellationToken,
    async_run_agent_turn,
    is_runtime_ui_event,
    registered_capabilities,
)
from zeta.runtime.config import session_agent_config
from zeta.runtime.requests import session_run_params
from zeta.runtime.scope import SessionScope
from zeta.store.events import EventReader, Filter
from zeta.store.substrate import warn_trace_failure_once

RuntimePublishedEvent = Event
CancellationEventForRun = Callable[[str], CancellationToken | None]
SESSION_TURN_AGENT_ID = "zeta.session.turn"


def current_timeline(*, runtime_context: SessionScope) -> list[Event]:
    try:
        if not isinstance(runtime_context.event_sink, EventReader):
            return []
        return runtime_context.event_sink.list_events(
            Filter(
                session_id=runtime_context.session_id,
                event_type_prefix="zeta.",
            )
        )
    except Exception as exc:
        warn_trace_failure_once("current_timeline", exc)
        return []


def session_turn_agent(
    runtime_context: SessionScope,
    *,
    publish_event: Callable[[RuntimePublishedEvent], None],
    cancellation_event_for_run: CancellationEventForRun | None = None,
) -> RegisteredAgent:
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

    return RegisteredAgent(
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
    runtime_context: SessionScope,
    event_dispatcher: EventDispatcher,
) -> dict[str, Any]:
    run_id = run_id or session_run_id()
    draft = session_turn_requested_draft(
        params,
        run_id=run_id,
        runtime_context=runtime_context,
    )
    outcome = await event_dispatcher.publish_event(draft)
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
        "trace": _empty_session_trace_result(),
    }


async def run_session_turn(
    params: dict[str, Any],
    *,
    run_id: str,
    caused_by: str,
    publish_event: Callable[[RuntimePublishedEvent], None],
    runtime_context: SessionScope,
    cancellation_event: CancellationToken | None,
) -> dict[str, Any]:
    request = session_run_params(params)
    enabled_capabilities = registered_capabilities(
        request.tools,
        tool_registry=runtime_context.tool_registry,
    )
    execution_mode: ExecutionMode = "direct" if request.workflow == "do" else "stage"
    prior_timeline = current_timeline(runtime_context=runtime_context)
    user_event = _record_user_message(
        {
            "type": "user_message",
            "content": request.objective,
            "workflow": request.workflow,
            "runtime": "zeta-rpc",
            "available_tools": list(enabled_capabilities),
            "run_id": run_id,
        },
        runtime_context=runtime_context,
        run_id=run_id,
    )
    publish_event(user_event)

    def sink(draft: DraftEvent) -> None:
        if is_runtime_ui_event(draft):
            return
        persisted = _record_runtime_draft(
            draft,
            runtime_context=runtime_context,
            run_id=run_id,
        )
        publish_event(persisted)

    try:
        result = await async_run_agent_turn(
            request.objective,
            prior_timeline,
            session_agent_config(
                request,
                enabled_capabilities=enabled_capabilities,
                execution_mode=execution_mode,
                session_id=runtime_context.session_id,
            ),
            context=request.context,
            event_sink=sink,
            trace_store=runtime_context.trace_store,
            tool_registry=runtime_context.tool_registry,
            caused_by=caused_by,
            cancellation_event=cancellation_event,
        )
    except AgentTurnAborted:
        return _session_result(
            "aborted",
            "",
            run_id=run_id,
            runtime_context=runtime_context,
        )
    return _session_result(
        _session_outcome(result.staged_effect, result.final_answer),
        result.final_answer,
        run_id=run_id,
        runtime_context=runtime_context,
    )


def session_turn_requested_draft(
    params: dict[str, Any],
    *,
    run_id: str,
    runtime_context: SessionScope,
) -> DraftEvent:
    payload = session_run_params(params).run_payload(run_id)
    return DraftEvent(
        "session.turn.requested",
        "zeta",
        payload,
        idempotency_key=f"session.turn.requested:{run_id}",
        session_id=runtime_context.session_id,
        run_id=run_id,
    )


def _record_user_message(
    event: dict[str, Any],
    *,
    runtime_context: SessionScope,
    run_id: str | None = None,
) -> Event:
    payload = {key: value for key, value in event.items() if key != "type"}
    outcome = runtime_context.event_sink.accept(
        user_message_draft(
            payload,
            session_id=runtime_context.session_id,
            run_id=run_id,
            turn_id=event.get("turn_id")
            if isinstance(event.get("turn_id"), str)
            else None,
        )
    )
    return outcome.event


def _record_runtime_draft(
    draft: DraftEvent,
    *,
    runtime_context: SessionScope,
    run_id: str,
) -> Event:
    tagged = replace(
        draft,
        payload={**draft.payload, "run_id": run_id},
        session_id=runtime_context.session_id,
        run_id=run_id,
    )
    outcome = runtime_context.event_sink.accept(tagged)
    _project_trace_for_run(runtime_context, outcome.event.run_id)
    return outcome.event


def _project_trace_for_run(runtime_context: SessionScope, run_id: str | None) -> None:
    if run_id is None or not isinstance(runtime_context.event_sink, EventReader):
        return
    try:
        project_trace_events(
            runtime_context.event_sink.list_events(
                Filter(
                    session_id=runtime_context.session_id,
                    run_id=run_id,
                    event_type_prefix="zeta.",
                )
            ),
            runtime_context.trace_store,
        )
    except Exception as exc:
        warn_trace_failure_once("project_trace_for_run", exc)


def _session_result(
    outcome: str,
    final_answer: str,
    *,
    run_id: str,
    runtime_context: SessionScope,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "run_id": run_id,
        "outcome": outcome,
        "final_answer": final_answer,
        "trace": _projected_session_trace_result(runtime_context, run_id)
        if isinstance(runtime_context.event_sink, EventReader)
        else _empty_session_trace_result(),
    }
    cursor = _final_event_cursor(runtime_context, run_id)
    if cursor is not None:
        result["final_event_cursor"] = cursor
    return result


def _projected_session_trace_result(
    runtime_context: SessionScope,
    run_id: str,
) -> dict[str, list[str]]:
    trace = _empty_session_trace_result()
    events = runtime_context.event_sink.list_events(
        Filter(
            session_id=runtime_context.session_id,
            run_id=run_id,
            event_type_prefix="zeta.",
        )
    )
    projection = project_trace_events(events, runtime_context.trace_store)
    for event in events:
        event_type = event_timeline_type(event)
        if event_type == "model":
            _add_unique(trace["model_event_ids"], event.id)
            _add_unique(trace["prompt_ids"], projection.prompt_object_ids.get(event.id))
            _add_unique(
                trace["assistant_message_ids"],
                projection.assistant_message_ids.get(event.id),
            )
            continue
        if event_type == "tool_call":
            _add_unique(trace["tool_event_ids"], event.id)
            _add_unique(
                trace["tool_call_ids"], projection.tool_call_object_ids.get(event.id)
            )
            continue
        if event_type == "tool_result":
            _add_unique(trace["tool_event_ids"], event.id)
            _add_unique(
                trace["tool_result_ids"],
                projection.tool_result_object_ids.get(event.id),
            )
    return trace


def _empty_session_trace_result() -> dict[str, list[str]]:
    return {
        "prompt_ids": [],
        "assistant_message_ids": [],
        "model_event_ids": [],
        "tool_event_ids": [],
        "tool_call_ids": [],
        "tool_result_ids": [],
    }


def _add_unique(values: list[str], value: Any) -> None:
    if isinstance(value, str) and value and value not in values:
        values.append(value)


def _final_event_cursor(runtime_context: SessionScope, run_id: str) -> str | None:
    if not isinstance(runtime_context.event_sink, EventReader):
        return None
    events = runtime_context.event_sink.list_events(
        Filter(session_id=runtime_context.session_id, run_id=run_id)
    )
    if not events:
        return None
    return str(events[-1].cursor) if events[-1].cursor is not None else None


def optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _session_outcome(staged_effect: dict[str, Any] | None, final_answer: str) -> str:
    del final_answer
    if staged_effect is not None:
        return "staged"
    return "completed"


def session_run_id() -> str:
    return f"run_{uuid.uuid4().hex}"
