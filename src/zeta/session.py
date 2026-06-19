"""Session resources for Zeta runtime calls."""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zeta.agents.capabilities import AgentConfig
from zeta.capabilities.base import ExecutionMode
from zeta.dispatch import AgentDefinition, AgentRun, AsyncEventDispatcher, TriggerRule
from zeta.events import DraftEvent, Event
from zeta.loop import (
    AgentTurnAborted,
    AgentTurnResult,
    CancellationToken,
    async_run_agent_turn,
    is_runtime_ui_event,
    registered_capabilities,
)
from zeta.store.events import EventReader, Filter
from zeta.timeline import current_timeline, timeline_event_from_durable_event

if TYPE_CHECKING:
    from zeta.capabilities.registry import CapabilityRegistry
    from zeta.store.events import EventStoreProtocol
    from zeta.store.substrate import Store


@dataclass(frozen=True)
class Session:
    """Runtime dependencies for one Zeta host/session."""

    session_id: str
    event_sink: EventStoreProtocol
    trace_store: Store
    tool_registry: CapabilityRegistry
    state_dir: Path
    session_dir: Path


@dataclass
class SessionRequestError(ValueError):
    """Raised when a session-level request cannot be converted into a turn."""

    code: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)


def default_session() -> Session:
    """Return the default process session for pure Zeta runtime calls."""
    state_dir = zeta_state_dir()
    session_id = os.environ.get("ZETA_SESSION_ID") or "default"
    return session_for_id(
        session_id=session_id,
        state_dir=state_dir,
        session_dir=state_dir / "sessions" / session_id,
    )


def session_for_id(
    *,
    session_id: str,
    state_dir: Path,
    session_dir: Path,
    tool_registry: CapabilityRegistry | None = None,
) -> Session:
    """Build the default Zeta runtime dependencies for one session."""
    from zeta.store.events import SqliteEventStore, event_store_path
    from zeta.store.substrate import SqliteStore, zeta_sqlite_path

    if tool_registry is None:
        from zeta.capabilities.registry import registry as tool_registry

    return Session(
        session_id=session_id,
        event_sink=SqliteEventStore(event_store_path(state_dir)),
        trace_store=SqliteStore(zeta_sqlite_path(state_dir), session_id=session_id),
        tool_registry=tool_registry,
        state_dir=state_dir,
        session_dir=session_dir,
    )


def zeta_state_dir() -> Path:
    root = os.environ.get("ZETA_STATE_DIR")
    return Path(root).expanduser() if root else Path.home() / ".zeta"


async def run_session_turn_from_event(
    run: AgentRun,
    *,
    runtime_context: Session,
    publish_event: Callable[[dict[str, Any]], None],
    cancellation_event: CancellationToken | None = None,
) -> dict[str, Any]:
    params = dict(run.triggering_event.payload)
    run_id = run.triggering_event.turn_id or optional_string(params.get("run_id"))
    if run_id is None:
        run_id = session_run_id()
    return await run_session_turn(
        params,
        run_id=run_id,
        caused_by=run.triggering_event.id,
        publish_event=publish_event,
        runtime_context=runtime_context,
        cancellation_event=cancellation_event,
    )


