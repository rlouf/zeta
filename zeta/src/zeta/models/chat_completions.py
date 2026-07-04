"""OpenAI-compatible chat completions transport for Zeta."""

import http.client
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any, Protocol
from urllib.parse import urlparse, urlunparse

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from zeta.models.profiles import model_name, model_url
from zeta.models.types import ModelInput, ModelOutput, ModelUsage

MUTED = "\033[38;2;110;106;134m"
LOVE = "\033[38;2;235;111;146m"
RESET = "\033[0m"
DEFAULT_MODEL_IDLE_TIMEOUT_SECONDS = 120.0
DEFAULT_MODEL_FIRST_OUTPUT_TIMEOUT_SECONDS = 600.0
MODEL_METADATA_TIMEOUT_SECONDS = 0.5
DEFAULT_MAX_COMPLETION_TOKENS = 8192

USAGE_TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")
ModelTelemetrySink = Callable[[dict[str, Any]], None]
_MODEL_CONTEXT_TOKENS_CACHE: dict[tuple[str, str], int] = {}


def tool_call_id(tool_call: dict[str, Any], *, index: int) -> str:
    return str(tool_call.get("id") or f"call-{index}")


def should_color(stream: object) -> bool:
    return (
        bool(getattr(stream, "isatty", lambda: False)())
        and "NO_COLOR" not in os.environ
    )


def muted(text: str, *, enabled: bool) -> str:
    if not enabled:
        return text
    return f"{MUTED}{text}{RESET}"


class ChatCompletionStreamSink(Protocol):
    """Receive visible chat completion stream events."""

    def content_delta(self, text: str) -> None:
        """Handle one visible assistant text delta."""
        ...

    def reasoning_delta(self, text: str) -> None:
        """Handle one model reasoning text delta."""
        ...


def stream_timeout_from_env(
    env: Mapping[str, str],
    name: str,
    default: float,
) -> float | None:
    """Parse a stream timeout variable; non-positive values disable it."""
    value = env.get(name)
    if value is None or value.strip() == "":
        return default
    try:
        seconds = float(value)
    except ValueError:
        return default
    if seconds <= 0:
        return None
    return seconds


def model_idle_timeout_from_env(env: Mapping[str, str]) -> float | None:
    """Return the configured client-side model stream idle timeout."""
    return stream_timeout_from_env(
        env,
        "ZETA_MODEL_IDLE_TIMEOUT_SECONDS",
        DEFAULT_MODEL_IDLE_TIMEOUT_SECONDS,
    )


def model_idle_timeout() -> float | None:
    """Return the configured client-side model stream idle timeout."""
    return model_idle_timeout_from_env(os.environ)


def model_first_output_timeout_from_env(env: Mapping[str, str]) -> float | None:
    """Return the configured limit on connect plus time to first chunk."""
    return stream_timeout_from_env(
        env,
        "ZETA_MODEL_FIRST_OUTPUT_TIMEOUT_SECONDS",
        DEFAULT_MODEL_FIRST_OUTPUT_TIMEOUT_SECONDS,
    )


def model_first_output_timeout() -> float | None:
    """Return the configured limit on connect plus time to first chunk."""
    return model_first_output_timeout_from_env(os.environ)


def model_endpoint_valid(url: str) -> bool:
    """Return whether a model endpoint URL includes a host."""
    return urlparse(url).hostname is not None


def endpoint_reachable(url: str) -> bool:
    """Return whether the configured endpoint accepts TCP connections."""
    parsed = urlparse(url)
    host = parsed.hostname
    if host is None:
        return False
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False


def model_endpoint_open(selected_url: str | None = None) -> bool:
    """Return whether the configured OpenAI-compatible server is listening."""
    return endpoint_reachable(model_url(selected_url))


