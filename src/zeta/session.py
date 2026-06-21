"""Session resources for Zeta runtime calls."""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from zeta.agents.capabilities import AgentConfig
from zeta.context.builder import event_timeline_type, project_trace_events
from zeta.dispatch import EventDispatcher, RegisteredAgent, terminal_queue_item_result
from zeta.events import (
    user_message_draft,
)
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
from zeta.store.events import EventReader, Filter
from zeta.store.substrate import warn_trace_failure_once

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


SessionWorkflow = Literal["ask", "propose", "do"]


@dataclass(frozen=True)
class SessionRunParams:
    objective: str
    workflow: SessionWorkflow = "ask"
    tools: tuple[str, ...] | None = None
    context: str = ""
    system: str | None = None
    model: str | None = None
    url: str | None = None
    thinking: str | None = None
    api: str | None = None
    max_steps: int | None = None
    max_wall_seconds: float | None = None

    @classmethod
    def from_mapping(cls, params: dict[str, Any]) -> SessionRunParams:
        objective = params.get("objective")
        if objective is None or objective == "":
            raise SessionRequestError(
                "missing_objective",
                "session.run requires objective",
                {"message": "session.run requires objective"},
            )
        workflow = params["workflow"] if "workflow" in params else "ask"
        if workflow not in {"ask", "propose", "do"}:
            raise SessionRequestError(
                "invalid_workflow",
                "workflow must be ask, propose, or do",
                {
                    "message": "workflow must be ask, propose, or do",
                    "workflow": workflow,
                },
            )
        requested_tools = params.get("tools")
        tools = requested_tools if requested_tools is not None else None
        return cls(
            objective=cast(str, objective),
            workflow=cast(SessionWorkflow, workflow),
            tools=cast(tuple[str, ...] | None, tools),
            context=params.get("context", ""),
            system=params.get("system"),
            model=params.get("model"),
            url=params.get("url"),
            thinking=params.get("thinking"),
            api=params.get("api"),
            max_steps=params.get("max_steps"),
            max_wall_seconds=params.get("max_wall_seconds"),
        )

    def turn_payload(self, run_id: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "objective": self.objective,
            "workflow": self.workflow,
            "runtime": "zeta-rpc",
            "run_id": run_id,
            "tools": list(self.tools or ()),
            "context": self.context,
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
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        return payload


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


RuntimePublishedEvent = Event | DraftEvent
CancellationEventForRun = Callable[[str], CancellationToken | None]
SESSION_TURN_AGENT_ID = "zeta.session.turn"


def current_timeline(*, runtime_context: Session) -> list[Event]:
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
    runtime_context: Session,
    *,
    publish_event: Callable[[RuntimePublishedEvent], None],
    cancellation_event_for_run: CancellationEventForRun | None = None,
) -> RegisteredAgent:
    async def run_agent(invocation: AgentInvocation) -> dict[str, Any]:
        params = dict(invocation.triggering_event.payload)
        run_id = invocation.triggering_event.turn_id or optional_string(
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
    runtime_context: Session,
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
        "trace": empty_session_trace_result(),
    }


async def run_session_turn(
    params: dict[str, Any],
    *,
    run_id: str,
    caused_by: str,
    publish_event: Callable[[RuntimePublishedEvent], None],
    runtime_context: Session,
    cancellation_event: CancellationToken | None,
) -> dict[str, Any]:
    request = SessionRunParams.from_mapping(params)
    enabled_capabilities = registered_capabilities(
        request.tools,
        tool_registry=runtime_context.tool_registry,
    )
    execution_mode: ExecutionMode = "direct" if request.workflow == "do" else "stage"
    prior_timeline = current_timeline(runtime_context=runtime_context)
    user_event = record_user_message(
        {
            "type": "user_message",
            "content": request.objective,
            "workflow": request.workflow,
            "runtime": "zeta-rpc",
            "available_tools": list(enabled_capabilities),
            "run_id": run_id,
            "turn_id": run_id,
        },
        runtime_context=runtime_context,
    )
    publish_event(user_event)

    def sink(draft: DraftEvent) -> None:
        if is_runtime_ui_event(draft):
            publish_event(
                replace(
                    draft,
                    payload={**draft.payload, "run_id": run_id},
                    session_id=runtime_context.session_id,
                    turn_id=run_id,
                )
            )
            return
        persisted = record_runtime_draft(
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
        return session_result(
            "aborted",
            "",
            run_id=run_id,
            runtime_context=runtime_context,
        )
    return session_result(
        session_outcome(result.staged_effect, result.final_answer),
        result.final_answer,
        run_id=run_id,
        runtime_context=runtime_context,
    )


def session_turn_requested_draft(
    params: dict[str, Any],
    *,
    run_id: str,
    runtime_context: Session,
) -> DraftEvent:
    payload = SessionRunParams.from_mapping(params).turn_payload(run_id)
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
) -> Event:
    payload = {key: value for key, value in event.items() if key != "type"}
    outcome = runtime_context.event_sink.accept(
        user_message_draft(
            payload,
            session_id=runtime_context.session_id,
            turn_id=event.get("turn_id")
            if isinstance(event.get("turn_id"), str)
            else None,
        )
    )
    return outcome.event


