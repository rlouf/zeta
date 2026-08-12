"""Run one capability call and record its terminal result."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable
from typing import Any, cast

from zeta.capabilities.execution import (
    CapabilityCallResult,
    CapabilityExecutionContext,
    handle_tool_call,
)
from zeta.capabilities.registry import CapabilityToolSchema
from zeta.events import DraftEvent
from zeta.journal.views import draft_timeline_type
from zeta.loop.config import AgentConfig
from zeta.loop.gateway import request_assistant_message
from zeta.loop.outcomes import RunState
from zeta.loop.request import RunDependencies
from zeta.loop.stages.abort import check_run_abort
from zeta.models.types import ModelInput, ModelOutput, tool_call_id
from zeta.tools.content import (
    ContentModelIdentity,
    ContentToolRuntime,
    bind_content_tools,
)
from zeta.tools.events import EventToolBindings, bind_event_tools
from zeta.tools.history import (
    ContextBudgetBinding,
    bind_context_budget_tools,
    bind_history_tools,
    context_compaction_settings,
)

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
    position: int | None = None,
) -> CapabilityCallResult:
    position = index if position is None else position
    state.note_step("check_budget")
    check_run_abort(state, ctx=ctx)
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

    async def request_content_model(
        model_input: ModelInput,
    ) -> tuple[ModelOutput, dict[str, Any]]:
        model_output, _streamed, telemetry = await request_assistant_message(
            model_input,
            config=config,
            model_gateway=ctx.model_gateway,
            should_stop=ctx.abort_reason,
        )
        return model_output, telemetry

    def select_final(object_id: str, content: str) -> None:
        state.final_object_id = object_id
        state.selected_final_answer = content

    compaction_strategy, compaction_threshold_tokens = context_compaction_settings(
        ctx.builder.transform
    )
    runtime_tools = {
        **bind_content_tools(
            ContentToolRuntime(
                workspace=ctx.content_workspace,
                position=position,
                transform_budget=ctx.content_transform_budget,
                source_queue_item_id=ctx.source_queue_item_id,
                abort_reason=ctx.abort_reason,
                model=ContentModelIdentity(
                    profile=config.model_profile,
                    name=config.model_name,
                    url=config.model_url,
                    thinking=config.thinking,
                    api=config.model_api,
                ),
                request_model=request_content_model,
                record_promotions=state.content_promotions.extend,
                select_final=select_final,
            )
        ),
        **bind_event_tools(
            EventToolBindings(
                position=position,
                publishable_events=ctx.publishable_events,
                source_queue_item_id=ctx.source_queue_item_id,
                source_agent_id=ctx.source_agent_id,
                source_session_id=ctx.source_session_id,
                publish_event_requests=state.publish_event_requests,
                wait_requests=state.wait_requests,
                cancel_requests=state.cancel_requests,
            )
        ),
        **bind_context_budget_tools(
            ContextBudgetBinding(
                telemetry=model_telemetry or {},
                prompt_object_id=(
                    state.prompt_traces[-1].prompt_object_id
                    if state.prompt_traces
                    else None
                ),
                store=ctx.builder.store(),
                selected_url=config.model_url,
                selected_model=config.model_name,
                compaction_strategy=compaction_strategy,
                compaction_threshold_tokens=compaction_threshold_tokens,
            )
        ),
        **bind_history_tools(ctx.query_log_reader),
    }

    async def run_internal_tool(
        capability_id: str,
        params: dict[str, Any],
    ) -> dict[str, Any] | None:
        runtime_tool = runtime_tools.get(capability_id)
        if runtime_tool is None:
            return None
        runtime_result = runtime_tool(params)
        if inspect.isawaitable(runtime_result):
            return await cast(Awaitable[dict[str, Any] | None], runtime_result)
        return runtime_result

    capability_ctx = CapabilityExecutionContext(
        event_sink=ctx.event_sink,
        trace_store=ctx.builder.store(),
        tool_registry=ctx.tool_registry,
        tool_executor=ctx.tool_executor,
        base_dir=config.base_dir,
        effect_scope=config.effect_scope or ctx.source_queue_item_id,
        internal_tool_executor=run_internal_tool,
        event_id_factory=ctx.event_id_factory,
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
