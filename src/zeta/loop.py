"""Headless native-tool-call turn execution for Zeta."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, cast

from zeta import runtime_events
from zeta.agents.capabilities import AgentConfig
from zeta.capabilities.base import ExecutionMode, proposed_effect
from zeta.capabilities.registry import CapabilityProjection, CapabilityRegistry
from zeta.capabilities.registry import registry as _runtime_tool_registry
from zeta.context import prompt_transform_from_policy
from zeta.context.builder import (
    PreparedPrompt,
    PromptBuilder,
    prepared_prompt_from,
    render_model_input,
)
from zeta.context.components import PromptTrace, prompt_trace_payload
from zeta.events import DraftEvent, Event
from zeta.models import (
    DefaultModelGateway,
    ModelInput,
    ModelOutput,
)
from zeta.models.chat_completions import tool_call_id
from zeta.runtime_events import (
    ModelRuntimeEvent,
    ToolCallRuntimeEvent,
    ToolResultRuntimeEvent,
    TurnAbortedRuntimeEvent,
)
from zeta.store.substrate import Store
from zeta.timeline import timeline_event_from_durable_event

AgentEventSink = Callable[[DraftEvent], None]
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


@dataclass(frozen=True, init=False)
class AgentTurnResult:
    """Result from one native tool-call loop."""

    final_text: str = ""
    events: list[DraftEvent] = field(default_factory=list)
    staged_effect: dict[str, Any] | None = None
    final_text_streamed: bool = False
    model_telemetry: dict[str, Any] = field(default_factory=dict)
    model_telemetry_calls: list[dict[str, Any]] = field(default_factory=list)
    prompt_traces: list[PromptTrace] = field(default_factory=list)
    steps: list[StepResult] = field(default_factory=list)

    def __init__(
        self,
        final_text: str = "",
        events: Iterable[DraftEvent | dict[str, Any]] = (),
        staged_effect: dict[str, Any] | None = None,
        final_text_streamed: bool = False,
        model_telemetry: dict[str, Any] | None = None,
        model_telemetry_calls: list[dict[str, Any]] | None = None,
        prompt_traces: list[PromptTrace] | None = None,
        steps: list[StepResult] | None = None,
    ) -> None:
        object.__setattr__(self, "final_text", final_text)
        object.__setattr__(self, "events", normalize_draft_events(events))
        object.__setattr__(self, "staged_effect", staged_effect)
        object.__setattr__(self, "final_text_streamed", final_text_streamed)
        object.__setattr__(self, "model_telemetry", model_telemetry or {})
        object.__setattr__(
            self,
            "model_telemetry_calls",
            model_telemetry_calls or [],
        )
        object.__setattr__(self, "prompt_traces", prompt_traces or [])
        object.__setattr__(self, "steps", steps or [])


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
        final_text: str = "",
        staged_effect: dict[str, Any] | None = None,
        final_text_streamed: bool = False,
    ) -> AgentTurnResult:
        return AgentTurnResult(
            final_text=final_text,
            events=self.events,
            staged_effect=staged_effect,
            final_text_streamed=final_text_streamed,
            model_telemetry=self.latest_model_telemetry,
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

    def timeline_events(self) -> list[dict[str, Any]]:
        return [
            draft_timeline_event(draft)
            for draft in self.events
            if not is_runtime_ui_event(draft)
        ]


AgentTurnState = RunState


class AgentTurnAborted(RuntimeError):
    """Raised when a cooperative turn budget or cancellation request aborts."""

    def __init__(
        self,
        reason: str,
        *,
        result: AgentTurnResult,
        event_recorded: bool,
    ) -> None:
        super().__init__(reason.replace("_", " "))
        self.reason = reason
        self.result = result
        self.event_recorded = event_recorded


class CancellationToken(Protocol):
    def is_set(self) -> bool: ...


class ModelStream(Protocol):
    def content_delta(self, text: str) -> None: ...

    def reasoning_delta(self, text: str) -> None: ...


class ModelGateway(Protocol):
    def available(self, config: AgentConfig) -> bool: ...

    def generate(
        self,
        model_input: ModelInput,
        config: AgentConfig,
        *,
        stream: ModelStream | None = None,
        telemetry_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> ModelOutput: ...


@dataclass(frozen=True)
class TurnContext:
    event_sink: AgentEventSink | None
    trace_store: Store | None
    tool_registry: CapabilityRegistry
    builder: PromptBuilder
    cancellation_event: CancellationToken | None
    deadline: float | None
    model_gateway: ModelGateway = field(default_factory=DefaultModelGateway)


def _run_agent_turn_blocking(
    objective: str,
    timeline: list[dict[str, Any]],
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
) -> AgentTurnResult:
    """Run an assistant/tool loop without mutating session state."""
    gateway = model_gateway or DefaultModelGateway()
    if not gateway.available(config):
        raise RuntimeError("model endpoint is not reachable")
    deadline = agent_deadline(config, deadline)
    active_tool_registry = tool_registry or _runtime_tool_registry
    allowed_capabilities = agent_allowed_capabilities(
        config,
        tool_registry=active_tool_registry,
    )
    state = AgentTurnState(next_model_caused_by=caused_by)
    builder = prompt_builder or PromptBuilder(
        store=trace_store,
        transform=prompt_transform_from_policy(config.compaction_policy),
    )
    ctx = TurnContext(
        event_sink=event_sink,
        trace_store=trace_store,
        tool_registry=active_tool_registry,
        builder=builder,
        model_gateway=gateway,
        cancellation_event=cancellation_event,
        deadline=deadline,
    )
    projection = active_tool_registry.project(allowed_capabilities)
    tools = projection.descriptors
    return run_agent_steps(
        objective,
        timeline,
        config=config,
        context=context,
        allowed_capabilities=allowed_capabilities,
        projection=projection,
        tools=tools,
        state=state,
        ctx=ctx,
    )


async def async_run_agent_turn(
    objective: str,
    timeline: list[dict[str, Any]],
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
) -> AgentTurnResult:
    return await asyncio.to_thread(
        _run_agent_turn_blocking,
        objective,
        timeline,
        config,
        context=context,
        event_sink=event_sink,
        prompt_builder=prompt_builder,
        trace_store=trace_store,
        tool_registry=tool_registry,
        model_gateway=model_gateway,
        caused_by=caused_by,
        cancellation_event=cancellation_event,
        deadline=deadline,
    )


def run_agent_steps(
    objective: str,
    timeline: list[dict[str, Any]],
    *,
    config: AgentConfig,
    context: str,
    allowed_capabilities: tuple[str, ...],
    projection: CapabilityProjection,
    tools: list[dict[str, Any]],
    state: RunState,
    ctx: TurnContext,
) -> AgentTurnResult:
    for _ in turn_indices(config.max_turns):
        state.note_step("check_budget")
        check_turn_budget(
            state,
            ctx=ctx,
        )
        turn = request_model_turn(
            objective,
            timeline,
            config=config,
            allowed_capabilities=allowed_capabilities,
            context=context,
            tools=tools,
            state=state,
            ctx=ctx,
        )
        if ctx.cancellation_event is not None and ctx.cancellation_event.is_set():
            check_turn_budget(
                state,
                ctx=ctx,
                check_deadline=False,
            )
        assistant = turn.assistant.to_provider()
        assistant_event_id, tool_calls = record_model_event(
            assistant,
            state.events,
            prompt_trace=turn.prompt_trace,
            caused_by=state.next_model_caused_by,
            ctx=ctx,
        )
        if not tool_calls:
            state.note_step("finish_run")
            return state.result(
                final_text=turn.assistant.content,
                final_text_streamed=turn.streamed_content,
            )
        outcome = run_capability_calls(
            tool_calls,
            config=config,
            allowed_capabilities=allowed_capabilities,
            projection=projection,
            model_telemetry=turn.model_telemetry,
            prompt_trace=turn.prompt_trace,
            assistant_event_id=assistant_event_id,
            state=state,
            ctx=ctx,
        )
        if outcome is not None:
            return outcome
    state.note_step("finish_run")
    return state.result()


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


def request_model_turn(
    objective: str,
    timeline: list[dict[str, Any]],
    *,
    config: AgentConfig,
    allowed_capabilities: tuple[str, ...],
    context: str,
    tools: list[dict[str, Any]],
    state: AgentTurnState,
    ctx: TurnContext,
) -> ModelTurn:
    prepared_prompt, model_input = build_prompt_step(
        objective,
        timeline,
        config=config,
        allowed_capabilities=allowed_capabilities,
        context=context,
        current_events=state.timeline_events(),
        tools=tools,
        state=state,
        builder=ctx.builder,
    )
    model_output, streamed_content, model_telemetry = call_model_step(
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
    timeline: list[dict[str, Any]],
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
        timeline,
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


def call_model_step(
    model_input: ModelInput,
    *,
    config: AgentConfig,
    state: RunState,
    model_gateway: ModelGateway | None = None,
    event_sink: AgentEventSink | None,
) -> tuple[ModelOutput, bool, dict[str, Any]]:
    state.note_step("call_model")
    model_output, streamed_content, model_telemetry = request_assistant_message(
        model_input,
        config=config,
        model_gateway=model_gateway or DefaultModelGateway(),
        events=state.events,
        event_sink=event_sink,
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
    prompt_trace = builder.record_assistant_message(
        prepared_prompt,
        model_output,
    )
    state.note_prompt_trace(prompt_trace)
    state.note_model_telemetry(model_telemetry)
    return assistant, prompt_trace


def run_capability_calls(
    tool_calls: list[dict[str, Any]],
    *,
    config: AgentConfig,
    allowed_capabilities: tuple[str, ...],
    projection: CapabilityProjection,
    model_telemetry: dict[str, Any],
    prompt_trace: PromptTrace | None,
    assistant_event_id: str | None,
    state: AgentTurnState,
    ctx: TurnContext,
) -> AgentTurnResult | None:
    for index, tool_call in enumerate(tool_calls):
        result_event = run_capability_step(
            tool_call,
            index=index,
            config=config,
            allowed_capabilities=allowed_capabilities,
            projection=projection,
            model_telemetry=(model_telemetry if index == 0 else None),
            prompt_trace=prompt_trace,
            assistant_event_id=assistant_event_id,
            state=state,
            ctx=ctx,
        )
        state.events.extend(result_event.events)
        state.next_model_caused_by = next_model_parent(result_event.events)
        if result_event.staged_effect is not None and config.stop_on_staged_effect:
            state.note_step("finish_run")
            return state.result(staged_effect=result_event.staged_effect)
        if result_event.stop:
            state.note_step("finish_run")
            return state.result()
    return None


def run_capability_step(
    tool_call: dict[str, Any],
    *,
    index: int,
    config: AgentConfig,
    allowed_capabilities: tuple[str, ...],
    projection: CapabilityProjection,
    model_telemetry: dict[str, Any] | None,
    prompt_trace: PromptTrace | None,
    assistant_event_id: str | None,
    state: RunState,
    ctx: TurnContext,
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
    result = handle_tool_call(
        tool_call,
        allowed_capabilities=allowed_capabilities,
        projection=projection,
        index=index,
        execution_mode=config.execution_mode,
        model_telemetry=model_telemetry,
        prompt_trace=prompt_trace,
        caused_by=assistant_event_id,
        ctx=ctx,
    )
    state.note_step("record_capability_result")
    return result


TERMINAL_TOOL_STATUSES = {"completed", "failed", "refused", "cancelled", "timed_out"}


def terminal_capability_result_event(
    events: list[DraftEvent],
    call_id: str,
) -> dict[str, Any] | None:
    for draft in reversed(events):
        event = draft_timeline_event(draft)
        if event.get("type") != "tool_result":
            continue
        if event.get("tool_call_id") != call_id:
            continue
        if event.get("status") in TERMINAL_TOOL_STATUSES:
            return event
    return None


def check_turn_budget(
    state: AgentTurnState,
    *,
    ctx: TurnContext,
    check_deadline: bool = True,
) -> None:
    raise_if_agent_turn_aborted(
        state,
        ctx=ctx,
        deadline=ctx.deadline if check_deadline else None,
    )


def agent_deadline(config: AgentConfig, deadline: float | None) -> float | None:
    if config.max_wall_seconds is None:
        return deadline
    configured = time_monotonic() + max(config.max_wall_seconds, 0.0)
    if deadline is None:
        return configured
    return min(deadline, configured)


def raise_if_agent_turn_aborted(
    state: AgentTurnState,
    *,
    ctx: TurnContext,
    deadline: float | None,
) -> None:
    reason = agent_abort_reason(ctx.cancellation_event, deadline)
    if reason is None:
        return
    state.note_step("abort_run")
    event = turn_aborted_event(reason, caused_by=state.next_model_caused_by)
    record_runtime_event(
        state.events,
        runtime_events.TurnAbortedRuntimeEvent.from_event(event),
        ctx=ctx,
    )
    raise AgentTurnAborted(
        reason,
        result=state.result(),
        event_recorded=True,
    )


def agent_abort_reason(
    cancellation_event: CancellationToken | None,
    deadline: float | None,
) -> str | None:
    if cancellation_event is not None and cancellation_event.is_set():
        return "cancelled"
    if deadline is not None and time_monotonic() >= deadline:
        return "deadline_exceeded"
    return None


def turn_aborted_event(reason: str, *, caused_by: str | None) -> dict[str, Any]:
    return TurnAbortedRuntimeEvent(
        event_id=str(uuid.uuid4()),
        reason=reason,
        caused_by=caused_by,
    ).to_event()


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


def invoke_capability(
    capability_id: str,
    params: dict[str, Any],
    *,
    execution_mode: ExecutionMode = "stage",
    tool_registry: CapabilityRegistry | None = None,
) -> dict[str, Any]:
    active_tool_registry = tool_registry or _runtime_tool_registry
    return active_tool_registry.invoke(
        capability_id,
        params,
        execution_mode=execution_mode,
    )


def turn_indices(max_turns: int | None) -> Iterable[int]:
    if max_turns is None:
        max_turns = DEFAULT_MAX_TURNS
    return range(max(max_turns, 0))


def request_assistant_message(
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
    model_output = gateway.generate(
        model_input,
        config,
        stream=turn_stream_sink,
        telemetry_sink=model_telemetry.update,
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
            DraftEvent(
                "runtime.stream.chunk",
                "zeta",
                {"text": text, "_timeline_type": "runtime.stream.chunk"},
            ),
            self.event_sink,
        )

    def reasoning_delta(self, text: str) -> None:
        if not text:
            return
        emit_event(
            self.events,
            DraftEvent(
                "runtime.status.update",
                "zeta",
                {
                    "status": "reasoning_delta",
                    "text": text,
                    "_timeline_type": "runtime.status.update",
                },
            ),
            self.event_sink,
        )


def is_runtime_ui_event(draft: DraftEvent) -> bool:
    return draft.event_type in {"runtime.stream.chunk", "runtime.status.update"}


def draft_event_id(draft: DraftEvent) -> str | None:
    key = draft.idempotency_key
    prefix = f"{draft.event_type}:"
    if key is None or not key.startswith(prefix):
        return None
    event_id = key[len(prefix) :].strip()
    return event_id or None


def normalize_draft_events(
    events: Iterable[DraftEvent | dict[str, Any]],
) -> list[DraftEvent]:
    return [normalize_draft_event(event) for event in events]


def normalize_draft_event(event: DraftEvent | dict[str, Any]) -> DraftEvent:
    if isinstance(event, DraftEvent):
        return event
    runtime_event = runtime_events.runtime_event_from_event(event)
    if runtime_event is None:
        return DraftEvent(
            event_type=str(event.get("type") or "event"),
            source=str(event.get("source") or "zeta"),
            payload={key: value for key, value in event.items() if key != "type"},
        )
    return runtime_event.to_durable(session_id=None, turn_id=None)


def draft_timeline_event(draft: DraftEvent) -> dict[str, Any]:
    event = Event(
        id=draft_event_id(draft) or f"evt_{uuid.uuid4().hex}",
        event_type=draft.event_type,
        source=draft.source,
        payload=dict(draft.payload),
        idempotency_key=draft.idempotency_key,
        caused_by=draft.caused_by,
        session_id=draft.session_id,
        turn_id=draft.turn_id,
        timestamp_micros=time.time_ns() // 1_000,
    )
    return timeline_event_from_durable_event(event)


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
    ctx: TurnContext,
) -> None:
    record_runtime_event(
        events,
        runtime_events.runtime_event_from_event(event),
        event=event,
        ctx=ctx,
    )


def record_runtime_event(
    events: list[DraftEvent],
    runtime_event: runtime_events.RuntimeEvent | None,
    *,
    event: dict[str, Any] | None = None,
    ctx: TurnContext,
) -> dict[str, Any]:
    recorded = (
        runtime_event.to_event() if runtime_event is not None else dict(event or {})
    )
    if runtime_event is not None:
        emit_event(
            events,
            runtime_event.to_durable(session_id=None, turn_id=None),
            ctx.event_sink,
        )
    return recorded


@dataclass(frozen=True)
class CapabilityCallResult:
    events: list[DraftEvent]
    staged_effect: dict[str, Any] | None = None
    stop: bool = False


def model_event(assistant: dict[str, Any]) -> dict[str, Any]:
    return ModelRuntimeEvent.from_assistant(assistant).to_event()


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
    ctx: TurnContext,
) -> tuple[str | None, list[dict[str, Any]]]:
    event = model_event(assistant)
    if caused_by is not None:
        event["caused_by"] = caused_by
    if prompt_trace is not None:
        attach_prompt_trace(event, prompt_trace)
    event_id = ensure_event_id(event) if event else None
    tool_calls = assistant_tool_calls(assistant)
    tool_call_object_ids = model_tool_call_object_ids(
        tool_calls,
        caused_by=event_id,
        prompt_trace=prompt_trace,
        prompt_builder=ctx.builder,
    )
    if tool_call_object_ids:
        event["tool_call_object_ids"] = tool_call_object_ids
    if event:
        record_runtime_event(
            events,
            runtime_events.ModelRuntimeEvent.from_event(event),
            ctx=ctx,
        )
    return event_id, tool_calls


def next_model_parent(events: list[DraftEvent]) -> str | None:
    for draft in reversed(events):
        event = draft_timeline_event(draft)
        if str(event.get("type") or "") != "tool_result":
            continue
        event_id = event.get("id")
        if isinstance(event_id, str) and event_id:
            return event_id
    return None


def attach_prompt_trace(event: dict[str, Any], trace: PromptTrace) -> None:
    event["prompt_trace"] = prompt_trace_payload(trace)


def attach_tool_call_trace(
    event: dict[str, Any],
    *,
    prompt_trace: PromptTrace | None,
    prompt_builder: PromptBuilder | None,
) -> None:
    if prompt_trace is None or prompt_builder is None:
        return
    object_id = prompt_builder.record_tool_call(prompt_trace, event)
    if object_id:
        event["tool_call_object_id"] = object_id


def attach_tool_result_trace(
    event: dict[str, Any],
    call_event: dict[str, Any],
    *,
    prompt_trace: PromptTrace | None,
    prompt_builder: PromptBuilder | None,
) -> None:
    if prompt_trace is None or prompt_builder is None:
        return
    object_id = prompt_builder.record_tool_result(prompt_trace, call_event, event)
    if object_id:
        event["tool_result_object_id"] = object_id
        call_object_id = str(call_event.get("tool_call_object_id") or "")
        if call_object_id:
            event["tool_call_object_id"] = call_object_id


def model_tool_call_object_ids(
    tool_calls: list[dict[str, Any]],
    *,
    caused_by: str | None,
    prompt_trace: PromptTrace | None,
    prompt_builder: PromptBuilder | None,
) -> list[str]:
    object_ids: list[str] = []
    if prompt_trace is None or prompt_builder is None:
        return object_ids
    for index, tool_call in enumerate(tool_calls):
        call_event = model_tool_call_event(tool_call, index=index, caused_by=caused_by)
        if not call_event:
            continue
        attach_tool_call_trace(
            call_event,
            prompt_trace=prompt_trace,
            prompt_builder=prompt_builder,
        )
        object_id = call_event.get("tool_call_object_id")
        if isinstance(object_id, str):
            object_ids.append(object_id)
    return object_ids


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
        return ToolCallRuntimeEvent(tool_call=self, caused_by=caused_by).to_event()


@dataclass(frozen=True)
class CapabilityCallInvocation:
    tool_call: ModelToolCall
    runtime_event: ToolCallRuntimeEvent

    @property
    def call_event(self) -> dict[str, Any]:
        return self.runtime_event.to_event()

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


def handle_tool_call(
    tool_call: dict[str, Any],
    *,
    allowed_capabilities: tuple[str, ...],
    projection: CapabilityProjection,
    index: int,
    execution_mode: ExecutionMode = "stage",
    model_telemetry: dict[str, Any] | None = None,
    prompt_trace: PromptTrace | None = None,
    caused_by: str | None = None,
    ctx: TurnContext,
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
            prompt_trace=prompt_trace,
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
            prompt_trace=prompt_trace,
            ctx=ctx,
        )
    return run_valid_tool_call(
        invocation,
        capability_id=validation.capability_id,
        execution_mode=execution_mode,
        model_telemetry=model_telemetry,
        prompt_trace=prompt_trace,
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
        runtime_event=ToolCallRuntimeEvent(tool_call=record, caused_by=caused_by),
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
    capability_id = projection.alias_to_id.get(invocation.name)
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
    schema_errors = tool_registry.validate_capability_args(
        capability_id,
        invocation.params,
    )
    if schema_errors:
        return ToolCallValidation(error=("schema-mismatch", "; ".join(schema_errors)))
    return ToolCallValidation(capability_id=capability_id)


def reject_tool_call(
    invocation: CapabilityCallInvocation,
    code: str,
    message: str,
    *,
    model_telemetry: dict[str, Any] | None,
    prompt_trace: PromptTrace | None,
    ctx: TurnContext,
) -> CapabilityCallResult:
    return invalid_tool_result(
        invocation.call_id,
        invocation.name,
        invocation.params,
        code,
        message,
        call_event=invocation.call_event,
        model_telemetry=model_telemetry,
        prompt_trace=prompt_trace,
        ctx=ctx,
    )


def run_valid_tool_call(
    invocation: CapabilityCallInvocation,
    *,
    capability_id: str,
    execution_mode: ExecutionMode,
    model_telemetry: dict[str, Any] | None,
    prompt_trace: PromptTrace | None,
    ctx: TurnContext,
) -> CapabilityCallResult:
    events: list[DraftEvent] = []
    call_event = invocation.call_event
    call_event["capability_id"] = capability_id
    attach_tool_call_trace(
        call_event,
        prompt_trace=prompt_trace,
        prompt_builder=ctx.builder,
    )
    emit_tool_event(
        events,
        call_event,
        ctx=ctx,
    )
    try:
        result = invoke_capability(
            capability_id,
            invocation.params,
            execution_mode=execution_mode,
            tool_registry=ctx.tool_registry,
        )
    except Exception as exc:
        result = tool_error("tool-crashed", f"{type(exc).__name__}: {exc}")
    staged_effect = (
        result_staged_effect(result)
        if tool_call_stages_effect(
            capability_id,
            execution_mode,
            tool_registry=ctx.tool_registry,
        )
        else None
    )
    stop = bool(
        execution_mode == "stage"
        and capability_stops_turn_after_stage(
            capability_id, tool_registry=ctx.tool_registry
        )
        and result.get("ok") is True
    )
    emit_tool_event(
        events,
        traced_tool_result_event(
            invocation.call_id,
            invocation.name,
            result,
            capability_id=capability_id,
            call_event=call_event,
            model_telemetry=model_telemetry,
            prompt_trace=prompt_trace,
            prompt_builder=ctx.builder,
        ),
        ctx=ctx,
    )
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
    prompt_trace: PromptTrace | None = None,
    caused_by: str | None = None,
    ctx: TurnContext,
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
    attach_tool_call_trace(
        event,
        prompt_trace=prompt_trace,
        prompt_builder=ctx.builder,
    )
    result_event = tool_result_event(
        call_id,
        name,
        tool_error(code, message),
        model_telemetry=model_telemetry,
        prompt_trace=prompt_trace,
    )
    if isinstance(event.get("caused_by"), str):
        result_event["caused_by"] = event["caused_by"]
    attach_tool_result_trace(
        result_event,
        event,
        prompt_trace=prompt_trace,
        prompt_builder=ctx.builder,
    )
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


def traced_tool_result_event(
    call_id: str,
    name: str,
    result: dict[str, Any],
    *,
    capability_id: str = "",
    call_event: dict[str, Any],
    model_telemetry: dict[str, Any] | None = None,
    prompt_trace: PromptTrace | None = None,
    prompt_builder: PromptBuilder | None = None,
) -> dict[str, Any]:
    event = tool_result_event(
        call_id,
        name,
        result,
        capability_id=capability_id,
        model_telemetry=model_telemetry,
        prompt_trace=prompt_trace,
    )
    if isinstance(call_event.get("caused_by"), str):
        event["caused_by"] = call_event["caused_by"]
    attach_tool_result_trace(
        event,
        call_event,
        prompt_trace=prompt_trace,
        prompt_builder=prompt_builder,
    )
    return event


def tool_result_event(
    call_id: str,
    name: str,
    result: dict[str, Any],
    *,
    capability_id: str = "",
    model_telemetry: dict[str, Any] | None = None,
    prompt_trace: PromptTrace | None = None,
) -> dict[str, Any]:
    trace_payload = (
        prompt_trace_payload(prompt_trace) if prompt_trace is not None else None
    )
    return ToolResultRuntimeEvent(
        call_id=call_id,
        name=name,
        result=result,
        capability_id=capability_id,
        model_telemetry=model_telemetry,
        prompt_trace=trace_payload,
    ).to_event()


REFUSED_TOOL_ERROR_CODES = {
    "direct-execution-disallowed",
    "disallowed-tool",
    "invalid-json-args",
    "invalid-tool-call",
    "schema-mismatch",
    "staging-unsupported",
    "unknown-tool",
}


def tool_result_status(result: dict[str, Any]) -> str:
    if result.get("ok") is True:
        return "completed"
    error = result.get("error")
    if isinstance(error, dict) and error.get("code") in REFUSED_TOOL_ERROR_CODES:
        return "refused"
    return "failed"


def normalized_tool_result(name: str, result: dict[str, Any]) -> dict[str, Any]:
    stored = dict(result)
    if stored.get("ok") is not False or isinstance(stored.get("error"), dict):
        return stored
    message = tool_failure_message(stored)
    if message:
        stored["error"] = {
            "code": f"{name or 'tool'}-failed",
            "message": message,
        }
    return stored


def tool_failure_message(result: dict[str, Any]) -> str:
    content = result.get("content")
    text = first_tool_text(content)
    if text:
        return flatten_tool_text(text)
    metadata = result.get("metadata")
    if isinstance(metadata, dict):
        status = metadata.get("status")
        if isinstance(status, int):
            return f"status {status}"
    return ""


def first_tool_text(content: object) -> str:
    if not isinstance(content, list):
        return ""
    for item in content:
        if not isinstance(item, dict):
            continue
        text = cast("dict[str, Any]", item).get("text")
        if isinstance(text, str) and text.strip():
            return text
    return ""


def flatten_tool_text(text: str) -> str:
    return " ".join(text.strip().split())


def tool_error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def tool_call_stages_effect(
    name: str,
    execution_mode: ExecutionMode,
    *,
    tool_registry: CapabilityRegistry | None = None,
) -> bool:
    if execution_mode != "stage":
        return False
    active_tool_registry = tool_registry or _runtime_tool_registry
    capability_id = active_tool_registry.resolve(name)
    if capability_id is None:
        return False
    capability = active_tool_registry.get(capability_id)
    return capability is not None and capability.spec.mutates()


def capability_stops_turn_after_stage(
    capability_id: str,
    *,
    tool_registry: CapabilityRegistry,
) -> bool:
    capability = tool_registry.get(capability_id)
    return capability is not None and capability.policy.stop_turn_after_stage


def result_staged_effect(result: dict[str, Any]) -> dict[str, Any] | None:
    return proposed_effect(result)
