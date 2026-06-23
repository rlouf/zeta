"""Run one request inside a durable thread."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from zeta.capabilities.types import ExecutionMode
from zeta.records.events import (
    DraftEvent,
    Event,
    event_timeline_type,
    user_message_draft,
)
from zeta.records.provenance import project_trace_events
from zeta.records.stores import EventReader, Filter, warn_trace_failure_once
from zeta.run.config import AgentConfig
from zeta.run.runtime import (
    AgentRunAborted,
    CancellationToken,
    is_runtime_ui_event,
    registered_capabilities,
    run_agent,
)
from zeta.run.threads import SessionScope

RuntimePublishedEvent = Event


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
    tools: list[str] | None = None
    context: str = ""
    system: str | None = None
    model: str | None = None
    url: str | None = None
    thinking: str | None = None
    api: str | None = None
    max_steps: int | None = None
    max_wall_seconds: float | None = None

    def run_payload(self, run_id: str) -> dict[str, Any]:
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


def session_run_params(params: dict[str, Any]) -> SessionRunParams:
    """Construct validated session run params without reviving mapping parser methods."""

    try:
        request = SessionRunParams(**params)
    except TypeError as exc:
        raise SessionRequestError(
            "invalid_params",
            f"SessionRunParams parameters are invalid: {exc}",
            {"message": f"SessionRunParams parameters are invalid: {exc}"},
        ) from exc
    if not request.objective:
        raise SessionRequestError(
            "missing_objective",
            "session.run requires objective",
            {"message": "session.run requires objective"},
        )
    if request.workflow not in {"ask", "propose", "do"}:
        raise SessionRequestError(
            "invalid_workflow",
            "workflow must be ask, propose, or do",
            {
                "message": "workflow must be ask, propose, or do",
                "workflow": request.workflow,
            },
        )
    if request.tools is not None:
        for tool in request.tools:
            if not isinstance(tool, str) or not tool:
                raise SessionRequestError(
                    "invalid_tools",
                    "tools must contain non-empty strings",
                    {"message": "tools must contain non-empty strings"},
                )
    return request


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
        persisted = _record_runtime_event(
            draft,
            runtime_context=runtime_context,
            run_id=run_id,
        )
        publish_event(persisted)

    try:
        result = await run_agent(
            request.objective,
            prior_timeline,
            AgentConfig(
                system_prompt=request.system,
                allowed_capabilities=enabled_capabilities,
                max_turns=request.max_steps,
                stop_on_staged_effect=True,
                execution_mode=execution_mode,
                model_name=request.model,
                model_url=request.url,
                model_session_id=runtime_context.session_id,
                thinking=request.thinking,
                model_api=request.api,
                max_wall_seconds=request.max_wall_seconds,
            ),
            context=request.context,
            event_sink=sink,
            trace_store=runtime_context.trace_store,
            tool_registry=runtime_context.tool_registry,
            caused_by=caused_by,
            cancellation_event=cancellation_event,
        )
    except AgentRunAborted:
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


def _record_runtime_event(
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
    _record_trace_for_run(runtime_context, outcome.event.run_id)
    return outcome.event


def _record_trace_for_run(runtime_context: SessionScope, run_id: str | None) -> None:
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
        warn_trace_failure_once("record_trace_for_run", exc)


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
        "trace": _session_trace_result(runtime_context, run_id)
        if isinstance(runtime_context.event_sink, EventReader)
        else empty_session_trace_result(),
    }
    cursor = _final_event_cursor(runtime_context, run_id)
    if cursor is not None:
        result["final_event_cursor"] = cursor
    return result


def _session_trace_result(
    runtime_context: SessionScope,
    run_id: str,
) -> dict[str, list[str]]:
    trace = empty_session_trace_result()
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


def empty_session_trace_result() -> dict[str, list[str]]:
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


def _session_outcome(staged_effect: dict[str, Any] | None, final_answer: str) -> str:
    del final_answer
    if staged_effect is not None:
        return "staged"
    return "completed"


def session_run_id() -> str:
    return f"run_{uuid.uuid4().hex}"
