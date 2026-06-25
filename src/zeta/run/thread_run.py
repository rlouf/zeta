"""Run one request inside a durable thread."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from zeta.capabilities.types import ExecutionMode
from zeta.records.events import (
    DraftEvent,
    Event,
)
from zeta.run.config import AgentConfig
from zeta.run.context import RuntimeContext
from zeta.run.runtime import (
    AgentRunAborted,
    AgentRunRequest,
    CancellationToken,
    final_event_cursor,
    run_agent,
    session_trace_result,
)
from zeta.run.runtime import (
    current_timeline as runtime_current_timeline,
)

RuntimePublishedEvent = Event


def current_timeline(*, runtime_context: RuntimeContext) -> list[Event]:
    return runtime_current_timeline(runtime_context=runtime_context)


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


def session_agent_request(params: dict[str, Any]) -> AgentRunRequest:
    request = session_run_params(params)
    execution_mode: ExecutionMode = "direct" if request.workflow == "do" else "stage"
    return AgentRunRequest(
        objective=request.objective,
        workflow=request.workflow,
        runtime="zeta-rpc",
        tools=tuple(request.tools or ()),
        context=request.context,
        config=AgentConfig(
            system_prompt=request.system,
            max_turns=request.max_steps,
            stop_on_staged_effect=True,
            execution_mode=execution_mode,
            model_name=request.model,
            model_url=request.url,
            thinking=request.thinking,
            model_api=request.api,
            max_wall_seconds=request.max_wall_seconds,
        ),
    )


async def run_session_request(
    params: dict[str, Any],
    *,
    run_id: str,
    caused_by: str,
    publish_event: Callable[[RuntimePublishedEvent], None],
    runtime_context: RuntimeContext,
    cancellation_event: CancellationToken | None,
) -> dict[str, Any]:
    try:
        result = await run_agent(
            session_agent_request(params),
            run_id=run_id,
            caused_by=caused_by,
            publish_event=publish_event,
            runtime_context=runtime_context,
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
    runtime_context: RuntimeContext,
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


def _session_result(
    outcome: str,
    final_answer: str,
    *,
    run_id: str,
    runtime_context: RuntimeContext,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "run_id": run_id,
        "outcome": outcome,
        "final_answer": final_answer,
        "trace": session_trace_result(runtime_context, run_id),
    }
    cursor = final_event_cursor(runtime_context, run_id)
    if cursor is not None:
        result["final_event_cursor"] = cursor
    return result


def _session_outcome(staged_effect: dict[str, Any] | None, final_answer: str) -> str:
    del final_answer
    if staged_effect is not None:
        return "staged"
    return "completed"


def session_run_id() -> str:
    return f"run_{uuid.uuid4().hex}"
