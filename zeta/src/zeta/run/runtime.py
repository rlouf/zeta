"""Headless native-tool-call run execution for Zeta."""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from zeta.capabilities.execution import (
    CapabilityCallResult,
    CapabilityExecutionContext,
    handle_tool_call,
)
from zeta.capabilities.host import HostDirectory
from zeta.capabilities.registry import (
    CapabilityRegistry,
    CapabilityToolSchema,
)
from zeta.capabilities.registry import registry as _runtime_tool_registry
from zeta.context import prompt_transform_from_policy
from zeta.context.builder import (
    PreparedPrompt,
    PromptBuilder,
    prepared_prompt_from,
    render_model_input,
)
from zeta.context.components import PromptTrace
from zeta.models import DefaultModelGateway
from zeta.models.chat_completions import tool_call_id
from zeta.models.types import ModelInput, ModelOutput
from zeta.records.events import (
    DraftEvent,
    Event,
    draft_event_id,
    draft_from_runtime_event,
    draft_timeline_type,
    ensure_runtime_event_id,
    event_timeline_type,
    event_view,
    turn_aborted_draft,
    user_message_draft,
)
from zeta.records.provenance import (
    project_prompt_trace_projection,
)
from zeta.records.stores.event_store import EventReader, Filter
from zeta.records.stores.object_store import Store, warn_trace_failure_once
from zeta.run.cancellation import (
    AbortReason,
    AgentRunAborted,
    CancellationToken,
    agent_deadline,
    run_abort_reason,
)
from zeta.run.config import AgentConfig
from zeta.run.context import RuntimeContext
from zeta.run.outcomes import (
    AgentRunResult,
    RunInfo,
    RunState,
)
from zeta.run.projection import draft_views_for_prompt, is_runtime_ui_event
from zeta.run.streaming import ModelTurnStreamSink, StatusAwareModelStream

AgentEventSink = Callable[[DraftEvent], None]
TimelineEvent = Event | dict[str, Any]
DEFAULT_MAX_TURNS = 25
tool_registry = _runtime_tool_registry
time_monotonic = time.monotonic
MODEL_TIMELINE_TYPES = frozenset(
    {
        "user_message",
        "model",
        "model_usage",
        "tool_call",
        "tool_result",
        "turn_aborted",
    }
)


@dataclass(frozen=True)
class AgentRunRequest:
    """Durable request envelope shared by session and authored-agent runs."""

    objective: str
    workflow: str
    runtime: str
    tools: tuple[str, ...]
    context: str
    config: AgentConfig
    fresh: bool = False


class ModelStream(Protocol):
    def content_delta(self, text: str) -> None: ...

    def reasoning_delta(self, text: str) -> None: ...


