"""OpenAI-compatible chat completions transport for Zeta."""

import json
from collections.abc import Callable
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from zeta.models.limits import model_context_tokens
from zeta.models.profiles import model_name, model_url
from zeta.models.sse import (
    ChatCompletionStreamSink,
    read_streamed_chat_completion,
    stream_json_sse,
)
from zeta.models.types import ModelInput, ModelOutput, ModelUsage, normalized_usage

DEFAULT_MAX_COMPLETION_TOKENS = 8192


ModelTelemetrySink = Callable[[dict[str, Any]], None]


async def request_chat_completion(
    body: dict[str, Any],
    *,
    selected_url: str | None = None,
    stream_sink: ChatCompletionStreamSink | None = None,
    should_stop: Callable[[], str | None] | None = None,
) -> dict[str, Any]:
    """POST one streaming chat completions request and return the final response."""
    stream_body = {**body, "stream": True}
    payload = await read_streamed_chat_completion(
        stream_json_sse(
            model_url(selected_url),
            stream_body,
            headers={"Accept": "text/event-stream"},
            should_stop=should_stop,
        ),
        stream_sink=stream_sink,
    )
    if not isinstance(payload, dict):
        raise RuntimeError("model request failed: response was not a JSON object")
    return payload


def model_telemetry(
    payload: dict[str, Any],
    *,
    context_tokens: int | None = None,
) -> dict[str, Any]:
    telemetry: dict[str, Any] = {}
    usage = normalized_usage(payload.get("usage"))
    if usage is not None:
        telemetry["usage"] = usage
    if context_tokens is not None:
        telemetry["model_context_tokens"] = context_tokens
    return telemetry


def emit_model_telemetry(
    payload: dict[str, Any],
    *,
    context_tokens: int | None,
    telemetry_sink: ModelTelemetrySink | None,
) -> None:
    if telemetry_sink is None:
        return
    telemetry = model_telemetry(payload, context_tokens=context_tokens)
    if telemetry:
        telemetry_sink(telemetry)


async def chat_completion_messages(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] = "auto",
    max_tokens: int = DEFAULT_MAX_COMPLETION_TOKENS,
    selected_model: str | None = None,
    selected_url: str | None = None,
    stream_sink: ChatCompletionStreamSink | None = None,
    telemetry_sink: ModelTelemetrySink | None = None,
    thinking: str | None = None,
    should_stop: Callable[[], str | None] | None = None,
) -> dict[str, Any]:
    """Request one native OpenAI-compatible chat completion message."""
    context_tokens = model_context_tokens(selected_url, selected_model)
    body = chat_completion_request_body(
        messages,
        tools=tools,
        tool_choice=tool_choice,
        max_tokens=max_tokens,
        selected_model=selected_model,
        thinking=thinking,
    )
    payload = await request_chat_completion(
        body,
        selected_url=selected_url,
        stream_sink=stream_sink,
        should_stop=should_stop,
    )
    emit_model_telemetry(
        payload,
        context_tokens=context_tokens,
        telemetry_sink=telemetry_sink,
    )
    output = model_output_from_chat_completion(payload)
    if output.finish_reason == "length" and output.message.get("tool_calls"):
        raise RuntimeError(
            "model request failed: the response hit max_tokens in the middle "
            "of a tool call, leaving its arguments incomplete"
        )
    return output.message


def chat_completion_request_body(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] = "auto",
    max_tokens: int = DEFAULT_MAX_COMPLETION_TOKENS,
    selected_model: str | None = None,
    response_format: dict[str, Any] | None = None,
    thinking: str | None = None,
) -> dict[str, Any]:
    """Build the OpenAI-compatible chat completions request body.

    `thinking` uses the reasoning-effort vocabulary: `None` leaves the
    model's default in place, `"none"` disables thinking, and an effort
    level is sent as `reasoning_effort`.
    """
    body: dict[str, Any] = {
        "model": model_name(selected_model),
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "stream_options": {"include_usage": True},
    }
    if thinking == "none":
        body["chat_template_kwargs"] = {"enable_thinking": False}
    elif thinking is not None:
        body["reasoning_effort"] = thinking
    if tools:
        body["tools"] = tools
        body["tool_choice"] = tool_choice
    if response_format is not None:
        body["response_format"] = response_format
    return body


def chat_completion_request_from_input(model_input: ModelInput) -> dict[str, Any]:
    return chat_completion_request_body(
        model_input.messages,
        tools=model_input.tools,
        tool_choice=model_input.tool_choice,
        max_tokens=model_input.max_tokens or DEFAULT_MAX_COMPLETION_TOKENS,
        selected_model=model_input.selected_model,
        thinking=model_input.thinking,
    )


def model_output_from_chat_completion(payload: dict[str, Any]) -> ModelOutput:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("model request failed: response choices were invalid")
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        raise RuntimeError("model request failed: response choice was invalid")
    if not isinstance(first_choice.get("message"), dict):
        raise RuntimeError("model request failed: assistant message was invalid")
    message = dict(first_choice["message"])
    usage_payload = payload.get("usage")
    replay_items = message.get("_responses_items")
    return ModelOutput(
        message=message,
        finish_reason=first_choice.get("finish_reason")
        if isinstance(first_choice.get("finish_reason"), str)
        else None,
        usage=model_usage_from_payload(usage_payload)
        if isinstance(usage_payload, dict)
        else None,
        provider_metadata={
            key: value
            for key in ("id", "object", "created", "model", "system_fingerprint")
            if (value := payload.get(key)) is not None
        },
        provider_replay_items=tuple(
            item for item in replay_items if isinstance(item, dict)
        )
        if isinstance(replay_items, list)
        else (),
    )


def model_usage_from_payload(payload: dict[str, Any]) -> ModelUsage | None:
    usage = ModelUsage(
        prompt_tokens=usage_token_count(payload.get("prompt_tokens")),
        completion_tokens=usage_token_count(payload.get("completion_tokens")),
        total_tokens=usage_token_count(payload.get("total_tokens")),
    )
    if (
        usage.prompt_tokens is None
        and usage.completion_tokens is None
        and usage.total_tokens is None
    ):
        return None
    return usage


def usage_token_count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def json_schema_response_format(
    *,
    name: str,
    schema: dict[str, Any],
    strict: bool = True,
) -> dict[str, Any]:
    """Return an OpenAI-compatible structured-output response format."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": strict,
            "schema": schema,
        },
    }


async def chat_structured_output(
    messages: list[dict[str, Any]],
    *,
    schema: dict[str, Any],
    response_name: str,
    max_tokens: int = DEFAULT_MAX_COMPLETION_TOKENS,
    selected_model: str | None = None,
    selected_url: str | None = None,
) -> dict[str, Any]:
    """Request one JSON object using structured outputs and validate it."""
    body = chat_completion_request_body(
        messages,
        max_tokens=max_tokens,
        selected_model=selected_model,
        response_format=json_schema_response_format(
            name=response_name,
            schema=schema,
        ),
    )
    payload = await request_chat_completion(body, selected_url=selected_url)
    message = payload["choices"][0]["message"]
    if not isinstance(message, dict):
        raise RuntimeError("model request failed: assistant message was invalid")
    data = parse_structured_message_content(message.get("content"))
    try:
        Draft202012Validator(schema).validate(data)
    except ValidationError as exc:
        raise RuntimeError(f"model structured output failed validation: {exc}") from exc
    return data


def parse_structured_message_content(content: Any) -> dict[str, Any]:
    if not isinstance(content, str):
        raise RuntimeError("model structured output was not a JSON string")
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"model structured output was invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("model structured output was not a JSON object")
    return data
