"""Headless native-tool-call agent loop for Zeta."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, cast

from .models import (
    CODEX_RESPONSES_API,
    ChatCompletionStreamSink,
    chat_completion_messages,
    model_endpoint_open,
)
from .prompt import PromptBuilder, prompt_transform_from_env
from .prompt.system import model_tool_descriptors
from .tools.base import proposed_effect
from .tools.registry import ExecutionMode, ToolRegistry
from .tools.registry import registry as _runtime_tool_registry
from .trace import PromptTrace, Store, prompt_trace_payload

AgentEventSink = Callable[[dict[str, Any]], None]
ModelStatusFactory = Callable[[], AbstractContextManager[object]]
DEFAULT_MAX_TURNS = 25
tool_registry = _runtime_tool_registry


@dataclass(frozen=True)
class AgentConfig:
    """Configuration for one Zeta turn."""

    system_prompt: str | None = None
    allowed_tools: Iterable[str] | None = None
    max_turns: int | None = None
    stop_on_staged_effect: bool = True
    execution_mode: ExecutionMode = "stage"
    model_profile: str | None = None
    model_name: str | None = None
    model_url: str | None = None
    model_session_id: str | None = None
    thinking: str | None = None
    model_api: str | None = None


@dataclass(frozen=True)
class AgentTurnResult:
    """Result from one native tool-call loop."""

    final_text: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    staged_effect: dict[str, Any] | None = None
    final_text_streamed: bool = False
    model_telemetry: dict[str, Any] = field(default_factory=dict)
    model_telemetry_calls: list[dict[str, Any]] = field(default_factory=list)
    prompt_traces: list[PromptTrace] = field(default_factory=list)


def run_agent_turn(
    objective: str,
    timeline: list[dict[str, Any]],
    config: AgentConfig,
    *,
    context: str = "",
    event_sink: AgentEventSink | None = None,
    model_status: ModelStatusFactory | None = None,
    stream_sink: ChatCompletionStreamSink | None = None,
    prompt_builder: PromptBuilder | None = None,
    trace_store: Store | None = None,
    tool_registry: ToolRegistry | None = None,
    caused_by: str | None = None,
) -> AgentTurnResult:
    """Run an assistant/tool loop without mutating session state."""
    if not agent_model_endpoint_open(config):
        raise RuntimeError("model endpoint is not reachable")
    active_tool_registry = tool_registry or _runtime_tool_registry
    allowed_tools = agent_allowed_tools(config, tool_registry=active_tool_registry)
    events: list[dict[str, Any]] = []
    latest_model_telemetry: dict[str, Any] = {}
    model_telemetry_calls: list[dict[str, Any]] = []
    prompt_traces: list[PromptTrace] = []
    builder = prompt_builder or PromptBuilder(
        store=trace_store,
        transform=prompt_transform_from_env(),
    )
    tools = model_tool_descriptors(allowed_tools, tool_registry=active_tool_registry)
    next_model_caused_by = caused_by
    for _ in turn_indices(config.max_turns):
        prepared_prompt = builder.build(
            objective,
            timeline,
            system=config.system_prompt,
            allowed_tools=allowed_tools,
            context=context,
            current_events=events,
            tools=tools,
            tool_choice="auto",
            selected_model=config.model_name,
            thinking=config.thinking,
        )
        assistant, streamed_content, model_telemetry = request_assistant_message(
            prepared_prompt.messages,
            tools=prepared_prompt.tools,
            tool_choice=prepared_prompt.tool_choice,
            config=config,
            model_status=model_status,
            stream_sink=stream_sink,
        )
        prompt_trace = builder.record_assistant_message(prepared_prompt, assistant)
        if prompt_trace is not None:
            prompt_traces.append(prompt_trace)
        if model_telemetry:
            latest_model_telemetry = model_telemetry
            model_telemetry_calls.append(model_telemetry)
        assistant_event_id, tool_calls = record_model_event(
            assistant,
            events,
            prompt_trace=prompt_trace,
            prompt_builder=builder,
            event_sink=event_sink,
            caused_by=next_model_caused_by,
        )
        if not tool_calls:
            return AgentTurnResult(
                final_text=str(assistant.get("content") or ""),
                events=events,
                final_text_streamed=streamed_content,
                model_telemetry=latest_model_telemetry,
                model_telemetry_calls=model_telemetry_calls,
                prompt_traces=prompt_traces,
            )
        for index, tool_call in enumerate(tool_calls):
            result_event = handle_tool_call(
                tool_call,
                allowed_tools=allowed_tools,
                index=index,
                execution_mode=config.execution_mode,
                model_telemetry=(model_telemetry if index == 0 else None),
                prompt_trace=prompt_trace,
                prompt_builder=builder,
                event_sink=event_sink,
                tool_registry=active_tool_registry,
                caused_by=assistant_event_id,
            )
            events.extend(result_event.events)
            next_model_caused_by = next_model_parent(result_event.events)
            if result_event.staged_effect is not None and config.stop_on_staged_effect:
                return AgentTurnResult(
                    final_text="",
                    events=events,
                    staged_effect=result_event.staged_effect,
                    model_telemetry=latest_model_telemetry,
                    model_telemetry_calls=model_telemetry_calls,
                    prompt_traces=prompt_traces,
                )
            if result_event.stop:
                return AgentTurnResult(
                    events=events,
                    model_telemetry=latest_model_telemetry,
                    model_telemetry_calls=model_telemetry_calls,
                    prompt_traces=prompt_traces,
                )
    return AgentTurnResult(
        events=events,
        model_telemetry=latest_model_telemetry,
        model_telemetry_calls=model_telemetry_calls,
        prompt_traces=prompt_traces,
    )


def agent_model_endpoint_open(config: AgentConfig) -> bool:
    if config.model_api == CODEX_RESPONSES_API:
        return True
    if config.model_url is None:
        return model_endpoint_open()
    return model_endpoint_open(config.model_url)


def agent_allowed_tools(
    config: AgentConfig,
    *,
    tool_registry: ToolRegistry | None = None,
) -> tuple[str, ...]:
    return registered_tools(config.allowed_tools, tool_registry=tool_registry)


def registered_tools(
    allowed_tools: Iterable[str] | None,
    *,
    tool_registry: ToolRegistry | None = None,
) -> tuple[str, ...]:
    """Filter to registered tools, preserving the caller's order."""
    active_tool_registry = tool_registry or _runtime_tool_registry
    if allowed_tools is None:
        return tuple(active_tool_registry.list_tool_names())
    available = set(active_tool_registry.list_tool_names())
    return tuple(name for name in allowed_tools if name in available)