class ModelGateway(Protocol):
    def available(self, config: AgentConfig) -> bool: ...

    async def generate(
        self,
        model_input: ModelInput,
        config: AgentConfig,
        *,
        stream: ModelStream | None = None,
        telemetry_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> ModelOutput: ...


@dataclass(frozen=True)
class RunDependencies:
    event_sink: AgentEventSink | None
    trace_store: Store | None
    tool_registry: CapabilityRegistry
    builder: PromptBuilder
    abort_reason: AbortReason
    tool_hosts: HostDirectory | None = None
    model_gateway: ModelGateway = field(default_factory=DefaultModelGateway)


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
                staged_effect=info.staged_effect,
                answer_streamed=info.answer_streamed,
            )
        self.state.stop = "max_turns"
        self.state.note_step("finish_run")
        return self.state.result()


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
    state.note_step("check_budget")
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
    staged_effect: dict[str, Any] | None = None
    tool_calls = list(state.pending_tool_calls)
    model_telemetry = dict(state.pending_model_telemetry)
    assistant_event_id = state.pending_tool_parent_id
    state.pending_tool_calls = []
    state.pending_model_telemetry = {}
    state.pending_tool_parent_id = None
    for index, tool_call in enumerate(tool_calls):
        result_event = await run_capability_step(
            tool_call,
            index=index,
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
        if result_event.staged_effect is not None and config.stop_on_staged_effect:
            staged_effect = result_event.staged_effect
            state.stop = "staged_effect"
            break
        if result_event.stop:
            state.stop = "finished"
            break
    state.events.extend(batch_events)
    state.turn += 1
    return state, RunInfo(
        kind="tools",
        appended_events=tuple(appended_events),
        staged_effect=staged_effect,
    )


def silent_run_dependencies(ctx: RunDependencies) -> RunDependencies:
    return replace(ctx, event_sink=None)


def publish_step_info(info: RunInfo, *, ctx: RunDependencies) -> None:
    if ctx.event_sink is None or info.kind == "tools":
        return
    for draft in info.appended_events:
        ctx.event_sink(draft)


async def run_agent(
    request: AgentRunRequest,
    *,
    run_id: str,
    caused_by: str,
    publish_event: Callable[[Event], None],
    runtime_context: RuntimeContext,
    cancellation_event: CancellationToken | None,
    model_gateway: ModelGateway | None = None,
) -> AgentRunResult:
    """Run one durable agent turn inside a runtime session."""
    enabled_capabilities = registered_capabilities(
        request.tools or request.config.allowed_capabilities,
        tool_registry=runtime_context.tool_registry,
    )
    prior_timeline = (
        [] if request.fresh else current_timeline(runtime_context=runtime_context)
    )
    user_message: dict[str, Any] = {
        "type": "user_message",
        "content": request.objective,
        "workflow": request.workflow,
        "runtime": request.runtime,
        "available_tools": list(enabled_capabilities),
        "run_id": run_id,
    }
    model = run_model_metadata(request.config)
    if model:
        user_message["model"] = model
    user_event = _record_user_message(
        user_message,
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

    return await run_agent_loop(
        request.objective,
        prior_timeline,
        replace(
            request.config,
            allowed_capabilities=enabled_capabilities,
            model_session_id=runtime_context.session_id,
        ),
        context=request.context,
        event_sink=sink,
        trace_store=runtime_context.trace_store,
        tool_registry=runtime_context.tool_registry,
        model_gateway=model_gateway,
        caused_by=caused_by,
        cancellation_event=cancellation_event,
    )


def run_model_metadata(config: AgentConfig) -> dict[str, str]:
    metadata = {
        "profile": config.model_profile,
        "model": config.model_name,
        "url": config.model_url,
        "api": config.model_api,
    }
    return {key: value for key, value in metadata.items() if value}


async def run_agent_loop(
    objective: str,
    timeline: Sequence[TimelineEvent],
    config: AgentConfig,
    *,
    context: str = "",
    event_sink: AgentEventSink | None = None,
    prompt_builder: PromptBuilder | None = None,
    trace_store: Store | None = None,
    tool_registry: CapabilityRegistry | None = None,
    model_gateway: ModelGateway | None = None,
    caused_by: str | None = None,
    cancellation_event: CancellationToken | None = None,
    deadline: float | None = None,
) -> AgentRunResult:
    """Run an assistant/tool loop without mutating session state."""
    gateway = model_gateway or DefaultModelGateway()
    if not gateway.available(config):
        raise RuntimeError("model endpoint is not reachable")
    clock = time_monotonic
    deadline = agent_deadline(config.max_wall_seconds, deadline, clock=clock)
    active_tool_registry = tool_registry or _runtime_tool_registry
    allowed_capabilities = agent_allowed_capabilities(
        config,
        tool_registry=active_tool_registry,
    )
    state = RunState(next_model_caused_by=caused_by)
    builder = prompt_builder or PromptBuilder(
        store=trace_store,
        transform=prompt_transform_from_policy(config.compaction_policy),
    )
    deps = RunDependencies(
        event_sink=event_sink,
        trace_store=trace_store,
        tool_registry=active_tool_registry,
        tool_hosts=HostDirectory.from_registry(active_tool_registry),
        builder=builder,
        model_gateway=gateway,
        abort_reason=run_abort_reason(cancellation_event, deadline, clock=clock),
    )
    tool_schema = active_tool_registry.model_tool_schema(allowed_capabilities)
    tools = tool_schema.descriptors
    return await AgentRun(
        objective=objective,
        timeline=timeline,
        config=config,
        context=context,
        deps=deps,
        allowed_capabilities=allowed_capabilities,
        tool_schema=tool_schema,
        tools=tools,
        state=state,
    ).run()


def current_timeline(*, runtime_context: RuntimeContext) -> list[Event]:
    try:
        if not isinstance(runtime_context.event_sink, EventReader):
            return []
        events = runtime_context.event_sink.list_events(
            Filter(
                session_id=runtime_context.session_id,
                event_type_prefix="zeta.",
            )
        )
        return [
            event
            for event in events
            if event_timeline_type(event) in MODEL_TIMELINE_TYPES
        ]
    except Exception as exc:
        warn_trace_failure_once("current_timeline", exc)
        return []


def _record_user_message(
    event: dict[str, Any],
    *,
    runtime_context: RuntimeContext,
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
    runtime_context: RuntimeContext,
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


def _record_trace_for_run(runtime_context: RuntimeContext, run_id: str | None) -> None:
    if run_id is None or not isinstance(runtime_context.event_sink, EventReader):
        return
    try:
        project_prompt_trace_projection(
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


def session_trace_result(
    runtime_context: RuntimeContext,
    run_id: str,
) -> dict[str, list[str]]:
    if not isinstance(runtime_context.event_sink, EventReader):
        return empty_session_trace_result()
    trace = empty_session_trace_result()
    events = runtime_context.event_sink.list_events(
        Filter(
            session_id=runtime_context.session_id,
            run_id=run_id,
            event_type_prefix="zeta.",
        )
    )
    projection = project_prompt_trace_projection(events, runtime_context.trace_store)
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


def final_event_cursor(runtime_context: RuntimeContext, run_id: str) -> str | None:
    if not isinstance(runtime_context.event_sink, EventReader):
        return None
    events = runtime_context.event_sink.list_events(
        Filter(session_id=runtime_context.session_id, run_id=run_id)
    )
    if not events:
        return None
    return str(events[-1].cursor) if events[-1].cursor is not None else None


@dataclass(frozen=True)
class ModelTurn:
    assistant: AssistantMessage
    streamed_content: bool
    model_telemetry: dict[str, Any]
    prompt_trace: PromptTrace | None


@dataclass(frozen=True)
class AssistantMessage:
    content: str
    reasoning_content: str
    tool_calls: tuple[dict[str, Any], ...]
    provider_payload: dict[str, Any]

    @classmethod
    def from_provider(cls, assistant: dict[str, Any]) -> AssistantMessage:
        content = assistant.get("content")
        reasoning = assistant.get("reasoning_content")
        return cls(
            content=content if isinstance(content, str) else "",
            reasoning_content=reasoning if isinstance(reasoning, str) else "",
            tool_calls=tuple(assistant_tool_calls(assistant)),
            provider_payload=dict(assistant),
        )

    def to_provider(self) -> dict[str, Any]:
        return dict(self.provider_payload)


async def request_model_turn(
    objective: str,
    timeline: Sequence[TimelineEvent],
    *,
    config: AgentConfig,
    allowed_capabilities: tuple[str, ...],
    context: str,
    tools: list[dict[str, Any]],
    state: RunState,
    ctx: RunDependencies,
) -> ModelTurn:
    prepared_prompt, model_input = build_prompt_step(
        objective,
        timeline,
        config=config,
        allowed_capabilities=allowed_capabilities,
        context=context,
        current_events=draft_views_for_prompt(state.events, ctx.builder),
        tools=tools,
        state=state,
        builder=ctx.builder,
    )
    model_output, streamed_content, model_telemetry = await call_model_step(
        model_input,
        config=config,
        state=state,
        model_gateway=ctx.model_gateway,
        event_sink=ctx.event_sink,
    )
    assistant, prompt_trace = record_assistant_step(
        prepared_prompt,
        model_output,
        model_telemetry,
        state=state,
        builder=ctx.builder,
    )
    return ModelTurn(
        assistant=assistant,
        streamed_content=streamed_content,
        model_telemetry=model_telemetry,
        prompt_trace=prompt_trace,
    )


def build_prompt_step(
    objective: str,
    timeline: Sequence[TimelineEvent],
    *,
    config: AgentConfig,
    allowed_capabilities: tuple[str, ...],
    context: str,
    current_events: Iterable[dict[str, Any]],
    tools: list[dict[str, Any]],
    state: RunState,
    builder: PromptBuilder,
) -> tuple[PreparedPrompt, ModelInput]:
    state.note_step("build_prompt")
    prompt_plan = builder.plan_prompt(
        objective,
        [
            event_view(event) if isinstance(event, Event) else dict(event)
            for event in timeline
        ],
        system=config.system_prompt,
        allowed_capabilities=allowed_capabilities,
        context=context,
        current_events=current_events,
        tools=tools,
        tool_choice="auto",
        selected_model=config.model_name,
        thinking=config.thinking,
    )
    stored_prompt = builder.commit_prompt_plan(prompt_plan)
    model_input = render_model_input(stored_prompt)
    prepared_prompt = prepared_prompt_from(stored_prompt, model_input=model_input)
    return prepared_prompt, model_input


async def call_model_step(
    model_input: ModelInput,
    *,
    config: AgentConfig,
    state: RunState,
    model_gateway: ModelGateway | None = None,
    event_sink: AgentEventSink | None,
) -> tuple[ModelOutput, bool, dict[str, Any]]:
    state.note_step("call_model")
    requested = request_assistant_message(
        model_input,
        config=config,
        model_gateway=model_gateway or DefaultModelGateway(),
        events=state.events,
        event_sink=event_sink,
    )
    model_output, streamed_content, model_telemetry = (
        await requested if inspect.isawaitable(requested) else requested
    )
    return model_output, streamed_content, model_telemetry


def record_assistant_step(
    prepared_prompt: PreparedPrompt,
    model_output: ModelOutput,
    model_telemetry: dict[str, Any],
    *,
    state: RunState,
    builder: PromptBuilder,
) -> tuple[AssistantMessage, PromptTrace | None]:
    assistant = AssistantMessage.from_provider(model_output.message)
    state.note_step("record_assistant")
    prompt_trace = (
        PromptTrace(prompt_object_id=prepared_prompt.prompt_object_id)
        if prepared_prompt.prompt_object_id is not None
        else None
    )
    state.note_prompt_trace(prompt_trace)
    state.note_model_telemetry(model_telemetry)
    return assistant, prompt_trace


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
        tool_hosts=ctx.tool_hosts or HostDirectory.from_registry(ctx.tool_registry),
    )
    handled = handle_tool_call(
        tool_call,
        allowed_capabilities=allowed_capabilities,
        tool_schema=tool_schema,
        index=index,
        execution_mode=config.execution_mode,
        model_telemetry=model_telemetry,
        caused_by=assistant_event_id,
        ctx=capability_ctx,
    )
    result = await handled if inspect.isawaitable(handled) else handled
    state.note_step("record_capability_result")
    return result


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


def check_run_abort(
    state: RunState,
    *,
    ctx: RunDependencies,
    check_deadline: bool = True,
) -> None:
    raise_if_agent_run_aborted(
        state,
        ctx=ctx,
        check_deadline=check_deadline,
    )


def raise_if_agent_run_aborted(
    state: RunState,
    *,
    ctx: RunDependencies,
    check_deadline: bool,
) -> None:
    reason = ctx.abort_reason(check_deadline=check_deadline)
    if reason is None:
        return
    state.note_step("abort_run")
    record_runtime_event(
        state.events,
        turn_aborted_draft(
            reason=reason,
            session_id=None,
            turn_id=None,
            caused_by=state.next_model_caused_by,
        ),
        ctx=ctx,
    )
    raise AgentRunAborted(
        reason,
        result=state.result(),
        event_recorded=True,
    )


def agent_model_endpoint_open(config: AgentConfig) -> bool:
    return DefaultModelGateway().available(config)


def agent_allowed_capabilities(
    config: AgentConfig,
    *,
    tool_registry: CapabilityRegistry | None = None,
) -> tuple[str, ...]:
    return registered_capabilities(
        config.allowed_capabilities,
        tool_registry=tool_registry,
    )


def registered_capabilities(
    allowed_capabilities: Iterable[str] | None,
    *,
    tool_registry: CapabilityRegistry | None = None,
) -> tuple[str, ...]:
    """Filter to registered capabilities, preserving the caller's order."""
    active_tool_registry = tool_registry or _runtime_tool_registry
    if allowed_capabilities is None:
        return tuple(active_tool_registry.list_auto_enabled_capability_ids())
    enabled = []
    for name in allowed_capabilities:
        capability_id = active_tool_registry.resolve(name)
        if capability_id is not None:
            enabled.append(capability_id)
    return tuple(enabled)


def turn_indices(max_turns: int | None) -> Iterable[int]:
    if max_turns is None:
        max_turns = DEFAULT_MAX_TURNS
    return range(max(max_turns, 0))


async def request_assistant_message(
    model_input: ModelInput,
    *,
    config: AgentConfig,
    model_gateway: ModelGateway | None = None,
    events: list[DraftEvent] | None = None,
    event_sink: AgentEventSink | None = None,
) -> tuple[ModelOutput, bool, dict[str, Any]]:
    model_telemetry: dict[str, Any] = {}
    recorded_events = events if events is not None else []
    turn_stream_sink = ModelTurnStreamSink(recorded_events, event_sink)
    gateway = model_gateway or DefaultModelGateway()
    status_factory = config.model_status_factory
    if status_factory is None:
        generated = gateway.generate(
            model_input,
            config,
            stream=turn_stream_sink,
            telemetry_sink=model_telemetry.update,
        )
        model_output = await generated if inspect.isawaitable(generated) else generated
    else:
        with status_factory() as status:
            generated = gateway.generate(
                model_input,
                config,
                stream=StatusAwareModelStream(turn_stream_sink, status),
                telemetry_sink=model_telemetry.update,
            )
            model_output = (
                await generated if inspect.isawaitable(generated) else generated
            )
    return (
        model_output,
        turn_stream_sink.streamed_content,
        model_telemetry,
    )


def record_runtime_event(
    events: list[DraftEvent],
    draft: DraftEvent,
    *,
    ctx: RunDependencies,
) -> DraftEvent:
    events.append(draft)
    if ctx.event_sink is not None:
        ctx.event_sink(draft)
    if ctx.event_sink is None:
        project_prompt_trace_projection(events, ctx.builder.store())
    return draft


def model_event_payload(assistant: dict[str, Any]) -> dict[str, Any]:
    content = assistant.get("content")
    reasoning = assistant.get("reasoning_content")
    event: dict[str, Any] = {"type": "model"}
    if isinstance(reasoning, str) and reasoning:
        event["reasoning"] = reasoning
    if isinstance(content, str) and content:
        event["content"] = content
    tool_calls = assistant_tool_calls(assistant)
    if tool_calls:
        event["tool_calls"] = tool_calls
    return event


def assistant_tool_calls(assistant: dict[str, Any]) -> list[dict[str, Any]]:
    raw_tool_calls = assistant.get("tool_calls")
    if not isinstance(raw_tool_calls, list):
        return []
    return [call for call in raw_tool_calls if isinstance(call, dict)]


def record_model_event(
    assistant: dict[str, Any],
    events: list[DraftEvent],
    *,
    prompt_trace: PromptTrace | None,
    caused_by: str | None = None,
    ctx: RunDependencies,
) -> tuple[str | None, list[dict[str, Any]]]:
    event = model_event_payload(assistant)
    if caused_by is not None:
        event["caused_by"] = caused_by
    if prompt_trace is not None:
        event["prompt_object_id"] = prompt_trace.prompt_object_id
    event_id = ensure_runtime_event_id(event) if event else None
    tool_calls = assistant_tool_calls(assistant)
    if event:
        record_runtime_event(
            events,
            draft_from_runtime_event(event, session_id=None, turn_id=None),
            ctx=ctx,
        )
    return event_id, tool_calls


def update_prompt_trace_from_events(
    assistant_event_id: str | None,
    *,
    state: RunState,
    ctx: RunDependencies,
) -> None:
    if assistant_event_id is None or not state.prompt_traces:
        return
    projection = project_prompt_trace_projection(state.events, ctx.builder.store())
    assistant_id = projection.assistant_message_ids.get(assistant_event_id)
    if assistant_id is None:
        return
    trace = state.prompt_traces[-1]
    state.prompt_traces[-1] = PromptTrace(
        prompt_object_id=trace.prompt_object_id,
        assistant_message_object_id=assistant_id,
    )


def next_model_parent(events: list[DraftEvent]) -> str | None:
    for draft in reversed(events):
        if draft_timeline_type(draft) != "tool_result":
            continue
        event_id = draft_event_id(draft)
        if isinstance(event_id, str) and event_id:
            return event_id
    return None