def record_runtime_draft(
    draft: DraftEvent,
    *,
    runtime_context: Session,
    run_id: str,
) -> Event:
    tagged = replace(
        draft,
        payload={**draft.payload, "run_id": run_id},
        session_id=runtime_context.session_id,
        turn_id=run_id,
    )
    outcome = runtime_context.event_sink.accept(tagged)
    project_trace_for_turn(runtime_context, outcome.event.turn_id)
    return outcome.event


def project_trace_for_turn(runtime_context: Session, turn_id: str | None) -> None:
    if turn_id is None or not isinstance(runtime_context.event_sink, EventReader):
        return
    try:
        project_trace_events(
            runtime_context.event_sink.list_events(
                Filter(
                    session_id=runtime_context.session_id,
                    turn_id=turn_id,
                    event_type_prefix="zeta.",
                )
            ),
            runtime_context.trace_store,
        )
    except Exception as exc:
        warn_trace_failure_once("project_trace_for_turn", exc)


def session_result(
    outcome: str,
    final_answer: str,
    *,
    run_id: str,
    runtime_context: Session,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "run_id": run_id,
        "outcome": outcome,
        "final_answer": final_answer,
        "trace": projected_session_trace_result(runtime_context, run_id)
        if isinstance(runtime_context.event_sink, EventReader)
        else empty_session_trace_result(),
    }
    cursor = final_event_cursor(runtime_context, run_id)
    if cursor is not None:
        result["final_event_cursor"] = cursor
    return result


def projected_session_trace_result(
    runtime_context: Session,
    run_id: str,
) -> dict[str, list[str]]:
    trace = empty_session_trace_result()
    events = runtime_context.event_sink.list_events(
        Filter(
            session_id=runtime_context.session_id,
            turn_id=run_id,
            event_type_prefix="zeta.",
        )
    )
    projection = project_trace_events(events, runtime_context.trace_store)
    for event in events:
        event_type = event_timeline_type(event)
        if event_type == "model":
            add_unique(trace["model_event_ids"], event.id)
            add_unique(trace["prompt_ids"], projection.prompt_object_ids.get(event.id))
            add_unique(
                trace["assistant_message_ids"],
                projection.assistant_message_ids.get(event.id),
            )
            continue
        if event_type == "tool_call":
            add_unique(trace["tool_event_ids"], event.id)
            add_unique(
                trace["tool_call_ids"], projection.tool_call_object_ids.get(event.id)
            )
            continue
        if event_type == "tool_result":
            add_unique(trace["tool_event_ids"], event.id)
            add_unique(
                trace["tool_result_ids"],
                projection.tool_result_object_ids.get(event.id),
            )
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


def final_event_cursor(runtime_context: Session, run_id: str) -> str | None:
    if not isinstance(runtime_context.event_sink, EventReader):
        return None
    events = runtime_context.event_sink.list_events(
        Filter(session_id=runtime_context.session_id, turn_id=run_id)
    )
    if not events:
        return None
    return str(events[-1].cursor) if events[-1].cursor is not None else None


def session_agent_config(
    params: SessionRunParams,
    *,
    enabled_capabilities: tuple[str, ...],
    execution_mode: ExecutionMode,
    session_id: str,
) -> AgentConfig:
    return AgentConfig(
        system_prompt=params.system,
        allowed_capabilities=enabled_capabilities,
        max_turns=params.max_steps,
        stop_on_staged_effect=True,
        execution_mode=execution_mode,
        model_name=params.model,
        model_url=params.url,
        model_session_id=session_id,
        thinking=params.thinking,
        model_api=params.api,
        max_wall_seconds=params.max_wall_seconds,
    )


def optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def session_outcome(staged_effect: dict[str, Any] | None, final_answer: str) -> str:
    del final_answer
    if staged_effect is not None:
        return "staged"
    return "completed"


def session_run_id() -> str:
    return f"run_{uuid.uuid4().hex}"
