"""Headless native-tool-call agent loop for Zeta."""

from __future__ import annotations

from contextlib import nullcontext
import itertools
import json
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Callable, ContextManager, Iterable, Literal, cast

from ..protocols import is_shell_prompt_handoff
from . import runtime
from .model import (
    ChatCompletionStreamSink,
    chat_completion_messages,
    model_endpoint_open,
)
from .tools import (
    allowed_tool_names,
    analyze_tool,
    model_tool_descriptors,
    run_tool,
    validate_tool_args,
)

EditMode = Literal["review_patch", "direct_replace"]
ExecutionMode = Literal["handoff", "direct"]
AgentEventSink = Callable[[dict[str, Any]], None]
ModelStatusFactory = Callable[[], ContextManager[object]]


@dataclass(frozen=True)
class AgentConfig:
    """Configuration for one Zeta turn."""

    system_prompt: str | None = None
    allowed_tools: Iterable[str] | None = None
    max_turns: int | None = None
    stop_on_handoff: bool = True
    edit_mode: EditMode = "review_patch"
    execution_mode: ExecutionMode = "handoff"
    model_profile: str | None = None
    model_name: str | None = None
    model_url: str | None = None


@dataclass(frozen=True)
class AgentTurnResult:
    """Result from one native tool-call loop."""

    final_text: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    handoff: dict[str, Any] | None = None
    final_text_streamed: bool = False
    model_telemetry: dict[str, Any] = field(default_factory=dict)


def run_agent_turn(
    objective: str,
    transcript: list[dict[str, Any]],
    config: AgentConfig,
    *,
    context: str = "",
    event_sink: AgentEventSink | None = None,
    model_status: ModelStatusFactory | None = None,
    stream_sink: ChatCompletionStreamSink | None = None,
) -> AgentTurnResult:
    """Run an assistant/tool loop without mutating session state."""
    if config.model_url is None:
        endpoint_open = model_endpoint_open()
    else:
        endpoint_open = model_endpoint_open(config.model_url)
    if not endpoint_open:
        raise RuntimeError("model endpoint is not reachable")
    if config.allowed_tools is None:
        allowed_tools = tuple(allowed_tool_names())
    else:
        allowed_tools = tuple(config.allowed_tools)
    events: list[dict[str, Any]] = []
    latest_model_telemetry: dict[str, Any] = {}
    for _ in turn_indices(config.max_turns):
        assistant, streamed_content, model_telemetry = request_assistant_message(
            runtime.zeta_chat_messages(
                objective,
                transcript,
                system=config.system_prompt,
                allowed_tools=allowed_tools,
                context=context,
                current_events=events,
            ),
            allowed_tools=allowed_tools,
            config=config,
            model_status=model_status,
            stream_sink=stream_sink,
        )
        if model_telemetry:
            latest_model_telemetry = model_telemetry
        message_event = assistant_message_event(assistant)
        if message_event:
            emit_event(events, message_event, event_sink)
        tool_calls = assistant_tool_calls(assistant)
        if not tool_calls:
            return AgentTurnResult(
                final_text=str(assistant.get("content") or ""),
                events=events,
                final_text_streamed=streamed_content,
                model_telemetry=latest_model_telemetry,
            )
        for index, tool_call in enumerate(tool_calls):
            result_event = handle_tool_call(
                tool_call,
                allowed_tools=allowed_tools,
                index=index,
                edit_mode=config.edit_mode,
                execution_mode=config.execution_mode,
                model_telemetry=(model_telemetry if index == 0 else None),
                event_sink=event_sink,
            )
            events.extend(result_event.events)
            if result_event.handoff is not None and config.stop_on_handoff:
                return AgentTurnResult(
                    final_text="",
                    events=events,
                    handoff=result_event.handoff,
                    model_telemetry=latest_model_telemetry,
                )
            if result_event.stop:
                return AgentTurnResult(
                    events=events,
                    model_telemetry=latest_model_telemetry,
                )
    return AgentTurnResult(events=events, model_telemetry=latest_model_telemetry)


def turn_indices(max_turns: int | None) -> Iterable[int]:
    if max_turns is None:
        return itertools.count()
    return range(max(max_turns, 0))


