"""Translation between chat-completions messages and the OpenAI Responses API.

The package keeps the chat-completions message dict as its internal format;
this module converts at the wire boundary. Assistant messages carry the raw
Responses output items under ``_responses_items`` so reasoning items (with
their encrypted content) and item ids replay verbatim on the next request.
"""

import json
import os
import time
from collections.abc import Iterable
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from zeta.models import ModelInput, ModelOutput
from zeta.models.chat_completions import (
    DEFAULT_MAX_COMPLETION_TOKENS,
    ChatCompletionStreamSink,
    ModelTelemetrySink,
    emit_model_telemetry,
    model_first_output_timeout,
    model_idle_timeout,
    parse_structured_message_content,
    stream_json_sse,
)
from zeta.models.codex_auth import CodexCredentials, load_codex_credentials
from zeta.models.profiles import DEFAULT_CODEX_BASE_URL

CODEX_ORIGINATOR = "zeta"
CODEX_CONTEXT_TOKENS = {"gpt-5.3-codex-spark": 128_000}
DEFAULT_CODEX_CONTEXT_TOKENS = 272_000

RESPONSES_ITEMS_FIELD = "_responses_items"
REASONING_SUMMARY_SEPARATOR = "\n\n"
QUOTA_ERROR_CODES = ("usage_limit_reached", "usage_not_included")
TERMINAL_EVENT_TYPES = (
    "response.completed",
    "response.done",
    "response.incomplete",
)

REASONING_EFFORT_BY_THINKING = {
    "none": "low",
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
}
DEFAULT_REASONING_EFFORT = "medium"


def responses_session_id() -> str:
    return os.environ.get("ZETA_SESSION_ID") or "default"


def responses_request_body(
    messages: list[dict[str, Any]],
    *,
    model: str,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] = "auto",
    max_tokens: int = DEFAULT_MAX_COMPLETION_TOKENS,
    thinking: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Build a streaming Responses request from chat-completions inputs.

    The Codex backend rejects ``store: true`` (statefulness comes from
    resending the full input with a stable ``prompt_cache_key``) and
    rejects ``max_output_tokens``, so ``max_tokens`` is accepted for
    signature parity with the chat client but never sent.
    """
    del max_tokens
    body: dict[str, Any] = {
        "model": model,
        "stream": True,
        "store": False,
        "input": responses_input_items(messages),
        "include": ["reasoning.encrypted_content"],
        "reasoning": {
            "effort": reasoning_effort(thinking),
            "summary": "auto",
        },
    }
    instructions = responses_instructions(messages)
    if instructions:
        body["instructions"] = instructions
    if session_id:
        body["prompt_cache_key"] = session_id
    if tools:
        body["tools"] = [responses_tool(tool) for tool in tools]
        body["tool_choice"] = tool_choice if isinstance(tool_choice, str) else "auto"
        body["parallel_tool_calls"] = True
    return body


def responses_request_from_input(model_input: ModelInput) -> dict[str, Any]:
    return responses_request_body(
        model_input.messages,
        model=codex_model_name(model_input.selected_model),
        tools=model_input.tools,
        tool_choice=model_input.tool_choice,
        max_tokens=model_input.max_tokens or DEFAULT_MAX_COMPLETION_TOKENS,
        thinking=model_input.thinking,
        session_id=model_input.session_id,
    )


def responses_instructions(messages: list[dict[str, Any]]) -> str:
    """Join system messages into the top-level instructions field."""
    parts = [
        str(message.get("content") or "")
        for message in messages
        if message.get("role") == "system"
    ]
    return "\n\n".join(part for part in parts if part)


def responses_input_items(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert chat messages to Responses input items, system excluded."""
    items: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "")
        if role == "system":
            continue
        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": str(message.get("tool_call_id") or ""),
                    "output": str(message.get("content") or ""),
                }
            )
            continue
        if role == "assistant":
            items.extend(assistant_input_items(message))
            continue
        items.append(
            {
                "type": "message",
                "role": role or "user",
                "content": [
                    {"type": "input_text", "text": str(message.get("content") or "")}
                ],
            }
        )
    return items


