"""Run one request inside a durable thread."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from zeta import ids
from zeta.capabilities.types import ExecutionMode
from zeta.events import DraftEvent, Event
from zeta.loop.cancellation import AgentRunAborted, CancellationToken
from zeta.loop.config import AgentConfig
from zeta.loop.request import AgentRunRequest
from zeta.loop.runtime import (
    current_timeline as runtime_current_timeline,
)
from zeta.loop.runtime import (
    final_event_cursor,
    run_agent,
    session_trace_result,
)
from zeta.loop.runtime_context import RuntimeContext
from zeta.models.profiles import active_model_selection

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
    runtime: str | None = None
    run_id: str | None = None
    idempotency_key: str | None = None
    tools: list[str] | None = None
    context: str = ""
    system: str | None = None
    fresh: bool = False
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
            "fresh",
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
    if request.idempotency_key is not None and (
        not isinstance(request.idempotency_key, str) or not request.idempotency_key
    ):
        raise SessionRequestError(
            "invalid_idempotency_key",
            "idempotency_key must be a non-empty string",
            {"message": "idempotency_key must be a non-empty string"},
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
        fresh=request.fresh,
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


def session_agent_request_for_context(
    params: dict[str, Any],
    *,
    runtime_context: RuntimeContext,
) -> AgentRunRequest:
    """Return a session request with the context's active model defaults."""
    request = session_agent_request(params)
    config = request.config
    if config.model_name is not None or config.model_url is not None:
        return request
    selection = active_model_selection(session_dir=runtime_context.session_dir)
    if selection is None:
        return request
    return AgentRunRequest(
        objective=request.objective,
        workflow=request.workflow,
        runtime=request.runtime,
        tools=request.tools,
        context=request.context,
        fresh=request.fresh,
        config=AgentConfig(
            system_prompt=config.system_prompt,
            max_turns=config.max_turns,
            stop_on_staged_effect=config.stop_on_staged_effect,
            execution_mode=config.execution_mode,
            model_profile=selection.profile,
            model_name=selection.model,
            model_url=selection.url,
            thinking=selection.thinking,
            model_api=selection.api,
            max_wall_seconds=config.max_wall_seconds,
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
            session_agent_request_for_context(
                params,
                runtime_context=runtime_context,
            ),
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
        _session_outcome(
            result.staged_effect,
            result.final_answer,
            stop_reason=result.stop_reason,
        ),
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
    request = session_run_params(params)
    payload = request.run_payload(run_id)
    idempotency_key = f"session.turn.requested:{run_id}"
    if request.idempotency_key is not None:
        idempotency_key = (
            f"session.turn.requested:{runtime_context.session_id}:"
            f"{request.idempotency_key}"
        )
    return DraftEvent(
        "session.turn.requested",
        "zeta",
        payload,
        idempotency_key=idempotency_key,
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


def _session_outcome(
    staged_effect: dict[str, Any] | None,
    final_answer: str,
    *,
    stop_reason: str | None = None,
) -> str:
    del final_answer
    if stop_reason == "max_turns":
        return "max_turns"
    if stop_reason == "aborted":
        return "aborted"
    if staged_effect is not None:
        return "staged"
    return "completed"


def session_run_id() -> str:
    return ids.claimed_run_id()
