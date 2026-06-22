"""Headless native-tool-call turn execution for Zeta."""

from __future__ import annotations

import inspect
import json
import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, cast

from zeta.agents.capabilities import AgentConfig
from zeta.capabilities.base import proposed_effect
from zeta.capabilities.registry import (
    CapabilityProjection,
    CapabilityRegistry,
)
from zeta.capabilities.registry import registry as _runtime_tool_registry
from zeta.context import prompt_transform_from_policy
from zeta.context.builder import (
    PreparedPrompt,
    PromptBuilder,
    TraceProjection,
    prepared_prompt_from,
    project_trace_drafts,
    render_model_input,
)
from zeta.context.components import PromptTrace
from zeta.events import (
    draft_event_view,
    event_view,
    normalized_tool_result,
    runtime_event_draft,
    status_update_draft,
    stream_chunk_draft,
    tool_result_status,
    turn_aborted_draft,
)
from zeta.kernel.capabilities import ExecutionMode
from zeta.kernel.events import DraftEvent, Event
from zeta.kernel.models import ModelInput, ModelOutput
from zeta.models import (
    DefaultModelGateway,
)
from zeta.models.chat_completions import tool_call_id
from zeta.store.substrate import Store

AgentEventSink = Callable[[DraftEvent], None]
TimelineEvent = Event | dict[str, Any]
DEFAULT_MAX_TURNS = 25
tool_registry = _runtime_tool_registry
time_monotonic = time.monotonic
StepName = Literal[
    "check_budget",
    "build_prompt",
    "call_model",
    "record_assistant",
    "record_capability_call",
    "execute_capability",
    "record_capability_result",
    "finish_run",
    "abort_run",
]


@dataclass(frozen=True)
class StepEffect:
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StepResult:
    step: StepName
    effects: tuple[StepEffect, ...] = ()


@dataclass(frozen=True)
class AgentRunResult:
    final_answer: str = ""
    telemetry: dict[str, Any] = field(default_factory=dict)
    events: list[DraftEvent] = field(default_factory=list)
    staged_effect: dict[str, Any] | None = None
    answer_streamed: bool = False
    model_telemetry_calls: list[dict[str, Any]] = field(default_factory=list)
    prompt_traces: list[PromptTrace] = field(default_factory=list)
    steps: list[StepResult] = field(default_factory=list)


@dataclass
class RunState:
    events: list[DraftEvent] = field(default_factory=list)
    latest_model_telemetry: dict[str, Any] = field(default_factory=dict)
    model_telemetry_calls: list[dict[str, Any]] = field(default_factory=list)
    prompt_traces: list[PromptTrace] = field(default_factory=list)
    steps: list[StepResult] = field(default_factory=list)
    next_model_caused_by: str | None = None

    def result(
        self,
        *,
        final_answer: str = "",
        staged_effect: dict[str, Any] | None = None,
        answer_streamed: bool = False,
    ) -> AgentRunResult:
        return AgentRunResult(
            final_answer=final_answer,
            events=self.events,
            staged_effect=staged_effect,
            answer_streamed=answer_streamed,
            telemetry=self.latest_model_telemetry,
            model_telemetry_calls=self.model_telemetry_calls,
            prompt_traces=self.prompt_traces,
            steps=self.steps,
        )

    def note_model_telemetry(self, model_telemetry: dict[str, Any]) -> None:
        if not model_telemetry:
            return
        self.latest_model_telemetry = model_telemetry
        self.model_telemetry_calls.append(model_telemetry)

    def note_prompt_trace(self, prompt_trace: PromptTrace | None) -> None:
        if prompt_trace is not None:
            self.prompt_traces.append(prompt_trace)

    def note_step(self, step: StepName, *effects: StepEffect) -> None:
        self.steps.append(StepResult(step, effects))


class AgentRunAborted(RuntimeError):
    """Raised when a cooperative turn budget or cancellation request aborts."""

    def __init__(
        self,
        reason: str,
        *,
        result: AgentRunResult,
        event_recorded: bool,
    ) -> None:
        super().__init__(reason.replace("_", " "))
        self.reason = reason
        self.result = result
        self.event_recorded = event_recorded


class CancellationToken(Protocol):
    def is_set(self) -> bool: ...


