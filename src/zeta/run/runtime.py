"""Headless native-tool-call run execution for Zeta."""

from __future__ import annotations

import inspect
import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from zeta.capabilities.execution import (
    CapabilityCallResult,
    CapabilityExecutionContext,
    handle_tool_call,
)
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
from zeta.models import (
    DefaultModelGateway,
)
from zeta.models.chat_completions import tool_call_id
from zeta.models.types import ModelInput, ModelOutput
from zeta.records.events import (
    DraftEvent,
    Event,
    draft_event_view,
    event_view,
    runtime_event_draft,
    status_update_draft,
    stream_chunk_draft,
    turn_aborted_draft,
)
from zeta.records.provenance import TraceProjection, project_trace_drafts
from zeta.records.stores import Store
from zeta.run.cancellation import (
    AbortReason,
    AgentRunAborted,
    CancellationToken,
    agent_deadline,
    run_abort_reason,
)
from zeta.run.config import AgentConfig, ModelStatus
from zeta.run.outcomes import (
    AgentRunResult,
    RunState,
    RunStepOutcome,
)

AgentEventSink = Callable[[DraftEvent], None]
TimelineEvent = Event | dict[str, Any]
DEFAULT_MAX_TURNS = 25
tool_registry = _runtime_tool_registry
time_monotonic = time.monotonic


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
        for _ in turn_indices(self.config.max_turns):
            outcome = await self.model_step()
            if outcome.kind == "continue":
                continue
            self.state.note_step("finish_run")
            return self.state.result(
                final_answer=outcome.final_answer,
                staged_effect=outcome.staged_effect,
                answer_streamed=outcome.answer_streamed,
            )
        self.state.note_step("finish_run")
        return self.state.result()

    async def model_step(self) -> RunStepOutcome:
        self.state.note_step("check_budget")
        check_run_abort(
            self.state,
            ctx=self.deps,
        )
        turn = await request_model_turn(
            self.objective,
            self.timeline,
            config=self.config,
            allowed_capabilities=self.allowed_capabilities,
            context=self.context,
            tools=self.tools,
            state=self.state,
            ctx=self.deps,
        )
        if self.deps.abort_reason(check_deadline=False) is not None:
            check_run_abort(
                self.state,
                ctx=self.deps,
                check_deadline=False,
            )
        assistant = turn.assistant.to_provider()
        assistant_event_id, tool_calls = record_model_event(
            assistant,
            self.state.events,
            prompt_trace=turn.prompt_trace,
            caused_by=self.state.next_model_caused_by,
            ctx=self.deps,
        )
        update_prompt_trace_from_projection(
            assistant_event_id,
            state=self.state,
            ctx=self.deps,
        )
        if not tool_calls:
            return RunStepOutcome(
                kind="finished",
                final_answer=turn.assistant.content,
                answer_streamed=turn.streamed_content,
            )
        return await self.tool_step(
            tool_calls,
            model_telemetry=turn.model_telemetry,
            assistant_event_id=assistant_event_id,
        )

    async def tool_step(
        self,
        tool_calls: list[dict[str, Any]],
        *,
        model_telemetry: dict[str, Any],
        assistant_event_id: str | None,
    ) -> RunStepOutcome:
        for index, tool_call in enumerate(tool_calls):
            result_event = await run_capability_step(
                tool_call,
                index=index,
                config=self.config,
                allowed_capabilities=self.allowed_capabilities,
                tool_schema=self.tool_schema,
                model_telemetry=(model_telemetry if index == 0 else None),
                assistant_event_id=assistant_event_id,
                state=self.state,
                ctx=self.deps,
            )
            self.state.events.extend(result_event.events)
            if result_event.events:
                project_trace_drafts(self.state.events, self.deps.builder.store())
            self.state.next_model_caused_by = next_model_parent(result_event.events)
            if (
                result_event.staged_effect is not None
                and self.config.stop_on_staged_effect
            ):
                return RunStepOutcome(
                    kind="staged_effect",
                    staged_effect=result_event.staged_effect,
                )
            if result_event.stop:
                return RunStepOutcome(kind="finished")
        return RunStepOutcome(kind="continue")