async def run_session_turn(
    params: dict[str, Any],
    *,
    run_id: str,
    caused_by: str,
    publish_event: Callable[[dict[str, Any]], None],
    runtime_context: Session,
    cancellation_event: CancellationToken | None,
) -> dict[str, Any]:
    objective = session_objective(params)
    workflow = session_workflow(params)
    enabled_capabilities = registered_capabilities(
        session_allowed_tools(params),
        tool_registry=runtime_context.tool_registry,
    )
    execution_mode: ExecutionMode = "direct" if workflow == "do" else "stage"
    prior_timeline = current_timeline(runtime_context=runtime_context)
    user_event = record_user_message(
        {
            "type": "user_message",
            "content": objective,
            "workflow": workflow,
            "runtime": "zeta-rpc",
            "available_tools": list(enabled_capabilities),
            "run_id": run_id,
            "turn_id": run_id,
        },
        runtime_context=runtime_context,
    )
    publish_event(session_event_with_cursor(runtime_context, user_event, run_id))

    def sink(draft: DraftEvent) -> None:
        if is_runtime_ui_event(draft):
            publish_event(
                session_event_with_cursor(
                    runtime_context,
                    live_runtime_event(
                        draft, runtime_context=runtime_context, run_id=run_id
                    ),
                    run_id,
                )
            )
            return
        persisted = record_runtime_draft(
            draft,
            runtime_context=runtime_context,
            run_id=run_id,
        )
        publish_event(session_event_with_cursor(runtime_context, persisted, run_id))

    try:
        result = await async_run_agent_turn(
            objective,
            prior_timeline,
            session_agent_config(
                params,
                enabled_capabilities=enabled_capabilities,
                execution_mode=execution_mode,
                session_id=runtime_context.session_id,
            ),
            context=session_context(params),
            event_sink=sink,
            trace_store=runtime_context.trace_store,
            tool_registry=runtime_context.tool_registry,
            caused_by=caused_by,
            cancellation_event=cancellation_event,
        )
    except AgentTurnAborted as exc:
        return session_result(
            "aborted",
            "",
            run_id=run_id,
            runtime_context=runtime_context,
            agent_result=exc.result,
        )
    return session_result(
        session_outcome(result.staged_effect, result.final_text),
        result.final_text,
        run_id=run_id,
        runtime_context=runtime_context,
        agent_result=result,
    )


def session_event_dispatcher(
    runtime_context: Session,
    *,
    publish_event: Callable[[dict[str, Any]], None],
    cancellation_event: CancellationToken | None = None,
) -> AsyncEventDispatcher:
    return AsyncEventDispatcher(
        runtime_context.event_sink,
        agents=[
            AgentDefinition(
                "zeta.session.turn",
                TriggerRule(event_type="session.turn.requested"),
                run=lambda run: run_session_turn_from_event(
                    run,
                    runtime_context=runtime_context,
                    publish_event=publish_event,
                    cancellation_event=cancellation_event,
                ),
            )
        ],
        publish_event=lambda event: publish_event(
            session_event_from_durable_event(event)
        ),
    )


def session_turn_requested_draft(
    params: dict[str, Any],
    *,
    run_id: str,
    runtime_context: Session,
) -> DraftEvent:
    objective = session_objective(params)
    workflow = session_workflow(params)
    payload: dict[str, Any] = {
        "objective": objective,
        "workflow": workflow,
        "runtime": "zeta-rpc",
        "run_id": run_id,
        "tools": list(session_allowed_tools(params) or ()),
        "context": session_context(params),
    }
    for key in (
        "system",
        "model",
        "url",
        "thinking",
        "api",
        "max_steps",
        "max_wall_seconds",
    ):
        value = params.get(key)
        if isinstance(value, str | int | float) and not isinstance(value, bool):
            payload[key] = value
    return DraftEvent(
        "session.turn.requested",
        "zeta",
        payload,
        idempotency_key=f"session.turn.requested:{run_id}",
        session_id=runtime_context.session_id,
        turn_id=run_id,
    )


def record_user_message(
    event: dict[str, Any],
    *,
    runtime_context: Session,
) -> dict[str, Any]:
    payload = {key: value for key, value in event.items() if key != "type"}
    payload["_timeline_type"] = "user_message"
    outcome = runtime_context.event_sink.accept(
        DraftEvent(
            event_type="zeta.user_message",
            source="zeta",
            payload=payload,
            idempotency_key=None,
            caused_by=None,
            session_id=runtime_context.session_id,
            turn_id=event.get("turn_id")
            if isinstance(event.get("turn_id"), str)
            else None,
        )
    )
    return timeline_event_from_durable_event(outcome.event)


