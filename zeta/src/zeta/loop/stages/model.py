"""Ask the model and record its answer.

This stage sends one prompt, receives one assistant message, and turns that
message into durable records.
"""

from __future__ import annotations

import inspect
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from zeta.context.builder import (
    PreparedPrompt,
    PromptBuilder,
)
from zeta.context.components import PromptTrace
from zeta.events import DraftEvent
from zeta.journal.drafts import (
    draft_from_runtime_event,
    ensure_runtime_event_id,
)
from zeta.journal.views import (
    draft_event_id,
    draft_timeline_type,
)
from zeta.loop.config import AgentConfig
from zeta.loop.gateway import ModelGateway, request_assistant_message
from zeta.loop.outcomes import (
    RunState,
)
from zeta.loop.projection import draft_views_for_prompt
from zeta.loop.request import (
    RunDependencies,
    record_runtime_event,
)
from zeta.loop.stages.prompt import build_prompt_step
from zeta.loop.types import (
    AgentEventSink,
    TimelineEvent,
)
from zeta.models import DefaultModelGateway
from zeta.models.types import ModelInput, ModelOutput
from zeta.trace.provenance import (
    project_prompt_trace_projection,
)


def assistant_tool_calls(assistant: dict[str, Any]) -> list[dict[str, Any]]:
    raw_tool_calls = assistant.get("tool_calls")
    if not isinstance(raw_tool_calls, list):
        return []
    return [call for call in raw_tool_calls if isinstance(call, dict)]


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


@dataclass(frozen=True)
class ModelTurn:
    assistant: AssistantMessage
    streamed_content: bool
    model_telemetry: dict[str, Any]
    prompt_trace: PromptTrace | None


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
