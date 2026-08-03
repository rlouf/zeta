"""Run one capability call.

This stage executes a model-requested tool call and records its result,
including the terminal statuses that end a call.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import Any

from jsonschema import Draft202012Validator

from zeta.capabilities.execution import (
    CapabilityCallResult,
    CapabilityExecutionContext,
    handle_tool_call,
)
from zeta.capabilities.registry import (
    CapabilityToolSchema,
)
from zeta.events import DraftEvent
from zeta.ids import publish_event_handle, wait_handle
from zeta.journal.views import (
    draft_timeline_type,
)
from zeta.loop.config import AgentConfig
from zeta.loop.outcomes import (
    PublishEventRequest,
    RunState,
    WaitRequest,
)
from zeta.loop.request import RunDependencies
from zeta.loop.stages.abort import check_run_abort
from zeta.models.types import tool_call_id

TERMINAL_TOOL_STATUSES = {"completed", "failed", "refused", "cancelled", "timed_out"}
PUBLISH_EVENT_CAPABILITY_ID = "zeta.publish_event"
WAIT_FOR_CAPABILITY_ID = "zeta.wait_for"


def terminal_capability_result_event(
    events: list[DraftEvent],
    call_id: str,
) -> DraftEvent | None:
    for draft in reversed(events):
        if draft_timeline_type(draft) != "tool_result":
            continue
        if draft.payload.get("tool_call_id") != call_id:
            continue
        if draft.payload.get("status") in TERMINAL_TOOL_STATUSES:
            return draft
    return None


async def run_capability_step(
    tool_call: dict[str, Any],
    *,
    index: int,
    config: AgentConfig,
    allowed_capabilities: tuple[str, ...],
    tool_schema: CapabilityToolSchema,
    model_telemetry: dict[str, Any] | None,
    assistant_event_id: str | None,
    state: RunState,
    ctx: RunDependencies,
    position: int | None = None,
) -> CapabilityCallResult:
    position = index if position is None else position
    state.note_step("check_budget")
    check_run_abort(
        state,
        ctx=ctx,
    )
    if (
        terminal_capability_result_event(
            state.events,
            tool_call_id(tool_call, index=index),
        )
        is not None
    ):
        state.note_step("record_capability_result")
        return CapabilityCallResult(events=[])
    state.note_step("record_capability_call")
    state.note_step("execute_capability")

    def run_internal_tool(
        capability_id: str,
        params: dict[str, Any],
    ) -> dict[str, Any] | None:
        if capability_id == PUBLISH_EVENT_CAPABILITY_ID:
            return request_published_event(
                params,
                position=position,
                state=state,
                ctx=ctx,
            )
        if capability_id == WAIT_FOR_CAPABILITY_ID:
            return request_wait(
                params,
                position=position,
                state=state,
                ctx=ctx,
            )
        return None

    capability_ctx = CapabilityExecutionContext(
        event_sink=ctx.event_sink,
        trace_store=ctx.builder.store(),
        tool_registry=ctx.tool_registry,
        tool_executor=ctx.tool_executor,
        base_dir=config.base_dir,
        effect_scope=config.effect_scope,
        query_log_reader=ctx.query_log_reader,
        internal_tool_executor=run_internal_tool,
    )
    handled = handle_tool_call(
        tool_call,
        allowed_capabilities=allowed_capabilities,
        tool_schema=tool_schema,
        index=index,
        model_telemetry=model_telemetry,
        caused_by=assistant_event_id,
        ctx=capability_ctx,
    )
    result = await handled if inspect.isawaitable(handled) else handled
    state.note_step("record_capability_result")
    return result


def request_published_event(
    params: dict[str, Any],
    *,
    position: int,
    state: RunState,
    ctx: RunDependencies,
) -> dict[str, Any]:
    event_type = params["event_type"]
    schema = ctx.publishable_events.get(event_type)
    if event_type not in ctx.publishable_events:
        return publish_event_error(
            "undeclared-event-type",
            f"the agent does not list event {event_type!r} in publishes",
        )
    payload = params["payload"]
    if schema is not None:
        errors = sorted(
            Draft202012Validator(schema).iter_errors(payload),
            key=lambda error: list(error.path),
        )
        if errors:
            return publish_event_error("invalid-event-payload", errors[0].message)
    at = normalized_publish_time(params.get("at"))
    if isinstance(at, dict):
        return at
    if ctx.source_queue_item_id is None:
        return publish_event_error(
            "missing-event-source",
            "the run does not have a source queue item",
        )
    handle = publish_event_handle(ctx.source_queue_item_id, position)
    state.publish_event_requests.append(
        PublishEventRequest(
            handle=handle,
            event_type=event_type,
            payload=dict(payload),
            at=at,
            position=position,
        )
    )
    return {"ok": True, "handle": handle}


def request_wait(
    params: dict[str, Any],
    *,
    position: int,
    state: RunState,
    ctx: RunDependencies,
) -> dict[str, Any]:
    if ctx.source_queue_item_id is None:
        return publish_event_error(
            "missing-wait-source",
            "the run does not have a source queue item",
        )
    deadline = normalized_wait_deadline(params.get("deadline"))
    if isinstance(deadline, dict):
        return deadline
    handle = wait_handle(ctx.source_queue_item_id, position)
    state.wait_requests.append(
        WaitRequest(
            handle=handle,
            event_type=params["event_type"],
            fields=dict(params.get("fields") or {}),
            deadline=deadline,
            position=position,
        )
    )
    return {"ok": True, "handle": handle, "stop": True}


def normalized_wait_deadline(value: Any) -> str | None | dict[str, Any]:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return publish_event_error(
            "invalid-wait-deadline",
            "deadline must be an ISO 8601 date-time with a UTC offset",
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return publish_event_error(
            "invalid-wait-deadline",
            "deadline must include a UTC offset",
        )
    return parsed.astimezone(UTC).isoformat()


def normalized_publish_time(value: Any) -> str | None | dict[str, Any]:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return publish_event_error(
            "invalid-publish-time",
            "at must be an ISO 8601 date-time with a UTC offset",
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return publish_event_error(
            "invalid-publish-time",
            "at must include a UTC offset",
        )
    return parsed.astimezone(UTC).isoformat()


def publish_event_error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}