def record_runtime_draft(
    draft: DraftEvent,
    *,
    runtime_context: Session,
    run_id: str,
) -> dict[str, Any]:
    tagged = replace(
        draft,
        payload={**draft.payload, "run_id": run_id},
        session_id=runtime_context.session_id,
        turn_id=run_id,
    )
    outcome = runtime_context.event_sink.accept(tagged)
    return timeline_event_from_durable_event(outcome.event)


def live_runtime_event(
    draft: DraftEvent,
    *,
    runtime_context: Session,
    run_id: str,
) -> dict[str, Any]:
    event = draft_timeline_event(
        replace(
            draft,
            payload={**draft.payload, "run_id": run_id},
            session_id=runtime_context.session_id,
            turn_id=run_id,
        )
    )
    event["session"] = runtime_context.session_id
    event["run_id"] = run_id
    event["turn_id"] = run_id
    return event


def session_result(
    outcome: str,
    final_text: str,
    *,
    run_id: str,
    runtime_context: Session,
    agent_result: AgentTurnResult | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "run_id": run_id,
        "outcome": outcome,
        "final_text": final_text,
        "trace": session_trace_result(agent_result),
    }
    cursor = final_event_cursor(runtime_context, run_id)
    if cursor is not None:
        result["final_event_cursor"] = cursor
    return result


def session_trace_result(agent_result: AgentTurnResult | None) -> dict[str, list[str]]:
    trace = empty_session_trace_result()
    if agent_result is None:
        return trace
    for prompt_trace in agent_result.prompt_traces:
        add_unique(trace["prompt_ids"], prompt_trace.prompt_object_id)
        add_unique(
            trace["assistant_message_ids"],
            prompt_trace.assistant_message_object_id,
        )
    for draft in agent_result.events:
        event = draft_timeline_event(draft)
        event_type = str(event.get("type") or "")
        if event_type == "model":
            add_unique(trace["model_event_ids"], event.get("id"))
            add_unique_list(trace["tool_call_ids"], event.get("tool_call_object_ids"))
            continue
        if event_type == "tool_call":
            add_unique(trace["tool_event_ids"], event.get("id"))
            add_unique(trace["tool_call_ids"], event.get("tool_call_object_id"))
            continue
        if event_type == "tool_result":
            add_unique(trace["tool_event_ids"], event.get("id"))
            add_unique(trace["tool_call_ids"], event.get("tool_call_object_id"))
            add_unique(trace["tool_result_ids"], event.get("tool_result_object_id"))
    return trace


def empty_session_trace_result() -> dict[str, list[str]]:
    return {
        "prompt_ids": [],
        "assistant_message_ids": [],
        "model_event_ids": [],
        "tool_event_ids": [],
        "tool_call_ids": [],
        "tool_result_ids": [],
    }


def add_unique(values: list[str], value: Any) -> None:
    if isinstance(value, str) and value and value not in values:
        values.append(value)


def add_unique_list(values: list[str], raw_values: Any) -> None:
    if not isinstance(raw_values, list | tuple):
        return
    for value in raw_values:
        add_unique(values, value)


def draft_timeline_event(draft: DraftEvent) -> dict[str, Any]:
    event = Event(
        id=draft_event_id(draft) or f"evt_{uuid.uuid4().hex}",
        event_type=draft.event_type,
        source=draft.source,
        payload=dict(draft.payload),
        idempotency_key=draft.idempotency_key,
        caused_by=draft.caused_by,
        session_id=draft.session_id,
        turn_id=draft.turn_id,
        timestamp_micros=time.time_ns() // 1_000,
    )
    return timeline_event_from_durable_event(event)


def draft_event_id(draft: DraftEvent) -> str | None:
    key = draft.idempotency_key
    prefix = f"{draft.event_type}:"
    if key is None or not key.startswith(prefix):
        return None
    event_id = key[len(prefix) :].strip()
    return event_id or None


def final_event_cursor(runtime_context: Session, run_id: str) -> str | None:
    if not isinstance(runtime_context.event_sink, EventReader):
        return None
    events = runtime_context.event_sink.list_events(
        Filter(session_id=runtime_context.session_id, turn_id=run_id)
    )
    if not events:
        return None
    return str(events[-1].seq)