class AbortReason(Protocol):
    def __call__(self, *, check_deadline: bool = True) -> str | None: ...


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


@dataclass(frozen=True)
class RunStepOutcome:
    kind: Literal["continue", "finished", "staged_effect", "aborted"]
    final_answer: str = ""
    staged_effect: dict[str, Any] | None = None
    answer_streamed: bool = False


@dataclass
class AgentRun:
    objective: str
    timeline: Sequence[TimelineEvent]
    config: AgentConfig
    context: str
    deps: RunDependencies
    allowed_capabilities: tuple[str, ...]
    projection: CapabilityProjection
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
        check_turn_budget(
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
            check_turn_budget(
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
                projection=self.projection,
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
    deadline = agent_deadline(config, deadline, clock=clock)
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
    projection = active_tool_registry.project(allowed_capabilities)
    tools = projection.descriptors
    return await AgentRun(
        objective=objective,
        timeline=timeline,
        config=config,
        context=context,
        deps=deps,
        allowed_capabilities=allowed_capabilities,
        projection=projection,
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
    projection: CapabilityProjection,
    model_telemetry: dict[str, Any] | None,
    assistant_event_id: str | None,
    state: RunState,
    ctx: RunDependencies,
) -> CapabilityCallResult:
    state.note_step("check_budget")
    check_turn_budget(
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
    handled = handle_tool_call(
        tool_call,
        allowed_capabilities=allowed_capabilities,
        projection=projection,
        index=index,
        execution_mode=config.execution_mode,
        model_telemetry=model_telemetry,
        caused_by=assistant_event_id,
        ctx=ctx,
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


def check_turn_budget(
    state: RunState,
    *,
    ctx: RunDependencies,
    check_deadline: bool = True,
) -> None:
    raise_if_agent_turn_aborted(
        state,
        ctx=ctx,
        check_deadline=check_deadline,
    )


def agent_deadline(
    config: AgentConfig,
    deadline: float | None,
    *,
    clock: Callable[[], float],
) -> float | None:
    if config.max_wall_seconds is None:
        return deadline
    configured = clock() + max(config.max_wall_seconds, 0.0)
    if deadline is None:
        return configured
    return min(deadline, configured)


def raise_if_agent_turn_aborted(
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


def agent_abort_reason(
    cancellation_event: CancellationToken | None,
    deadline: float | None,
    *,
    clock: Callable[[], float],
) -> str | None:
    if cancellation_event is not None and cancellation_event.is_set():
        return "cancelled"
    if deadline is not None and clock() >= deadline:
        return "deadline_exceeded"
    return None


def run_abort_reason(
    cancellation_event: CancellationToken | None,
    deadline: float | None,
    *,
    clock: Callable[[], float],
) -> AbortReason:
    def current_abort_reason(*, check_deadline: bool = True) -> str | None:
        return agent_abort_reason(
            cancellation_event,
            deadline if check_deadline else None,
            clock=clock,
        )

    return current_abort_reason


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


async def invoke_capability(
    capability_id: str,
    params: dict[str, Any],
    *,
    execution_mode: ExecutionMode = "stage",
    tool_registry: CapabilityRegistry | None = None,
) -> dict[str, Any]:
    active_tool_registry = tool_registry or _runtime_tool_registry
    return await active_tool_registry.invoke_async(
        capability_id,
        params,
        execution_mode=execution_mode,
    )


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
    generated = gateway.generate(
        model_input,
        config,
        stream=turn_stream_sink,
        telemetry_sink=model_telemetry.update,
    )
    model_output = await generated if inspect.isawaitable(generated) else generated
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


@dataclass(frozen=True)
class CapabilityCallResult:
    events: list[DraftEvent]
    staged_effect: dict[str, Any] | None = None
    stop: bool = False


def model_event(assistant: dict[str, Any]) -> dict[str, Any]:
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
    event = model_event(assistant)
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


def model_tool_call_event(
    tool_call: dict[str, Any],
    *,
    index: int,
    caused_by: str | None,
) -> dict[str, Any]:
    record = ModelToolCall.from_provider(tool_call, index=index)
    if record is None:
        return {}
    return record.event(caused_by=caused_by)


@dataclass(frozen=True)
class ModelToolCall:
    call_id: str
    name: str
    raw_arguments: str
    params: dict[str, Any]
    parse_error: str = ""

    @classmethod
    def from_provider(
        cls,
        tool_call: dict[str, Any],
        *,
        index: int,
    ) -> ModelToolCall | None:
        call_id = tool_call_id(tool_call, index=index)
        function = tool_call.get("function")
        if not isinstance(function, dict):
            return None
        name = str(function.get("name") or "")
        arguments = function.get("arguments")
        params, parse_error = parse_tool_arguments(arguments)
        raw_arguments = arguments if isinstance(arguments, str) else json.dumps(params)
        return cls(
            call_id=call_id,
            name=name,
            raw_arguments=raw_arguments,
            params=params,
            parse_error=parse_error,
        )

    def event(self, *, caused_by: str | None) -> dict[str, Any]:
        event: dict[str, Any] = {
            "type": "tool_call",
            "id": self.call_id,
            "tool_call_id": self.call_id,
            "status": "pending",
            "name": self.name,
            "input": self.params,
            "arguments": self.raw_arguments,
        }
        if caused_by is not None:
            event["caused_by"] = caused_by
        return event


@dataclass(frozen=True)
class CapabilityCallInvocation:
    tool_call: ModelToolCall
    call_event: dict[str, Any]

    @property
    def call_id(self) -> str:
        return self.tool_call.call_id

    @property
    def name(self) -> str:
        return self.tool_call.name

    @property
    def params(self) -> dict[str, Any]:
        return self.tool_call.params

    @property
    def parse_error(self) -> str:
        return self.tool_call.parse_error


@dataclass(frozen=True)
class ToolCallValidation:
    capability_id: str = ""
    error: tuple[str, str] | None = None


async def handle_tool_call(
    tool_call: dict[str, Any],
    *,
    allowed_capabilities: tuple[str, ...],
    projection: CapabilityProjection,
    index: int,
    execution_mode: ExecutionMode = "stage",
    model_telemetry: dict[str, Any] | None = None,
    caused_by: str | None = None,
    ctx: RunDependencies,
) -> CapabilityCallResult:
    call_id = tool_call_id(tool_call, index=index)
    invocation = tool_call_invocation(tool_call, index=index, caused_by=caused_by)
    if invocation is None:
        return invalid_tool_result(
            call_id,
            "",
            {},
            "invalid-tool-call",
            "tool call did not include a function payload",
            model_telemetry=model_telemetry,
            caused_by=caused_by,
            ctx=ctx,
        )
    validation = validate_tool_call(
        invocation,
        allowed_capabilities=allowed_capabilities,
        projection=projection,
        tool_registry=ctx.tool_registry,
    )
    if validation.error is not None:
        code, message = validation.error
        return reject_tool_call(
            invocation,
            code,
            message,
            model_telemetry=model_telemetry,
            ctx=ctx,
        )
    return await run_valid_tool_call(
        invocation,
        capability_id=validation.capability_id,
        execution_mode=execution_mode,
        model_telemetry=model_telemetry,
        ctx=ctx,
    )


def tool_call_invocation(
    tool_call: dict[str, Any],
    *,
    index: int,
    caused_by: str | None,
) -> CapabilityCallInvocation | None:
    record = ModelToolCall.from_provider(tool_call, index=index)
    if record is None:
        return None
    return CapabilityCallInvocation(
        tool_call=record,
        call_event=record.event(caused_by=caused_by),
    )


def validate_tool_call(
    invocation: CapabilityCallInvocation,
    *,
    allowed_capabilities: tuple[str, ...],
    projection: CapabilityProjection,
    tool_registry: CapabilityRegistry,
) -> ToolCallValidation:
    if invocation.parse_error:
        return ToolCallValidation(error=("invalid-json-args", invocation.parse_error))
    capability_id = projection.name_to_id.get(invocation.name)
    if capability_id is None:
        if tool_registry.resolve(invocation.name) is not None:
            return ToolCallValidation(
                error=(
                    "disallowed-tool",
                    f"tool is not allowed in this workflow: {invocation.name}",
                )
            )
        return ToolCallValidation(
            error=("unknown-tool", f"unknown tool: {invocation.name}")
        )
    if capability_id not in allowed_capabilities:
        return ToolCallValidation(
            error=(
                "disallowed-tool",
                f"tool is not allowed in this workflow: {invocation.name}",
            )
        )
    return ToolCallValidation(capability_id=capability_id)


def reject_tool_call(
    invocation: CapabilityCallInvocation,
    code: str,
    message: str,
    *,
    model_telemetry: dict[str, Any] | None,
    ctx: RunDependencies,
) -> CapabilityCallResult:
    return invalid_tool_result(
        invocation.call_id,
        invocation.name,
        invocation.params,
        code,
        message,
        call_event=invocation.call_event,
        model_telemetry=model_telemetry,
        ctx=ctx,
    )


async def run_valid_tool_call(
    invocation: CapabilityCallInvocation,
    *,
    capability_id: str,
    execution_mode: ExecutionMode,
    model_telemetry: dict[str, Any] | None,
    ctx: RunDependencies,
) -> CapabilityCallResult:
    events: list[DraftEvent] = []
    call_event = invocation.call_event
    call_event["capability_id"] = capability_id
    emit_tool_event(
        events,
        call_event,
        ctx=ctx,
    )
    try:
        invoked = invoke_capability(
            capability_id,
            invocation.params,
            execution_mode=execution_mode,
            tool_registry=ctx.tool_registry,
        )
        result = await invoked if inspect.isawaitable(invoked) else invoked
    except Exception as exc:
        result = tool_error("tool-crashed", f"{type(exc).__name__}: {exc}")
    staged_effect = result_staged_effect(result)
    stop = bool(
        execution_mode == "stage"
        and staged_effect is not None
        and result.get("ok") is True
    )
    result_event = tool_result_event(
        invocation.call_id,
        invocation.name,
        result,
        capability_id=capability_id,
        model_telemetry=model_telemetry,
    )
    if isinstance(call_event.get("caused_by"), str):
        result_event["caused_by"] = call_event["caused_by"]
    emit_tool_event(events, result_event, ctx=ctx)
    return CapabilityCallResult(
        events=events,
        staged_effect=staged_effect,
        stop=stop,
    )


def parse_tool_arguments(arguments: Any) -> tuple[dict[str, Any], str]:
    if isinstance(arguments, dict):
        return cast(dict[str, Any], arguments), ""
    if not isinstance(arguments, str):
        return {}, "function arguments were not a JSON object string"
    try:
        params = json.loads(arguments or "{}")
    except json.JSONDecodeError as exc:
        return {}, str(exc)
    if not isinstance(params, dict):
        return {}, "function arguments JSON was not an object"
    return cast(dict[str, Any], params), ""


def invalid_tool_result(
    call_id: str,
    name: str,
    params: dict[str, Any],
    code: str,
    message: str,
    *,
    call_event: dict[str, Any] | None = None,
    model_telemetry: dict[str, Any] | None = None,
    caused_by: str | None = None,
    ctx: RunDependencies,
) -> CapabilityCallResult:
    event = call_event or {
        "type": "tool_call",
        "id": call_id,
        "tool_call_id": call_id,
        "name": name,
        "input": params,
    }
    if caused_by is not None:
        event["caused_by"] = caused_by
    events: list[DraftEvent] = []
    result_event = tool_result_event(
        call_id,
        name,
        tool_error(code, message),
        model_telemetry=model_telemetry,
    )
    if isinstance(event.get("caused_by"), str):
        result_event["caused_by"] = event["caused_by"]
    emit_tool_event(
        events,
        event,
        ctx=ctx,
    )
    emit_tool_event(
        events,
        result_event,
        ctx=ctx,
    )
    return CapabilityCallResult(events=events)


def tool_result_event(
    call_id: str,
    name: str,
    result: dict[str, Any],
    *,
    capability_id: str = "",
    model_telemetry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "tool_result",
        "tool_call_id": call_id,
        "status": tool_result_status(result),
        "name": name,
        "result": normalized_tool_result(name, result),
    }
    ensure_event_id(event)
    if capability_id:
        event["capability_id"] = capability_id
    if model_telemetry:
        event["model_telemetry"] = dict(model_telemetry)
    return event


def tool_error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def result_staged_effect(result: dict[str, Any]) -> dict[str, Any] | None:
    return proposed_effect(result)