def request_assistant_message(
    messages: list[dict[str, Any]],
    *,
    allowed_tools: tuple[str, ...],
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

    turn_stream_sink = ModelTurnStreamSink(stream_sink, close_status)
    status_context.__enter__()
    status_open = True
    try:
        assistant = chat_completion_messages(
            messages,
            tools=model_tool_descriptors(allowed_tools),
            tool_choice="auto",
            selected_model=config.model_name,
            selected_url=config.model_url,
            stream_sink=turn_stream_sink if stream_sink is not None else None,
            telemetry_sink=model_telemetry.update,
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
    ) -> None:
        self.stream_sink = stream_sink
        self.close_status = close_status
        self.streamed_content = False

    def content_delta(self, text: str) -> None:
        if not text:
            return
        self.streamed_content = True
        self.close_status(None, None, None)
        if self.stream_sink is not None:
            self.stream_sink.content_delta(text)


def model_status_context(
    factory: ModelStatusFactory | None,
) -> ContextManager[object]:
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
    handoff: dict[str, Any] | None = None
    stop: bool = False


def assistant_message_event(assistant: dict[str, Any]) -> dict[str, Any]:
    content = assistant.get("content")
    tool_calls = assistant_tool_calls(assistant)
    event: dict[str, Any] = {"type": "assistant_message"}
    if isinstance(content, str) and content:
        event["content"] = content
    if tool_calls:
        event["tool_calls"] = tool_calls
    return event


def assistant_tool_calls(assistant: dict[str, Any]) -> list[dict[str, Any]]:
    raw_tool_calls = assistant.get("tool_calls")
    if not isinstance(raw_tool_calls, list):
        return []
    return [call for call in raw_tool_calls if isinstance(call, dict)]


def handle_tool_call(
    tool_call: dict[str, Any],
    *,
    allowed_tools: tuple[str, ...],
    index: int,
    edit_mode: EditMode = "review_patch",
    execution_mode: ExecutionMode = "handoff",
    model_telemetry: dict[str, Any] | None = None,
    event_sink: AgentEventSink | None = None,
) -> ToolCallResult:
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
            event_sink=event_sink,
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
    if parse_error:
        return invalid_tool_result(
            call_id,
            name,
            params,
            "invalid-json-args",
            parse_error,
            call_event=call_event,
            model_telemetry=model_telemetry,
            event_sink=event_sink,
        )
    if name not in allowed_tool_names():
        return invalid_tool_result(
            call_id,
            name,
            params,
            "unknown-tool",
            f"unknown tool: {name}",
            call_event=call_event,
            model_telemetry=model_telemetry,
            event_sink=event_sink,
        )
    if name not in allowed_tools:
        return invalid_tool_result(
            call_id,
            name,
            params,
            "disallowed-tool",
            f"tool is not allowed in this route: {name}",
            call_event=call_event,
            model_telemetry=model_telemetry,
            event_sink=event_sink,
        )
    schema_errors = validate_tool_args(name, params)
    if schema_errors:
        return invalid_tool_result(
            call_id,
            name,
            params,
            "schema-mismatch",
            "; ".join(schema_errors),
            call_event=call_event,
            model_telemetry=model_telemetry,
            event_sink=event_sink,
        )
    analysis = analyze_tool(name, params)
    analysis_event = {
        "type": "tool_analysis",
        "tool_call_id": call_id,
        "name": name,
        "analysis": analysis,
    }
    if analysis.get("valid") is not True:
        result = tool_error("invalid-analysis", "tool analysis rejected the input")
        events: list[dict[str, Any]] = []
        emit_event(events, call_event, event_sink)
        emit_event(events, analysis_event, event_sink)
        emit_event(
            events,
            tool_result_event(
                call_id,
                name,
                result,
                model_telemetry=model_telemetry,
            ),
            event_sink,
        )
        return ToolCallResult(events=events)
    events = []
    emit_event(events, call_event, event_sink)
    emit_event(events, analysis_event, event_sink)
    result = run_tool_for_mode(
        name,
        params,
        edit_mode=edit_mode,
        execution_mode=execution_mode,
    )
    handoff = result_handoff(result)
    stop = bool(
        execution_mode == "handoff" and name == "edit" and result.get("ok") is True
    )
    emit_event(
        events,
        tool_result_event(
            call_id,
            name,
            result,
            model_telemetry=model_telemetry,
        ),
        event_sink,
    )
    return ToolCallResult(
        events=events,
        handoff=handoff,
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


def run_tool_for_mode(
    name: str,
    params: dict[str, Any],
    *,
    edit_mode: EditMode,
    execution_mode: ExecutionMode,
) -> dict[str, Any]:
    if execution_mode == "direct":
        return run_tool(
            name,
            params,
            edit_mode=edit_mode,
            execution_mode=execution_mode,
        )
    if name == "edit":
        return run_tool(name, params, edit_mode=edit_mode)
    return run_tool(name, params)


def invalid_tool_result(
    call_id: str,
    name: str,
    params: dict[str, Any],
    code: str,
    message: str,
    *,
    call_event: dict[str, Any] | None = None,
    model_telemetry: dict[str, Any] | None = None,
    event_sink: AgentEventSink | None = None,
) -> ToolCallResult:
    event = call_event or {
        "type": "tool_call",
        "id": call_id,
        "tool_call_id": call_id,
        "name": name,
        "input": params,
    }
    events: list[dict[str, Any]] = []
    emit_event(events, event, event_sink)
    emit_event(
        events,
        tool_result_event(
            call_id,
            name,
            tool_error(code, message),
            model_telemetry=model_telemetry,
        ),
        event_sink,
    )
    return ToolCallResult(events=events)


def tool_result_event(
    call_id: str,
    name: str,
    result: dict[str, Any],
    *,
    model_telemetry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event = {
        "type": "tool_result",
        "tool_call_id": call_id,
        "name": name,
        "result": result,
    }
    if model_telemetry:
        event["model_telemetry"] = dict(model_telemetry)
    return event


def tool_error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def result_handoff(result: dict[str, Any]) -> dict[str, Any] | None:
    handoff = result.get("handoff")
    if is_shell_prompt_handoff(handoff):
        return cast(dict[str, Any], handoff)
    return None
