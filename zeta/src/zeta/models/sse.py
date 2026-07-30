"""Server-sent event decoding for streamed chat completions.

A streamed completion arrives as SSE lines. These helpers decode that stream
and accumulate the deltas into one assistant message.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from typing import Any, Protocol

from zeta.models.limits import (
    model_first_output_timeout,
    model_idle_timeout,
    model_stream_timeout,
)
from zeta.models.types import normalized_usage, tool_call_id


class ChatCompletionStreamSink(Protocol):
    """Receive visible chat completion stream events."""

    def content_delta(self, text: str) -> None:
        """Handle one visible assistant text delta."""
        ...

    def reasoning_delta(self, text: str) -> None:
        """Handle one model reasoning text delta."""
        ...


def stream_json_sse(
    url: str,
    body: dict[str, Any],
    *,
    headers: Mapping[str, str],
    first_output_timeout: float | None = None,
    idle_timeout: float | None = None,
) -> Iterator[str]:
    """POST JSON and yield Server-Sent Event data payloads."""
    import httpx

    timeout = model_stream_timeout(
        first_output_timeout=model_first_output_timeout()
        if first_output_timeout is None
        else first_output_timeout,
        idle_timeout=model_idle_timeout() if idle_timeout is None else idle_timeout,
    )
    request_headers = {
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
        **dict(headers),
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            with client.stream(
                "POST",
                url,
                json=body,
                headers=request_headers,
            ) as response:
                if getattr(response, "is_error", False):
                    response.read()
                response.raise_for_status()
                yield from parse_sse_lines(response.iter_lines())
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"model request failed: {http_error_detail(exc)}") from exc
    except (
        httpx.TimeoutException,
        httpx.NetworkError,
        httpx.ProtocolError,
        httpx.RequestError,
        json.JSONDecodeError,
    ) as exc:
        raise RuntimeError(f"model request failed: {exc}") from exc


def parse_sse_lines(lines: Iterable[str]) -> Iterator[str]:
    """Yield SSE data frames without requiring a Content-Type header."""
    data: list[str] = []
    for line in lines:
        if line == "":
            if data:
                yield "\n".join(data)
                data = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    if data:
        yield "\n".join(data)


def http_error_detail(error: Any) -> str:
    """Return an HTTP failure message including the server's error body."""
    try:
        body = error.response.text[:2048].strip()
    except RuntimeError:
        try:
            error.response.read()
            body = error.response.text[:2048].strip()
        except RuntimeError:
            body = ""
    if not body:
        return str(error)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        detail = body
    else:
        detail = format_stream_error(
            payload.get("error", payload) if isinstance(payload, dict) else payload
        )
    return f"{error}: {detail}"


def decode_stream_event(data: str) -> dict[str, Any] | None:
    """Decode one SSE frame to a JSON object, or None for the [DONE] sentinel."""
    if data == "[DONE]":
        return None
    try:
        event = json.loads(data)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"model stream failed: invalid JSON event: {exc}") from exc
    if not isinstance(event, dict):
        raise RuntimeError("model stream failed: event was not a JSON object")
    return event


def read_streamed_chat_completion(
    events: Iterable[str],
    *,
    stream_sink: ChatCompletionStreamSink | None = None,
) -> dict[str, Any]:
    """Read OpenAI-style chat completion SSE frames into one final response."""
    accumulator = ChatStreamAccumulator(stream_sink=stream_sink)
    done = False
    for data in events:
        chunk = decode_stream_event(data)
        if chunk is None:
            done = True
            break
        error = chunk.get("error")
        if error is not None:
            raise RuntimeError(f"model request failed: {format_stream_error(error)}")
        accumulator.add_chunk(chunk)
    if not done:
        raise RuntimeError("model stream failed: stream ended before [DONE]")
    return accumulator.response()


def format_stream_error(error: Any) -> str:
    """Return a compact model stream error message."""
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str):
            return message
    if isinstance(error, str):
        return error
    return json.dumps(error, sort_keys=True)