def assistant_input_items(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Replay recorded output items verbatim, or rebuild them from the dict."""
    recorded = message.get(RESPONSES_ITEMS_FIELD)
    if isinstance(recorded, list) and recorded:
        return [item for item in recorded if isinstance(item, dict)]
    items: list[dict[str, Any]] = []
    content = message.get("content")
    if content:
        items.append(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": str(content)}],
            }
        )
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        function = function if isinstance(function, dict) else {}
        items.append(
            {
                "type": "function_call",
                "call_id": str(call.get("id") or ""),
                "name": str(function.get("name") or ""),
                "arguments": str(function.get("arguments") or ""),
            }
        )
    return items


def responses_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Flatten a chat-completions tool descriptor to the Responses shape."""
    function = tool.get("function")
    function = function if isinstance(function, dict) else {}
    return {
        "type": "function",
        "name": str(function.get("name") or ""),
        "description": str(function.get("description") or ""),
        "parameters": function.get("parameters"),
        "strict": None,
    }


def reasoning_effort(thinking: str | None) -> str:
    """Map the thinking-effort vocabulary onto Responses reasoning effort."""
    if thinking is None:
        return DEFAULT_REASONING_EFFORT
    return REASONING_EFFORT_BY_THINKING.get(thinking, DEFAULT_REASONING_EFFORT)


def codex_responses_url(base_url: str | None) -> str:
    """Return the Codex Responses endpoint under the configured base URL."""
    base = (base_url or DEFAULT_CODEX_BASE_URL).rstrip("/")
    return f"{base}/codex/responses"


def codex_request_headers(
    credentials: CodexCredentials,
    session: str,
) -> dict[str, str]:
    """Return the identity and protocol headers the Codex backend expects."""
    return {
        "Authorization": f"Bearer {credentials.access_token}",
        "chatgpt-account-id": credentials.account_id,
        "originator": CODEX_ORIGINATOR,
        "OpenAI-Beta": "responses=experimental",
        "session-id": session,
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }


def codex_context_tokens(model: str) -> int:
    return CODEX_CONTEXT_TOKENS.get(model, DEFAULT_CODEX_CONTEXT_TOKENS)


def request_codex_response(
    body: dict[str, Any],
    *,
    selected_url: str | None = None,
    session: str,
    stream_sink: ChatCompletionStreamSink | None = None,
) -> dict[str, Any]:
    """POST one streaming Codex request and return the final payload."""
    credentials = load_codex_credentials()
    return read_streamed_responses(
        stream_json_sse(
            codex_responses_url(selected_url),
            body,
            headers=codex_request_headers(credentials, session),
            first_output_timeout=model_first_output_timeout(),
            idle_timeout=model_idle_timeout(),
        ),
        stream_sink=stream_sink,
    )


def codex_model_name(selected_model: str | None) -> str:
    if selected_model:
        return selected_model
    raise RuntimeError(
        "model request failed: a codex-responses profile must name its model"
    )


def codex_completion_messages(
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
    session_id: str | None = None,
) -> dict[str, Any]:
    """Request one assistant message from the Codex Responses backend."""
    model = codex_model_name(selected_model)
    session = session_id or responses_session_id()
    body = responses_request_body(
        messages,
        model=model,
        tools=tools,
        tool_choice=tool_choice,
        max_tokens=max_tokens,
        thinking=thinking,
        session_id=session,
    )
    payload = request_codex_response(
        body,
        selected_url=selected_url,
        session=session,
        stream_sink=stream_sink,
    )
    emit_model_telemetry(
        payload,
        context_tokens=codex_context_tokens(model),
        telemetry_sink=telemetry_sink,
    )
    output = model_output_from_responses_payload(payload)
    if output.finish_reason == "length" and output.message.get("tool_calls"):
        raise RuntimeError(
            "model request failed: the response hit max_tokens in the middle "
            "of a tool call, leaving its arguments incomplete"
        )
    return output.message


def model_output_from_responses_payload(payload: dict[str, Any]) -> ModelOutput:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("model request failed: response choices were invalid")
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        raise RuntimeError("model request failed: response choice was invalid")
    if not isinstance(first_choice.get("message"), dict):
        raise RuntimeError("model request failed: assistant message was invalid")
    return ModelOutput.from_chat_completion(payload)


def codex_structured_output(
    messages: list[dict[str, Any]],
    *,
    schema: dict[str, Any],
    response_name: str,
    max_tokens: int = DEFAULT_MAX_COMPLETION_TOKENS,
    selected_model: str | None = None,
    selected_url: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Request one schema-validated JSON object from the Codex backend."""
    session = session_id or responses_session_id()
    body = responses_request_body(
        messages,
        model=codex_model_name(selected_model),
        max_tokens=max_tokens,
        session_id=session,
    )
    body["text"] = {
        "format": {
            "type": "json_schema",
            "name": response_name,
            "strict": True,
            "schema": schema,
        }
    }
    payload = request_codex_response(body, selected_url=selected_url, session=session)
    message = payload["choices"][0]["message"]
    if not isinstance(message, dict):
        raise RuntimeError("model request failed: assistant message was invalid")
    data = parse_structured_message_content(message.get("content"))
    try:
        Draft202012Validator(schema).validate(data)
    except ValidationError as exc:
        raise RuntimeError(f"model structured output failed validation: {exc}") from exc
    return data


def read_streamed_responses(
    events: Iterable[str],
    *,
    stream_sink: ChatCompletionStreamSink | None = None,
) -> dict[str, Any]:
    """Read Responses SSE frames into one chat-completions-shaped payload."""
    accumulator = ResponsesStreamAccumulator(stream_sink=stream_sink)
    for data in events:
        if data == "[DONE]":
            break
        try:
            event = json.loads(data)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"model stream failed: invalid JSON event: {exc}"
            ) from exc
        if not isinstance(event, dict):
            raise RuntimeError("model stream failed: event was not a JSON object")
        accumulator.add_event(event)
    return accumulator.response()


class ResponsesStreamAccumulator:
    """Accumulate Responses stream events into a final assistant message."""

    def __init__(
        self,
        *,
        stream_sink: ChatCompletionStreamSink | None = None,
    ) -> None:
        self.stream_sink = stream_sink
        self.items: list[dict[str, Any]] = []
        self.content: list[str] = []
        self.reasoning: list[str] = []
        self.tool_calls: list[dict[str, Any]] = []
        self.usage: dict[str, int] | None = None
        self.status: str | None = None
        self.response_id: str | None = None

    def add_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        if event_type == "error":
            raise RuntimeError(f"model request failed: {responses_error(event)}")
        if event_type == "response.failed":
            raise_response_failure(event)
        if event_type == "response.created":
            self.add_created(event)
        elif event_type in (
            "response.reasoning_summary_text.delta",
            "response.reasoning_text.delta",
        ):
            self.add_reasoning_delta(str(event.get("delta") or ""))
        elif event_type == "response.reasoning_summary_part.done":
            self.add_reasoning_delta(REASONING_SUMMARY_SEPARATOR)
        elif event_type in ("response.output_text.delta", "response.refusal.delta"):
            self.add_content_delta(str(event.get("delta") or ""))
        elif event_type == "response.output_item.done":
            self.add_item(event.get("item"))
        elif event_type in TERMINAL_EVENT_TYPES:
            self.add_terminal(event_type, event)

    def add_created(self, event: dict[str, Any]) -> None:
        response = event.get("response")
        if isinstance(response, dict):
            self.response_id = str(response.get("id") or "") or None

    def add_reasoning_delta(self, text: str) -> None:
        if not text:
            return
        self.reasoning.append(text)
        if self.stream_sink is not None:
            self.stream_sink.reasoning_delta(text)

    def add_content_delta(self, text: str) -> None:
        if not text:
            return
        self.content.append(text)
        if self.stream_sink is not None:
            self.stream_sink.content_delta(text)

    def add_item(self, item: Any) -> None:
        if not isinstance(item, dict):
            return
        self.items.append(item)
        if item.get("type") == "function_call":
            self.tool_calls.append(
                {
                    "id": str(item.get("call_id") or ""),
                    "type": "function",
                    "function": {
                        "name": str(item.get("name") or ""),
                        "arguments": str(item.get("arguments") or ""),
                    },
                }
            )

    def add_terminal(self, event_type: str, event: dict[str, Any]) -> None:
        response = event.get("response")
        response = response if isinstance(response, dict) else {}
        status = str(response.get("status") or "")
        if not status:
            status = "incomplete" if event_type.endswith("incomplete") else "completed"
        self.status = status
        usage = response.get("usage")
        if isinstance(usage, dict):
            self.usage = responses_usage(usage)

    def response(self) -> dict[str, Any]:
        if self.status is None:
            raise RuntimeError(
                "model stream failed: stream ended before response.completed"
            )
        message = self.final_message()
        finish_reason = "stop"
        if self.status == "incomplete":
            finish_reason = "length"
        elif self.tool_calls:
            finish_reason = "tool_calls"
        payload: dict[str, Any] = {
            "choices": [{"message": message, "finish_reason": finish_reason}]
        }
        if self.usage is not None:
            payload["usage"] = self.usage
        return payload

    def final_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {
            "role": "assistant",
            "content": self.final_content(),
        }
        reasoning = "".join(self.reasoning).strip()
        if reasoning:
            message["reasoning_content"] = reasoning
        if self.tool_calls:
            message["tool_calls"] = self.tool_calls
        if self.items:
            message[RESPONSES_ITEMS_FIELD] = self.items
        return message

    def final_content(self) -> str | None:
        parts = [
            str(part.get("text") or "")
            for item in self.items
            if item.get("type") == "message"
            for part in item.get("content") or []
            if isinstance(part, dict) and part.get("type") == "output_text"
        ]
        text = "".join(parts) or "".join(self.content)
        if text:
            return text
        return None if self.tool_calls else ""


def responses_usage(usage: dict[str, Any]) -> dict[str, int]:
    """Normalize Responses usage onto the chat-completions token fields."""
    mapped = {
        "prompt_tokens": usage.get("input_tokens"),
        "completion_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }
    return {
        key: value
        for key, value in mapped.items()
        if isinstance(value, int) and not isinstance(value, bool)
    }


def responses_error(error: dict[str, Any]) -> str:
    message = error.get("message")
    if isinstance(message, str) and message:
        return message
    return json.dumps(error, sort_keys=True)


def raise_response_failure(event: dict[str, Any]) -> None:
    response = event.get("response")
    response = response if isinstance(response, dict) else {}
    error = response.get("error")
    error = error if isinstance(error, dict) else {}
    code = str(error.get("code") or "")
    if code in QUOTA_ERROR_CODES:
        raise RuntimeError(quota_error_message(error))
    raise RuntimeError(f"model request failed: {responses_error(error)}")


def quota_error_message(error: dict[str, Any]) -> str:
    plan = str(error.get("plan_type") or "")
    message = "you have hit your ChatGPT usage limit"
    if plan:
        message += f" ({plan} plan)"
    resets_at = error.get("resets_at")
    if isinstance(resets_at, int) and not isinstance(resets_at, bool):
        minutes = max(0, round((resets_at - time.time()) / 60))
        message += f"; it resets in ~{minutes} min"
    return f"model request failed: {message}"
