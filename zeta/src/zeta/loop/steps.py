"""One turn of the assistant and tool loop.

This module dispatches a turn across the stages. A step returns information; it
never decides what runs next.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from zeta.capabilities.registry import (
    CapabilityToolSchema,
)
from zeta.events import DraftEvent
from zeta.loop.config import AgentConfig
from zeta.loop.outcomes import (
    AgentRunResult,
    RunInfo,
    RunState,
)
from zeta.loop.request import RunDependencies, silent_run_dependencies
from zeta.loop.stages.abort import check_run_abort
from zeta.loop.stages.capability import run_capability_step
from zeta.loop.stages.model import (
    next_model_parent,
    record_model_event,
    request_model_turn,
    update_prompt_trace_from_events,
)
from zeta.loop.types import (
    DEFAULT_MAX_TURNS,
    TimelineEvent,
)
from zeta.trace.provenance import (
    project_prompt_trace_projection,
)


def publish_step_info(info: RunInfo, *, ctx: RunDependencies) -> None:
    if ctx.event_sink is None or info.kind == "tools":
        return
    for draft in info.appended_events:
        ctx.event_sink(draft)


def turn_indices(max_turns: int | None) -> Iterable[int]:
    if max_turns is None:
        max_turns = DEFAULT_MAX_TURNS
    return range(max(max_turns, 0))


async def step_model(
    state: RunState,
    *,
    objective: str,
    timeline: Sequence[TimelineEvent],
    config: AgentConfig,
    allowed_capabilities: tuple[str, ...],
    context: str,
    tools: list[dict[str, Any]],
    ctx: RunDependencies,
) -> tuple[RunState, RunInfo]:
    state.note_step("check_abort")
    check_run_abort(
        state,
        ctx=ctx,
    )
    turn = await request_model_turn(
        objective,
        timeline,
        config=config,
        allowed_capabilities=allowed_capabilities,
        context=context,
        tools=tools,
        state=state,
        ctx=ctx,
    )
    if ctx.abort_reason(check_deadline=False) is not None:
        check_run_abort(
            state,
            ctx=ctx,
            check_deadline=False,
        )
    assistant = turn.assistant.to_provider()
    before = len(state.events)
    assistant_event_id, tool_calls = record_model_event(
        assistant,
        state.events,
        prompt_trace=turn.prompt_trace,
        caused_by=state.next_model_caused_by,
        ctx=silent_run_dependencies(ctx),
    )
    update_prompt_trace_from_events(
        assistant_event_id,
        state=state,
        ctx=ctx,
    )
    appended_events = tuple(state.events[before:])
    state.turn += 1
    state.pending_tool_calls = list(tool_calls)
    state.pending_model_telemetry = dict(turn.model_telemetry)
    state.pending_tool_parent_id = assistant_event_id
    if not tool_calls:
        state.stop = "finished"
        return state, RunInfo(
            kind="model",
            appended_events=appended_events,
            prompt_trace=turn.prompt_trace,
            model_telemetry=turn.model_telemetry,
            final_answer=turn.assistant.content,
            answer_streamed=turn.streamed_content,
        )
    return state, RunInfo(
        kind="model",
        appended_events=appended_events,
        prompt_trace=turn.prompt_trace,
        model_telemetry=turn.model_telemetry,
        answer_streamed=turn.streamed_content,
    )


async def step_tools(
    state: RunState,
    *,
    config: AgentConfig,
    allowed_capabilities: tuple[str, ...],
    tool_schema: CapabilityToolSchema,
    ctx: RunDependencies,
) -> tuple[RunState, RunInfo]:
    appended_events: list[DraftEvent] = []
    batch_events: list[DraftEvent] = []
    tool_calls = list(state.pending_tool_calls)
    model_telemetry = dict(state.pending_model_telemetry)
    assistant_event_id = state.pending_tool_parent_id
    state.pending_tool_calls = []
    state.pending_model_telemetry = {}
    state.pending_tool_parent_id = None
    for index, tool_call in enumerate(tool_calls):
        position = state.next_tool_position
        state.next_tool_position += 1
        result_event = await run_capability_step(
            tool_call,
            index=index,
            position=position,
            config=config,
            allowed_capabilities=allowed_capabilities,
            tool_schema=tool_schema,
            model_telemetry=(model_telemetry if index == 0 else None),
            assistant_event_id=assistant_event_id,
            state=state,
            ctx=ctx,
        )
        batch_events.extend(result_event.events)
        appended_events.extend(result_event.events)
        if result_event.events:
            project_prompt_trace_projection(
                [*state.events, *batch_events],
                ctx.builder.store(),
            )
        state.next_model_caused_by = next_model_parent(result_event.events)
        if result_event.stop:
            state.stop = "tool_stop"
            break
    state.events.extend(batch_events)
    state.turn += 1
    return state, RunInfo(
        kind="tools",
        appended_events=tuple(appended_events),
    )


async def step(
    state: RunState,
    *,
    objective: str,
    timeline: Sequence[TimelineEvent],
    config: AgentConfig,
    allowed_capabilities: tuple[str, ...],
    context: str,
    tool_schema: CapabilityToolSchema,
    tools: list[dict[str, Any]],
    ctx: RunDependencies,
) -> tuple[RunState, RunInfo]:
    """Advance the run by one model call or one pending tool batch."""
    if state.stop is not None:
        return state, RunInfo(kind="stopped")
    if state.pending_tool_calls:
        return await step_tools(
            state,
            config=config,
            allowed_capabilities=allowed_capabilities,
            tool_schema=tool_schema,
            ctx=ctx,
        )
    return await step_model(
        state,
        objective=objective,
        timeline=timeline,
        config=config,
        allowed_capabilities=allowed_capabilities,
        context=context,
        tools=tools,
        ctx=ctx,
    )


@dataclass
class AgentRun:
    objective: str
    timeline: Sequence[TimelineEvent]
    config: AgentConfig
    context: str
    deps: RunDependencies
    allowed_capabilities: tuple[str, ...]
    tool_schema: CapabilityToolSchema
    tools: list[dict[str, Any]]
    state: RunState

    async def run(self) -> AgentRunResult:
        model_turns = 0
        max_model_turns = len(tuple(turn_indices(self.config.max_turns)))
        while model_turns < max_model_turns or self.state.pending_tool_calls:
            self.state, info = await step(
                self.state,
                objective=self.objective,
                timeline=self.timeline,
                config=self.config,
                allowed_capabilities=self.allowed_capabilities,
                context=self.context,
                tool_schema=self.tool_schema,
                tools=self.tools,
                ctx=self.deps,
            )
            if info.kind == "model":
                model_turns += 1
            publish_step_info(info, ctx=self.deps)
            if self.state.stop is None:
                continue
            self.state.note_step("finish_run")
            return self.state.result(
                final_answer=info.final_answer,
                answer_streamed=info.answer_streamed,
            )
        self.state.stop = "max_model_calls"
        self.state.note_step("finish_run")
        return self.state.result()