def run_tool(
    name: str,
    params: dict[str, Any],
    *,
    execution_mode: ExecutionMode = "stage",
    tool_registry: ToolRegistry | None = None,
) -> dict[str, Any]:
    active_tool_registry = tool_registry or _runtime_tool_registry
    return active_tool_registry.run_tool(
        name,
        params,
        execution_mode=execution_mode,
    )


def turn_indices(max_turns: int | None) -> Iterable[int]:
    if max_turns is None:
        max_turns = DEFAULT_MAX_TURNS
    return range(max(max_turns, 0))


def request_assistant_message(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]],
    tool_choice: str | dict[str, Any],
    config: AgentConfig,
    model_status: ModelStatusFactory | None,
    stream_sink: ChatCompletionStreamSink | None,
) -> tuple[dict[str, Any], bool, dict[str, Any]]:
    status_context = model_status_context(model_status)
    status_open = False
    model_telemetry: dict[str, Any] = {}

    def close_status(
        exc_type: type[BaseException] | None = None,
        exc: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        nonlocal status_open
        if not status_open:
            return
        status_open = False
        status_context.__exit__(exc_type, exc, traceback)

    status = status_context.__enter__()
    status_open = True
    turn_stream_sink = ModelTurnStreamSink(
        stream_sink,
        close_status,
        reasoning_sink=getattr(status, "reasoning_delta", None),
    )
    try:
        assistant = chat_completion_messages(
            messages,
            api=config.model_api,
            tools=tools,
            tool_choice=tool_choice,
            selected_model=config.model_name,
            selected_url=config.model_url,
            session_id=config.model_session_id,
            stream_sink=turn_stream_sink if stream_sink is not None else None,
            telemetry_sink=model_telemetry.update,
            thinking=config.thinking,
        )
    except BaseException as exc:
        close_status(type(exc), exc, exc.__traceback__)
        raise
    close_status()
    return assistant, turn_stream_sink.streamed_content, model_telemetry


class ModelTurnStreamSink:
    """Forward model text deltas after clearing the blocking status renderer."""

    def __init__(
        self,
        stream_sink: ChatCompletionStreamSink | None,
        close_status: Callable[
            [type[BaseException] | None, BaseException | None, TracebackType | None],
            None,
        ],
        reasoning_sink: Callable[[str], None] | None = None,
    ) -> None:
        self.stream_sink = stream_sink
        self.close_status = close_status
        self.reasoning_sink = reasoning_sink
        self.streamed_content = False

    def content_delta(self, text: str) -> None:
        if not text:
            return
        self.streamed_content = True
        self.close_status(None, None, None)
        if self.stream_sink is not None:
            self.stream_sink.content_delta(text)

    def reasoning_delta(self, text: str) -> None:
        # Reasoning is process, not answer: it feeds the status renderer
        # while the status is open and never reaches the answer stream.
        if self.reasoning_sink is not None:
            self.reasoning_sink(text)


def model_status_context(
    factory: ModelStatusFactory | None,
) -> AbstractContextManager[object]:
    if factory is None:
        return nullcontext()
    return factory()


def emit_event(
    events: list[dict[str, Any]],
    event: dict[str, Any],
    event_sink: AgentEventSink | None = None,
) -> None:
    events.append(event)
    if event_sink is not None:
        event_sink(event)


@dataclass(frozen=True)
class ToolCallResult:
    events: list[dict[str, Any]]
    staged_effect: dict[str, Any] | None = None
    stop: bool = False


def model_event(assistant: dict[str, Any]) -> dict[str, Any]:
    content = assistant.get("content")
    reasoning = assistant.get("reasoning_content")
    tool_calls = assistant_tool_calls(assistant)
    event: dict[str, Any] = {"type": "model"}
    if isinstance(reasoning, str) and reasoning:
        event["reasoning"] = reasoning
    if isinstance(content, str) and content:
        event["content"] = content
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
    events: list[dict[str, Any]],
    *,
    prompt_trace: PromptTrace | None,
    prompt_builder: PromptBuilder,
    event_sink: AgentEventSink | None,
    caused_by: str | None = None,
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
        prompt_builder=prompt_builder,
    )
    if tool_call_object_ids:
        event["tool_call_object_ids"] = tool_call_object_ids
    if event:
        emit_event(events, event, event_sink)
    return event_id, tool_calls