def model_server_root(selected_url: str | None = None) -> str:
    """Return the endpoint root for sibling metadata endpoints."""
    parsed = urlparse(model_url(selected_url))
    path = parsed.path.rstrip("/")
    for suffix in ("/v1/chat/completions", "/chat/completions"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    else:
        path = ""
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def model_context_tokens(
    selected_url: str | None = None,
    selected_model: str | None = None,
) -> int | None:
    """Return the configured model context length when the server exposes it."""
    resolved_url = model_url(selected_url)
    resolved_model = model_name(selected_model)
    cache_key = (resolved_url, resolved_model)
    cached = _MODEL_CONTEXT_TOKENS_CACHE.get(cache_key)
    if cached is not None:
        return cached
    for endpoint in ("/props", "/v1/models"):
        payload = request_model_metadata(endpoint, selected_url=selected_url)
        if not isinstance(payload, dict):
            continue
        tokens = context_tokens_from_metadata(payload, selected_model=resolved_model)
        if tokens is not None:
            _MODEL_CONTEXT_TOKENS_CACHE[cache_key] = tokens
            return tokens
    return None


def request_model_metadata(
    path: str,
    *,
    selected_url: str | None = None,
) -> dict[str, Any] | None:
    """Fetch a best-effort JSON document from a model metadata endpoint."""
    url = model_server_root(selected_url).rstrip("/") + "/" + path.lstrip("/")
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(
            req, timeout=MODEL_METADATA_TIMEOUT_SECONDS
        ) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (
        OSError,
        TimeoutError,
        http.client.HTTPException,
        urllib.error.URLError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ):
        return None
    if isinstance(payload, dict):
        return payload
    return None


def context_tokens_from_metadata(
    payload: dict[str, Any],
    *,
    selected_model: str | None = None,
) -> int | None:
    """Extract a context length from llama-server style metadata."""
    props_tokens = context_tokens_from_props(payload)
    if props_tokens is not None:
        return props_tokens
    return context_tokens_from_models(payload, selected_model=selected_model)


def context_tokens_from_props(payload: dict[str, Any]) -> int | None:
    settings = payload.get("default_generation_settings")
    if isinstance(settings, dict):
        tokens = positive_int(settings.get("n_ctx"))
        if tokens is not None:
            return tokens
        params = settings.get("params")
        if isinstance(params, dict):
            tokens = positive_int(params.get("n_ctx"))
            if tokens is not None:
                return tokens
    return positive_int(payload.get("n_ctx"))


def context_tokens_from_models(
    payload: dict[str, Any],
    *,
    selected_model: str | None = None,
) -> int | None:
    models = candidate_models(payload)
    if not models:
        return None
    for model in models:
        if selected_model and not model_matches_name(model, selected_model):
            continue
        tokens = context_tokens_from_model_entry(model)
        if tokens is not None:
            return tokens
    return context_tokens_from_model_entry(models[0])


def candidate_models(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("data", "models"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def model_matches_name(model: dict[str, Any], selected_model: str) -> bool:
    names = [
        value
        for value in (model.get("id"), model.get("name"), model.get("model"))
        if isinstance(value, str)
    ]
    aliases = model.get("aliases")
    if isinstance(aliases, list):
        names.extend(alias for alias in aliases if isinstance(alias, str))
    return selected_model in names


def context_tokens_from_model_entry(model: dict[str, Any]) -> int | None:
    for key in ("meta", "details"):
        value = model.get(key)
        if isinstance(value, dict):
            tokens = positive_int(value.get("n_ctx"))
            if tokens is not None:
                return tokens
    tokens = positive_int(model.get("context_length"))
    if tokens is not None:
        return tokens
    top_provider = model.get("top_provider")
    if isinstance(top_provider, dict):
        tokens = positive_int(top_provider.get("context_length"))
        if tokens is not None:
            return tokens
    return positive_int(model.get("n_ctx"))


def positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value <= 0:
        return None
    return value


def request_chat_completion(
    body: dict[str, Any],
    *,
    selected_url: str | None = None,
    stream_sink: ChatCompletionStreamSink | None = None,
) -> dict[str, Any]:
    """POST one streaming chat completions request and return the final response."""
    stream_body = {**body, "stream": True}
    payload = read_streamed_chat_completion(
        stream_json_sse(
            model_url(selected_url),
            stream_body,
            headers={"Accept": "text/event-stream"},
        ),
        stream_sink=stream_sink,
    )
    if not isinstance(payload, dict):
        raise RuntimeError("model request failed: response was not a JSON object")
    return payload


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


def model_stream_timeout(
    *,
    first_output_timeout: float | None,
    idle_timeout: float | None,
) -> Any:
    """Map model timeout intent onto httpx's explicit timeout fields."""
    import httpx

    return httpx.Timeout(
        timeout=None,
        connect=first_output_timeout,
        write=first_output_timeout,
        pool=first_output_timeout,
        read=idle_timeout,
    )


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


def normalized_usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    usage = {
        key: token_count
        for key in USAGE_TOKEN_FIELDS
        if isinstance((token_count := value.get(key)), int)
        and not isinstance(token_count, bool)
    }
    return usage or None


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


def chat_completion_messages(
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
    payload = request_chat_completion(
        body, selected_url=selected_url, stream_sink=stream_sink
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


def chat_structured_output(
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
    payload = request_chat_completion(body, selected_url=selected_url)
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


def local_model_path() -> str:
    """Return the optional local model path shown in startup help text."""
    return os.environ.get("ZETA_MODEL_PATH") or "<path-to-model.gguf>"


def ensure_server(
    *,
    selected_url: str | None = None,
    selected_model: str | None = None,
) -> bool:
    """Check that the configured OpenAI-compatible endpoint is reachable."""
    url = model_url(selected_url)
    if model_endpoint_open(selected_url):
        return True
    color = should_color(sys.stderr)
    error_line = f"✗ model: no OpenAI-compatible endpoint reachable at {url}"
    if color:
        error_line = f"{LOVE}{error_line}{RESET}"
    hint_lines = [
        "  Start a local OpenAI-compatible server:",
        "      llama-server \\",
        f"        -m {local_model_path()} \\",
        f"        --alias {model_name(selected_model)} --host 127.0.0.1 --port 8080 \\",
        "        -ngl 99 -c 262144 -fa on --reasoning auto",
    ]
    print("", file=sys.stderr)
    print(error_line, file=sys.stderr)
    print("", file=sys.stderr)
    for hint_line in hint_lines:
        print(muted(hint_line, enabled=color), file=sys.stderr)
    print("", file=sys.stderr)
    return False