async def run_agent(
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


class ModelTurnStreamSink:
    """Record model stream deltas as runtime events."""

    def __init__(
        self,
        events: list[DraftEvent],
        event_sink: AgentEventSink | None = None,
    ) -> None:
        self.events = events
        self.event_sink = event_sink
        self.streamed_content = False

    def content_delta(self, text: str) -> None:
        if not text:
            return
        self.streamed_content = True
        emit_event(
            self.events,
            stream_chunk_draft(text),
            self.event_sink,
        )

    def reasoning_delta(self, text: str) -> None:
        if not text:
            return
        emit_event(
            self.events,
            status_update_draft("reasoning_delta", text),
            self.event_sink,
        )


class StatusAwareModelStream:
    def __init__(self, stream: ModelTurnStreamSink, status: ModelStatus) -> None:
        self.stream = stream
        self.status = status

    @property
    def streamed_content(self) -> bool:
        return self.stream.streamed_content

    def content_delta(self, text: str) -> None:
        self.stream.content_delta(text)

    def reasoning_delta(self, text: str) -> None:
        self.status.reasoning_delta(text)
        self.stream.reasoning_delta(text)


def is_runtime_ui_event(draft: DraftEvent) -> bool:
    return draft.event_type in {"runtime.stream.chunk", "runtime.status.update"}


def draft_views_for_prompt(
    drafts: list[DraftEvent],
    builder: PromptBuilder,
) -> list[dict[str, Any]]:
    projection = project_trace_drafts(drafts, builder.store())
    views = []
    for draft in drafts:
        if is_runtime_ui_event(draft):
            continue
        view = draft_event_view(draft)
        event_id = draft_event_id(draft)
        if event_id is not None:
            add_projection_fields_for_prompt(view, event_id, projection)
        views.append(view)
    return views


def add_projection_fields_for_prompt(
    view: dict[str, Any],
    event_id: str,
    projection: TraceProjection,
) -> None:
    event_type = view.get("type")
    if event_type == "model":
        add_model_projection_fields(view, event_id, projection)
        return
    if event_type == "tool_call":
        call_id = projection.tool_call_object_ids.get(event_id)
        if call_id is not None:
            view["tool_call_object_id"] = call_id
        return
    if event_type == "tool_result":
        add_tool_result_projection_fields(view, event_id, projection)


def add_model_projection_fields(
    view: dict[str, Any],
    event_id: str,
    projection: TraceProjection,
) -> None:
    prompt_id = projection.prompt_object_ids.get(event_id)
    assistant_id = projection.assistant_message_ids.get(event_id)
    if prompt_id is not None:
        view["prompt_trace"] = {"prompt_object_id": prompt_id}
        if assistant_id is not None:
            view["prompt_trace"]["assistant_message_object_id"] = assistant_id
    tool_call_ids = projected_tool_call_ids(view, projection)
    if tool_call_ids:
        view["tool_call_object_ids"] = tool_call_ids


def projected_tool_call_ids(
    view: dict[str, Any],
    projection: TraceProjection,
) -> list[str]:
    tool_calls = view.get("tool_calls")
    if not isinstance(tool_calls, list):
        return []
    return [
        projection.tool_call_object_ids[tool_call["id"]]
        for tool_call in tool_calls
        if isinstance(tool_call, dict)
        and isinstance(tool_call.get("id"), str)
        and tool_call["id"] in projection.tool_call_object_ids
    ]


def add_tool_result_projection_fields(
    view: dict[str, Any],
    event_id: str,
    projection: TraceProjection,
) -> None:
    tool_call_id = view.get("tool_call_id")
    call_id = (
        projection.tool_call_object_ids.get(tool_call_id)
        if isinstance(tool_call_id, str)
        else None
    )
    result_id = projection.tool_result_object_ids.get(event_id)
    if call_id is not None:
        view["tool_call_object_id"] = call_id
    if result_id is not None:
        view["tool_result_object_id"] = result_id


def draft_event_id(draft: DraftEvent) -> str | None:
    key = draft.idempotency_key
    prefix = f"{draft.event_type}:"
    if key is None or not key.startswith(prefix):
        return None
    event_id = key[len(prefix) :].strip()
    return event_id or None


def emit_event(
    events: list[DraftEvent],
    event: DraftEvent,
    event_sink: AgentEventSink | None = None,
) -> None:
    events.append(event)
    if event_sink is not None:
        event_sink(event)


def emit_tool_event(
    events: list[DraftEvent],
    event: dict[str, Any],
    *,
    ctx: RunDependencies,
) -> None:
    record_runtime_event(
        events, runtime_event_draft(event, session_id=None, turn_id=None), ctx=ctx
    )


def record_runtime_event(
    events: list[DraftEvent],
    draft: DraftEvent,
    *,
    ctx: RunDependencies,
) -> DraftEvent:
    emit_event(events, draft, ctx.event_sink)
    if ctx.event_sink is None:
        project_trace_drafts(events, ctx.builder.store())
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


def ensure_event_id(event: dict[str, Any]) -> str:
    event_id = event.get("id")
    if isinstance(event_id, str) and event_id:
        return event_id
    event_id = str(uuid.uuid4())
    event["id"] = event_id
    return event_id


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
    event_id = ensure_event_id(event) if event else None
    tool_calls = assistant_tool_calls(assistant)
    if event:
        record_runtime_event(
            events,
            runtime_event_draft(event, session_id=None, turn_id=None),
            ctx=ctx,
        )
    return event_id, tool_calls


def update_prompt_trace_from_projection(
    assistant_event_id: str | None,
    *,
    state: RunState,
    ctx: RunDependencies,
) -> None:
    if assistant_event_id is None or not state.prompt_traces:
        return
    projection = project_trace_drafts(state.events, ctx.builder.store())
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


def draft_timeline_type(draft: DraftEvent) -> str:
    view_type = draft.payload.get("_timeline_type")
    if isinstance(view_type, str) and view_type:
        return view_type
    prefix = "zeta."
    if draft.event_type.startswith(prefix):
        return draft.event_type[len(prefix) :]
    return draft.event_type