def next_model_parent(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
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
    call_id = str(tool_call.get("id") or f"call-{index}")
    function = tool_call.get("function")
    if not isinstance(function, dict):
        return {}
    name = str(function.get("name") or "")
    arguments = function.get("arguments")
    params, _ = parse_tool_arguments(arguments)
    event: dict[str, Any] = {
        "type": "tool_call",
        "id": call_id,
        "tool_call_id": call_id,
        "name": name,
        "input": params,
        "arguments": arguments if isinstance(arguments, str) else json.dumps(params),
    }
    if caused_by is not None:
        event["caused_by"] = caused_by
    return event


def handle_tool_call(
    tool_call: dict[str, Any],
    *,
    allowed_tools: tuple[str, ...],
    index: int,
    execution_mode: ExecutionMode = "stage",
    model_telemetry: dict[str, Any] | None = None,
    prompt_trace: PromptTrace | None = None,
    prompt_builder: PromptBuilder | None = None,
    event_sink: AgentEventSink | None = None,
    tool_registry: ToolRegistry | None = None,
    caused_by: str | None = None,
) -> ToolCallResult:
    active_tool_registry = tool_registry or _runtime_tool_registry
    call_id = str(tool_call.get("id") or f"call-{index}")
    function = tool_call.get("function")
    if not isinstance(function, dict):
        return invalid_tool_result(
            call_id,
            "",
            {},
            "invalid-tool-call",
            "tool call did not include a function payload",
            model_telemetry=model_telemetry,
            prompt_trace=prompt_trace,
            prompt_builder=prompt_builder,
            event_sink=event_sink,
            caused_by=caused_by,
        )
    name = str(function.get("name") or "")
    arguments = function.get("arguments")
    params, parse_error = parse_tool_arguments(arguments)
    call_event = {
        "type": "tool_call",
        "id": call_id,
        "tool_call_id": call_id,
        "name": name,
        "input": params,
        "arguments": arguments if isinstance(arguments, str) else json.dumps(params),
    }
    if caused_by is not None:
        call_event["caused_by"] = caused_by

    def reject(code: str, message: str) -> ToolCallResult:
        return invalid_tool_result(
            call_id,
            name,
            params,
            code,
            message,
            call_event=call_event,
            model_telemetry=model_telemetry,
            prompt_trace=prompt_trace,
            prompt_builder=prompt_builder,
            event_sink=event_sink,
        )

    if parse_error:
        return reject("invalid-json-args", parse_error)
    if active_tool_registry.get(name) is None:
        return reject("unknown-tool", f"unknown tool: {name}")
    if name not in allowed_tools:
        return reject(
            "disallowed-tool", f"tool is not allowed in this workflow: {name}"
        )
    schema_errors = active_tool_registry.validate_tool_args(name, params)
    if schema_errors:
        return reject("schema-mismatch", "; ".join(schema_errors))
    events: list[dict[str, Any]] = []
    attach_tool_call_trace(
        call_event,
        prompt_trace=prompt_trace,
        prompt_builder=prompt_builder,
    )
    emit_event(events, call_event, event_sink)
    try:
        result = run_tool(
            name,
            params,
            execution_mode=execution_mode,
            tool_registry=active_tool_registry,
        )
    except Exception as exc:
        result = tool_error("tool-crashed", f"{type(exc).__name__}: {exc}")
    staged_effect = (
        result_staged_effect(result)
        if tool_call_stages_effect(
            name,
            execution_mode,
            tool_registry=active_tool_registry,
        )
        else None
    )
    stop = bool(
        execution_mode == "stage" and name == "edit" and result.get("ok") is True
    )
    emit_event(
        events,
        traced_tool_result_event(
            call_id,
            name,
            result,
            call_event=call_event,
            model_telemetry=model_telemetry,
            prompt_trace=prompt_trace,
            prompt_builder=prompt_builder,
        ),
        event_sink,
    )
    return ToolCallResult(
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
    prompt_builder: PromptBuilder | None = None,
    event_sink: AgentEventSink | None = None,
    caused_by: str | None = None,
) -> ToolCallResult:
    event = call_event or {
        "type": "tool_call",
        "id": call_id,
        "tool_call_id": call_id,
        "name": name,
        "input": params,
    }
    if caused_by is not None:
        event["caused_by"] = caused_by
    events: list[dict[str, Any]] = []
    attach_tool_call_trace(
        event,
        prompt_trace=prompt_trace,
        prompt_builder=prompt_builder,
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
        prompt_builder=prompt_builder,
    )
    emit_event(events, event, event_sink)
    emit_event(
        events,
        result_event,
        event_sink,
    )
    return ToolCallResult(events=events)


def traced_tool_result_event(
    call_id: str,
    name: str,
    result: dict[str, Any],
    *,
    call_event: dict[str, Any],
    model_telemetry: dict[str, Any] | None = None,
    prompt_trace: PromptTrace | None = None,
    prompt_builder: PromptBuilder | None = None,
) -> dict[str, Any]:
    event = tool_result_event(
        call_id,
        name,
        result,
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
    model_telemetry: dict[str, Any] | None = None,
    prompt_trace: PromptTrace | None = None,
) -> dict[str, Any]:
    event = {
        "type": "tool_result",
        "tool_call_id": call_id,
        "name": name,
        "result": result,
    }
    ensure_event_id(event)
    if model_telemetry:
        event["model_telemetry"] = dict(model_telemetry)
    if prompt_trace is not None:
        attach_prompt_trace(event, prompt_trace)
    return event


def tool_error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def tool_call_stages_effect(
    name: str,
    execution_mode: ExecutionMode,
    *,
    tool_registry: ToolRegistry | None = None,
) -> bool:
    if execution_mode != "stage":
        return False
    active_tool_registry = tool_registry or _runtime_tool_registry
    tool = active_tool_registry.get(name)
    return tool is not None and tool.spec.mutates()


def result_staged_effect(result: dict[str, Any]) -> dict[str, Any] | None:
    return proposed_effect(result)