class ChatStreamAccumulator:
    """Accumulate OpenAI-style chat completion chunks into a final message."""

    def __init__(
        self,
        *,
        stream_sink: ChatCompletionStreamSink | None = None,
    ) -> None:
        self.metadata: dict[str, Any] = {}
        self.role: str | None = None
        self.content: list[str] = []
        self.reasoning_content: list[str] = []
        self.tool_calls: dict[int, dict[str, Any]] = {}
        self.finish_reason: Any = None
        self.usage: dict[str, int] | None = None
        self.seen_choice = False
        self.stream_sink = stream_sink

    def add_chunk(self, chunk: dict[str, Any]) -> None:
        for key in ("id", "object", "created", "model", "system_fingerprint"):
            value = chunk.get(key)
            if value is not None and key not in self.metadata:
                self.metadata[key] = value
        usage = normalized_usage(chunk.get("usage"))
        if usage is not None:
            self.usage = usage
        choices = chunk.get("choices")
        if choices is None and usage is not None:
            return
        if not isinstance(choices, list):
            raise RuntimeError("model stream failed: event choices were invalid")
        for choice in choices:
            if not isinstance(choice, dict):
                raise RuntimeError("model stream failed: event choice was invalid")
            if choice.get("index", 0) != 0:
                continue
            self.seen_choice = True
            finish_reason = choice.get("finish_reason")
            if finish_reason is not None:
                self.finish_reason = finish_reason
            delta = choice.get("delta", {})
            if not isinstance(delta, dict):
                raise RuntimeError("model stream failed: event delta was invalid")
            self.add_delta(delta)

    def add_delta(self, delta: dict[str, Any]) -> None:
        role = delta.get("role")
        if isinstance(role, str):
            self.role = role
        content = delta.get("content")
        if isinstance(content, str):
            self.content.append(content)
            if self.stream_sink is not None:
                self.stream_sink.content_delta(content)
        reasoning_content = delta.get("reasoning_content")
        if isinstance(reasoning_content, str):
            self.reasoning_content.append(reasoning_content)
            if reasoning_content and self.stream_sink is not None:
                self.stream_sink.reasoning_delta(reasoning_content)
        tool_calls = delta.get("tool_calls")
        if tool_calls is not None:
            self.add_tool_calls(tool_calls)

    def add_tool_calls(self, tool_calls: Any) -> None:
        if not isinstance(tool_calls, list):
            raise RuntimeError("model stream failed: tool call delta was invalid")
        for raw_call in tool_calls:
            if not isinstance(raw_call, dict):
                raise RuntimeError("model stream failed: tool call was invalid")
            index = raw_call.get("index")
            if not isinstance(index, int):
                raise RuntimeError("model stream failed: tool call index was invalid")
            call = self.tool_calls.setdefault(
                index,
                {"function": {"name": "", "arguments": ""}},
            )
            call_id = raw_call.get("id")
            if isinstance(call_id, str):
                call["id"] = call_id
            call_type = raw_call.get("type")
            if isinstance(call_type, str):
                call["type"] = call_type
            function = raw_call.get("function")
            if function is not None:
                self.add_tool_function_delta(call, function)

    def add_tool_function_delta(
        self,
        call: dict[str, Any],
        function: Any,
    ) -> None:
        if not isinstance(function, dict):
            raise RuntimeError("model stream failed: tool function was invalid")
        call_function = call.setdefault("function", {"name": "", "arguments": ""})
        if not isinstance(call_function, dict):
            raise RuntimeError("model stream failed: tool function state was invalid")
        name = function.get("name")
        if isinstance(name, str):
            call_function["name"] = str(call_function.get("name") or "") + name
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            call_function["arguments"] = (
                str(call_function.get("arguments") or "") + arguments
            )

    def response(self) -> dict[str, Any]:
        if not self.seen_choice:
            raise RuntimeError("model stream failed: no completion choices received")
        message: dict[str, Any] = {
            "role": self.role or "assistant",
            "content": "".join(self.content),
        }
        if self.reasoning_content:
            message["reasoning_content"] = "".join(self.reasoning_content)
        if self.tool_calls:
            message["tool_calls"] = [
                self.final_tool_call(index) for index in sorted(self.tool_calls)
            ]
        return {
            **self.metadata,
            **({"usage": self.usage} if self.usage is not None else {}),
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": self.finish_reason,
                }
            ],
        }

    def final_tool_call(self, index: int) -> dict[str, Any]:
        call = self.tool_calls[index]
        function = call.get("function")
        if not isinstance(function, dict):
            function = {"name": "", "arguments": ""}
        return {
            "id": tool_call_id(call, index=index),
            "type": str(call.get("type") or "function"),
            "function": {
                "name": str(function.get("name") or ""),
                "arguments": str(function.get("arguments") or ""),
            },
        }
