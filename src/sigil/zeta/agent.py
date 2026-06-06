"""Headless native-tool-call agent loop for Zeta."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, cast

from ..protocols import is_shell_prompt_handoff
from . import runtime
from .model import chat_completion_messages, model_endpoint_open
from .tools import (
    allowed_tool_names,
    analyze_tool,
    model_tool_descriptors,
    run_tool,
    validate_tool_args,
)

EditMode = Literal["review_patch", "direct_replace"]
ExecutionMode = Literal["handoff", "direct"]


@dataclass(frozen=True)
class AgentConfig:
    """Configuration for one bounded Zeta turn."""

    system_prompt: str | None = None
    allowed_tools: Iterable[str] | None = None
    max_turns: int = 8
    stop_on_handoff: bool = True
    edit_mode: EditMode = "review_patch"
    execution_mode: ExecutionMode = "handoff"
    model_profile: str | None = None
    model_name: str | None = None
    model_url: str | None = None


@dataclass(frozen=True)
class AgentTurnResult:
    """Result from one bounded native tool-call loop."""

    final_text: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    handoff: dict[str, Any] | None = None


def run_agent_turn(
    objective: str,
    transcript: list[dict[str, Any]],
    config: AgentConfig,
    *,
    context: str = "",
) -> AgentTurnResult:
    """Run a bounded assistant/tool loop without mutating session state."""
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
    for _ in range(config.max_turns):
        assistant = chat_completion_messages(
            runtime.zeta_chat_messages(
                objective,
                [*transcript, *events],
                system=config.system_prompt,
                allowed_tools=allowed_tools,
                context=context,
            ),
            tools=model_tool_descriptors(allowed_tools),
            tool_choice="auto",
            selected_model=config.model_name,
            selected_url=config.model_url,
        )
        message_event = assistant_message_event(assistant)
        if message_event:
            events.append(message_event)
        tool_calls = assistant_tool_calls(assistant)
        if not tool_calls:
            return AgentTurnResult(
                final_text=str(assistant.get("content") or ""),
                events=events,
            )
        for index, tool_call in enumerate(tool_calls):
            result_event = handle_tool_call(
                tool_call,
                allowed_tools=allowed_tools,
                index=index,
                edit_mode=config.edit_mode,
                execution_mode=config.execution_mode,
            )
            events.extend(result_event.events)
            if result_event.handoff is not None and config.stop_on_handoff:
                return AgentTurnResult(
                    final_text="",
                    events=events,
                    handoff=result_event.handoff,
                )
            if result_event.stop:
                return AgentTurnResult(events=events)
    return AgentTurnResult(events=events)


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
        )
    if name not in allowed_tool_names():
        return invalid_tool_result(
            call_id,
            name,
            params,
            "unknown-tool",
            f"unknown tool: {name}",
            call_event=call_event,
        )
    if name not in allowed_tools:
        return invalid_tool_result(
            call_id,
            name,
            params,
            "disallowed-tool",
            f"tool is not allowed in this route: {name}",
            call_event=call_event,
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
        return ToolCallResult(
            events=[
                call_event,
                analysis_event,
                tool_result_event(call_id, name, result),
            ]
        )
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
    return ToolCallResult(
        events=[call_event, analysis_event, tool_result_event(call_id, name, result)],
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
) -> ToolCallResult:
    event = call_event or {
        "type": "tool_call",
        "id": call_id,
        "tool_call_id": call_id,
        "name": name,
        "input": params,
    }
    return ToolCallResult(
        events=[event, tool_result_event(call_id, name, tool_error(code, message))]
    )


def tool_result_event(
    call_id: str,
    name: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_call_id": call_id,
        "name": name,
        "result": result,
    }


def tool_error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def result_handoff(result: dict[str, Any]) -> dict[str, Any] | None:
    handoff = result.get("handoff")
    if is_shell_prompt_handoff(handoff):
        return cast(dict[str, Any], handoff)
    return None