def session_event_with_cursor(
    runtime_context: Session,
    event: dict[str, Any],
    run_id: str,
) -> dict[str, Any]:
    durable_event = durable_event_for_session_event(runtime_context, event, run_id)
    if durable_event is None:
        return event
    return session_event_from_durable_event(durable_event)


def durable_event_for_session_event(
    runtime_context: Session,
    event: dict[str, Any],
    run_id: str,
) -> Event | None:
    if not isinstance(runtime_context.event_sink, EventReader):
        return None
    event_id = event.get("id")
    if not isinstance(event_id, str):
        return None
    events = runtime_context.event_sink.list_events(
        Filter(session_id=runtime_context.session_id, turn_id=run_id)
    )
    for durable_event in events:
        if durable_event.id == event_id:
            return durable_event
    return None


def session_event_from_durable_event(event: Event) -> dict[str, Any]:
    projected = timeline_event_from_durable_event(event)
    if not projected:
        projected = generic_session_event_from_durable_event(event)
    projected["cursor"] = str(event.seq)
    return projected


def generic_session_event_from_durable_event(event: Event) -> dict[str, Any]:
    projected = {
        "type": event.event_type,
        "id": event.id,
        "source": event.source,
        "time": event.timestamp_micros / 1_000_000,
        **event.payload,
    }
    if event.session_id is not None:
        projected["session"] = event.session_id
    if event.turn_id is not None:
        projected["turn_id"] = event.turn_id
    if event.caused_by is not None:
        projected["caused_by"] = event.caused_by
    return projected


def session_objective(params: dict[str, Any]) -> str:
    objective = str(params.get("objective") or "")
    if not objective:
        raise SessionRequestError(
            "missing_objective",
            "session.run requires objective",
            {"message": "session.run requires objective"},
        )
    return objective


def session_workflow(params: dict[str, Any]) -> str:
    workflow = str(params.get("workflow") or "ask")
    if workflow not in {"ask", "propose", "do"}:
        raise SessionRequestError(
            "invalid_workflow",
            "workflow must be ask, propose, or do",
            {
                "message": "workflow must be ask, propose, or do",
                "workflow": workflow,
            },
        )
    return workflow


def session_allowed_tools(params: dict[str, Any]) -> tuple[str, ...] | None:
    requested_tools = params.get("tools")
    if not isinstance(requested_tools, list):
        return None
    return tuple(str(tool) for tool in requested_tools if isinstance(tool, str))


def session_agent_config(
    params: dict[str, Any],
    *,
    enabled_capabilities: tuple[str, ...],
    execution_mode: ExecutionMode,
    session_id: str,
) -> AgentConfig:
    return AgentConfig(
        system_prompt=optional_str_param(params, "system"),
        allowed_capabilities=enabled_capabilities,
        max_turns=params.get("max_steps")
        if isinstance(params.get("max_steps"), int)
        else None,
        stop_on_staged_effect=True,
        execution_mode=execution_mode,
        model_name=optional_str_param(params, "model"),
        model_url=optional_str_param(params, "url"),
        model_session_id=session_id,
        thinking=optional_str_param(params, "thinking"),
        model_api=optional_str_param(params, "api"),
        max_wall_seconds=optional_float_param(params, "max_wall_seconds"),
    )


def session_context(params: dict[str, Any]) -> str:
    context = params.get("context")
    return str(context) if isinstance(context, str) else ""


def optional_str_param(params: dict[str, Any], key: str) -> str | None:
    value = params.get(key)
    return value if isinstance(value, str) else None


def optional_float_param(params: dict[str, Any], key: str) -> float | None:
    value = params.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def session_outcome(staged_effect: dict[str, Any] | None, final_text: str) -> str:
    if staged_effect is not None:
        return "staged"
    if final_text:
        return "answered"
    return "completed"


def session_run_id() -> str:
    return f"run_{uuid.uuid4().hex}"
