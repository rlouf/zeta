"""Run one capability call.

This stage executes a model-requested tool call and records its result,
including the terminal statuses that end a call.
"""

from __future__ import annotations

import inspect
from typing import Any

from zeta.capabilities.execution import (
    CapabilityCallResult,
    CapabilityExecutionContext,
    handle_tool_call,
)
from zeta.capabilities.registry import (
    CapabilityToolSchema,
)
from zeta.events import DraftEvent
from zeta.journal.views import (
    draft_timeline_type,
)
from zeta.loop.config import AgentConfig
from zeta.loop.outcomes import (
    RunState,
)
from zeta.loop.request import RunDependencies
from zeta.loop.stages.abort import check_run_abort
from zeta.models.types import tool_call_id

TERMINAL_TOOL_STATUSES = {"completed", "failed", "refused", "cancelled", "timed_out"}


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
) -> CapabilityCallResult:
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
    capability_ctx = CapabilityExecutionContext(
        event_sink=ctx.event_sink,
        trace_store=ctx.builder.store(),
        tool_registry=ctx.tool_registry,
        tool_executor=ctx.tool_executor,
        base_dir=config.base_dir,
        effect_scope=config.effect_scope,
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
