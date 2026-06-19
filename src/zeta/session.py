"""Session resources for Zeta runtime calls."""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from zeta.agents.capabilities import AgentConfig
from zeta.capabilities.base import ExecutionMode
from zeta.context.builder import event_timeline_type, project_trace_events
from zeta.dispatch import AgentDefinition, AgentRun, AsyncEventDispatcher, TriggerRule
from zeta.events import (
    DraftEvent,
    Event,
    draft_event_id,
    user_message_draft,
)
from zeta.loop import (
    AgentTurnAborted,
    AgentTurnResult,
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
        objective = str(params.get("objective") or "")
        if not objective:
            raise SessionRequestError(
                "missing_objective",
                "session.run requires objective",
                {"message": "session.run requires objective"},
            )
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
        requested_tools = params.get("tools")
        tools = (
            tuple(tool for tool in requested_tools if isinstance(tool, str))
            if isinstance(requested_tools, list)
            else None
        )
        return cls(
            objective=objective,
            workflow=cast(SessionWorkflow, workflow),
            tools=tools,
            context=str(params["context"])
            if isinstance(params.get("context"), str)
            else "",
            system=optional_string(params.get("system")),
            model=optional_string(params.get("model")),
            url=optional_string(params.get("url")),
            thinking=optional_string(params.get("thinking")),
            api=optional_string(params.get("api")),
            max_steps=params.get("max_steps")
            if isinstance(params.get("max_steps"), int)
            else None,
            max_wall_seconds=optional_float(params.get("max_wall_seconds")),
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


async def run_session_turn_from_event(
    run: AgentRun,
    *,
    runtime_context: Session,
    publish_event: Callable[[RuntimePublishedEvent], None],
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
                live_runtime_event(
                    draft,
                    runtime_context=runtime_context,
                    run_id=run_id,
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
    publish_event: Callable[[RuntimePublishedEvent], None],
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
        publish_event=publish_event,
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


def live_runtime_event(
    draft: DraftEvent,
    *,
    runtime_context: Session,
    run_id: str,
) -> DraftEvent:
    return replace(
        draft,
        payload={**draft.payload, "run_id": run_id},
        session_id=runtime_context.session_id,
        turn_id=run_id,
    )


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
        "trace": session_trace_result(
            agent_result,
            runtime_context=runtime_context,
            run_id=run_id,
        ),
    }
    cursor = final_event_cursor(runtime_context, run_id)
    if cursor is not None:
        result["final_event_cursor"] = cursor
    return result


def session_trace_result(
    agent_result: AgentTurnResult | None,
    *,
    runtime_context: Session | None = None,
    run_id: str | None = None,
) -> dict[str, list[str]]:
    trace = empty_session_trace_result()
    if (
        runtime_context is not None
        and run_id is not None
        and isinstance(runtime_context.event_sink, EventReader)
    ):
        return projected_session_trace_result(runtime_context, run_id)
    if agent_result is None:
        return trace
    for prompt_trace in agent_result.prompt_traces:
        add_unique(trace["prompt_ids"], prompt_trace.prompt_object_id)
        add_unique(
            trace["assistant_message_ids"],
            prompt_trace.assistant_message_object_id,
        )
    for draft in agent_result.events:
        event_type = draft_timeline_type(draft)
        if event_type == "model":
            add_unique(trace["model_event_ids"], draft_event_id(draft))
            add_unique_list(
                trace["tool_call_ids"], draft_object_ids(draft, "tool_call")
            )
            continue
        if event_type == "tool_call":
            add_unique(trace["tool_event_ids"], draft_event_id(draft))
            add_unique_list(
                trace["tool_call_ids"], draft_object_ids(draft, "tool_call")
            )
            continue
        if event_type == "tool_result":
            add_unique(trace["tool_event_ids"], draft_event_id(draft))
            add_unique_list(
                trace["tool_call_ids"], draft_object_ids(draft, "tool_call")
            )
            add_unique_list(
                trace["tool_result_ids"], draft_object_ids(draft, "tool_result")
            )
    return trace


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
        add_projected_event_trace(trace, event, projection)
    return trace


def add_projected_event_trace(
    trace: dict[str, list[str]],
    event: Event,
    projection: Any,
) -> None:
    event_type = event_timeline_type(event)
    if event_type == "model":
        add_projected_model_trace(trace, event, projection)
        return
    if event_type == "tool_call":
        add_unique(trace["tool_event_ids"], event.id)
        add_unique(
            trace["tool_call_ids"], projection.tool_call_object_ids.get(event.id)
        )
        return
    if event_type == "tool_result":
        add_unique(trace["tool_event_ids"], event.id)
        add_unique(
            trace["tool_result_ids"],
            projection.tool_result_object_ids.get(event.id),
        )


def add_projected_model_trace(
    trace: dict[str, list[str]],
    event: Event,
    projection: Any,
) -> None:
    add_unique(trace["model_event_ids"], event.id)
    add_unique(trace["prompt_ids"], projection.prompt_object_ids.get(event.id))
    add_unique(
        trace["assistant_message_ids"],
        projection.assistant_message_ids.get(event.id),
    )


def draft_timeline_type(draft: DraftEvent) -> str:
    view_type = draft.payload.get("_timeline_type")
    if isinstance(view_type, str) and view_type:
        return view_type
    prefix = "zeta."
    if draft.event_type.startswith(prefix):
        return draft.event_type[len(prefix) :]
    return draft.event_type


def draft_object_ids(draft: DraftEvent, kind: str) -> list[str]:
    object_ids: list[str] = []
    for collection in ("used_objects", "returned_objects"):
        links = draft.payload.get(collection)
        if not isinstance(links, list):
            continue
        for link in links:
            if not isinstance(link, dict):
                continue
            if link.get("kind") != kind:
                continue
            object_id = link.get("id")
            if isinstance(object_id, str):
                object_ids.append(object_id)
    return object_ids


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
    return str(events[-1].seq)


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


def optional_float(value: Any) -> float | None:
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
