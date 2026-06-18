"""Tests for the OpenAI Responses translation layer."""

import json
from typing import Any

import pytest
from _zeta_helpers import FakeStreamingResponse

from zeta import models as zeta_models_api
from zeta.models import codex_auth
from zeta.models import responses as zeta_responses


def sse_frames(events: list[dict[str, Any]], *, done: bool = False) -> list[bytes]:
    frames = []
    for event in events:
        frames.append(f"data: {json.dumps(event)}\n".encode())
        frames.append(b"\n")
    if done:
        frames.append(b"data: [DONE]\n")
        frames.append(b"\n")
    return frames


class DeltaSink:
    def __init__(self) -> None:
        self.deltas: list[str] = []
        self.reasoning_deltas: list[str] = []

    def content_delta(self, text: str) -> None:
        self.deltas.append(text)

    def reasoning_delta(self, text: str) -> None:
        self.reasoning_deltas.append(text)


COMPLETED = {
    "type": "response.completed",
    "response": {
        "id": "resp_1",
        "status": "completed",
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "input_tokens_details": {"cached_tokens": 80},
        },
    },
}


def test_zeta_responses_body_moves_system_prompt_to_instructions() -> None:
    body = zeta_responses.responses_request_body(
        [
            {"role": "system", "content": "Be terse."},
            {"role": "user", "content": "hi"},
        ],
        model="gpt-5.5",
    )

    assert body["instructions"] == "Be terse."
    assert body["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hi"}],
        }
    ]
    assert body["model"] == "gpt-5.5"
    assert body["stream"] is True
    assert body["store"] is False
    assert body["include"] == ["reasoning.encrypted_content"]


def test_zeta_model_input_renders_existing_responses_request() -> None:
    model_input = zeta_models_api.ModelInput(
        messages=[
            {"role": "system", "content": "Be terse."},
            {"role": "user", "content": "hi"},
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "read",
                    "description": "Read a file.",
                    "parameters": {"type": "object"},
                },
            }
        ],
        tool_choice="auto",
        max_tokens=512,
        selected_model="gpt-5.5",
        session_id="session-1",
        thinking="minimal",
    )

    body = zeta_responses.responses_request_from_input(model_input)

    assert body["model"] == "gpt-5.5"
    assert body["stream"] is True
    assert body["store"] is False
    assert body["instructions"] == "Be terse."
    assert body["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hi"}],
        }
    ]
    assert body["tools"] == [
        {
            "type": "function",
            "name": "read",
            "description": "Read a file.",
            "parameters": {"type": "object"},
            "strict": None,
        }
    ]
    assert body["tool_choice"] == "auto"
    assert body["parallel_tool_calls"] is True
    assert body["prompt_cache_key"] == "session-1"
    assert body["reasoning"]["effort"] == "low"
    assert "max_output_tokens" not in body


def test_zeta_responses_body_converts_assistant_and_tool_messages() -> None:
    body = zeta_responses.responses_request_body(
        [
            {"role": "user", "content": "list files"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "ls", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": '{"ok":true}'},
            {"role": "assistant", "content": "Two files."},
        ],
        model="gpt-5.5",
    )

    assert body["input"][1] == {
        "type": "function_call",
        "call_id": "call_1",
        "name": "ls",
        "arguments": "{}",
    }
    assert body["input"][2] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": '{"ok":true}',
    }
    assert body["input"][3] == {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "Two files."}],
    }


def test_zeta_responses_body_replays_recorded_items_verbatim() -> None:
    recorded = [
        {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"},
        {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "ls"},
    ]
    body = zeta_responses.responses_request_body(
        [
            {"role": "user", "content": "list files"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "ls", "arguments": "{}"},
                    }
                ],
                "_responses_items": recorded,
            },
        ],
        model="gpt-5.5",
    )

    assert body["input"][1:] == recorded


def test_zeta_responses_body_converts_tools_with_null_strict() -> None:
    body = zeta_responses.responses_request_body(
        [{"role": "user", "content": "hi"}],
        model="gpt-5.5",
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "read",
                    "description": "Read a file.",
                    "parameters": {"type": "object"},
                },
            }
        ],
    )

    assert body["tools"] == [
        {
            "type": "function",
            "name": "read",
            "description": "Read a file.",
            "parameters": {"type": "object"},
            "strict": None,
        }
    ]
    assert body["tool_choice"] == "auto"
    assert body["parallel_tool_calls"] is True


def test_zeta_responses_body_maps_thinking_to_reasoning_effort() -> None:
    def effort(thinking: str | None) -> str:
        body = zeta_responses.responses_request_body(
            [{"role": "user", "content": "hi"}],
            model="gpt-5.5",
            thinking=thinking,
        )
        return body["reasoning"]["effort"]

    assert effort(None) == "medium"
    assert effort("none") == "low"
    assert effort("minimal") == "low"
    assert effort("low") == "low"
    assert effort("medium") == "medium"
    assert effort("high") == "high"


def test_zeta_responses_body_carries_session_cache_key_without_max_tokens() -> None:
    body = zeta_responses.responses_request_body(
        [{"role": "user", "content": "hi"}],
        model="gpt-5.5",
        max_tokens=512,
        session_id="session-1",
    )

    assert body["prompt_cache_key"] == "session-1"
    assert "max_output_tokens" not in body
    assert body["reasoning"]["summary"] == "auto"


def test_zeta_responses_stream_accumulates_text_and_reasoning() -> None:
    sink = DeltaSink()
    events = [
        {"type": "response.created", "response": {"id": "resp_1"}},
        {"type": "response.reasoning_summary_text.delta", "delta": "weighing"},
        {"type": "response.reasoning_summary_part.done"},
        {"type": "response.reasoning_summary_text.delta", "delta": "deciding"},
        {
            "type": "response.output_item.done",
            "item": {
                "type": "reasoning",
                "id": "rs_1",
                "encrypted_content": "opaque",
            },
        },
        {"type": "response.output_text.delta", "delta": "Hello"},
        {"type": "response.output_text.delta", "delta": " world"},
        {
            "type": "response.output_item.done",
            "item": {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Hello world"}],
            },
        },
        COMPLETED,
    ]

    payload = zeta_responses.read_streamed_responses(
        iter(sse_frames(events)), stream_sink=sink
    )

    message = payload["choices"][0]["message"]
    assert message["role"] == "assistant"
    assert message["content"] == "Hello world"
    assert message["reasoning_content"] == "weighing\n\ndeciding"
    assert message["_responses_items"][0]["encrypted_content"] == "opaque"
    assert sink.deltas == ["Hello", " world"]
    assert sink.reasoning_deltas == ["weighing", "\n\n", "deciding"]
    assert payload["choices"][0]["finish_reason"] == "stop"
    assert payload["usage"] == {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
    }


def test_zeta_model_output_from_responses_payload_preserves_replay_items() -> None:
    payload = zeta_responses.read_streamed_responses(
        iter(
            sse_frames(
                [
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "reasoning",
                            "id": "rs_1",
                            "encrypted_content": "opaque",
                        },
                    },
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "message",
                            "id": "msg_1",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "done"}],
                        },
                    },
                    COMPLETED,
                ]
            )
        )
    )

    output = zeta_responses.model_output_from_responses_payload(payload)

    assert output.message["content"] == "done"
    assert output.finish_reason == "stop"
    assert output.usage == zeta_models_api.ModelUsage(
        prompt_tokens=100,
        completion_tokens=20,
        total_tokens=120,
    )
    assert output.provider_replay_items == (
        {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"},
        {
            "type": "message",
            "id": "msg_1",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "done"}],
        },
    )


def test_zeta_responses_codex_completion_returns_adapter_message(monkeypatch) -> None:
    payload = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "raw"},
                "finish_reason": "stop",
            }
        ]
    }
    converted: list[dict[str, Any]] = []

    def fake_request(body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        del body
        del kwargs
        return payload

    def fake_model_output(
        raw_payload: dict[str, Any],
    ) -> zeta_models_api.ModelOutput:
        converted.append(raw_payload)
        return zeta_models_api.ModelOutput(
            message={"role": "assistant", "content": "converted"},
            finish_reason="stop",
        )

    monkeypatch.setattr(zeta_responses, "request_codex_response", fake_request)
    monkeypatch.setattr(
        zeta_responses,
        "model_output_from_responses_payload",
        fake_model_output,
    )

    message = zeta_responses.codex_completion_messages(
        [{"role": "user", "content": "hi"}],
        selected_model="gpt-5.5",
    )

    assert message == {"role": "assistant", "content": "converted"}
    assert converted == [payload]


def test_zeta_responses_stream_collects_tool_calls() -> None:
    events = [
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "read",
                "arguments": '{"path":"notes.md"}',
            },
        },
        COMPLETED,
    ]

    payload = zeta_responses.read_streamed_responses(iter(sse_frames(events)))

    message = payload["choices"][0]["message"]
    assert message["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "read", "arguments": '{"path":"notes.md"}'},
        }
    ]
    assert payload["choices"][0]["finish_reason"] == "tool_calls"
    assert message["_responses_items"][0]["id"] == "fc_1"


def test_zeta_responses_stream_marks_incomplete_as_length() -> None:
    events = [
        {
            "type": "response.incomplete",
            "response": {"id": "resp_1", "status": "incomplete"},
        },
    ]

    payload = zeta_responses.read_streamed_responses(iter(sse_frames(events)))

    assert payload["choices"][0]["finish_reason"] == "length"


def test_zeta_responses_stream_raises_on_error_event() -> None:
    events = [{"type": "error", "code": "server_error", "message": "boom"}]

    with pytest.raises(RuntimeError, match="boom"):
        zeta_responses.read_streamed_responses(iter(sse_frames(events)))


def test_zeta_responses_stream_renders_quota_errors_with_reset_hint() -> None:
    events = [
        {
            "type": "response.failed",
            "response": {
                "id": "resp_1",
                "status": "failed",
                "error": {
                    "code": "usage_limit_reached",
                    "message": "limit",
                    "plan_type": "pro",
                    "resets_at": 1767225600,
                },
            },
        }
    ]

    with pytest.raises(RuntimeError, match="usage limit"):
        zeta_responses.read_streamed_responses(iter(sse_frames(events)))


def test_zeta_responses_stream_requires_terminal_event() -> None:
    events = [{"type": "response.output_text.delta", "delta": "Hello"}]

    with pytest.raises(RuntimeError, match="ended before"):
        zeta_responses.read_streamed_responses(iter(sse_frames(events)))


def test_zeta_responses_codex_url_appends_endpoint() -> None:
    assert (
        zeta_responses.codex_responses_url("https://chatgpt.com/backend-api")
        == "https://chatgpt.com/backend-api/codex/responses"
    )
    assert (
        zeta_responses.codex_responses_url("https://chatgpt.com/backend-api/")
        == "https://chatgpt.com/backend-api/codex/responses"
    )
    assert zeta_responses.codex_responses_url(None) == (
        "https://chatgpt.com/backend-api/codex/responses"
    )


def test_zeta_responses_codex_headers_carry_identity() -> None:
    credentials = codex_auth.CodexCredentials(access_token="tok-1", account_id="acct_1")

    headers = zeta_responses.codex_request_headers(credentials, "session-1")

    assert headers["Authorization"] == "Bearer tok-1"
    assert headers["chatgpt-account-id"] == "acct_1"
    assert headers["originator"] == "zeta"
    assert headers["OpenAI-Beta"] == "responses=experimental"
    assert headers["session-id"] == "session-1"
    assert headers["Accept"] == "text/event-stream"


def test_zeta_responses_codex_completion_round_trip(monkeypatch) -> None:
    events = [
        {
            "type": "response.output_item.done",
            "item": {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Hello world"}],
            },
        },
        COMPLETED,
    ]
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float | None = None) -> Any:
        captured["url"] = request.full_url
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return FakeStreamingResponse(sse_frames(events))

    monkeypatch.setattr(zeta_responses.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        zeta_responses,
        "load_codex_credentials",
        lambda: codex_auth.CodexCredentials(access_token="tok-1", account_id="acct_1"),
    )
    monkeypatch.setenv("ZETA_SESSION_ID", "session-1")
    telemetry: dict[str, Any] = {}

    message = zeta_responses.codex_completion_messages(
        [
            {"role": "system", "content": "Be terse."},
            {"role": "user", "content": "hi"},
        ],
        selected_model="gpt-5.5",
        telemetry_sink=telemetry.update,
        thinking="high",
    )

    assert message["content"] == "Hello world"
    assert captured["url"] == "https://chatgpt.com/backend-api/codex/responses"
    assert captured["headers"]["Authorization"] == "Bearer tok-1"
    assert captured["headers"]["Originator"] == "zeta"
    assert captured["body"]["model"] == "gpt-5.5"
    assert captured["body"]["store"] is False
    assert captured["body"]["prompt_cache_key"] == "session-1"
    assert captured["body"]["instructions"] == "Be terse."
    assert captured["body"]["reasoning"]["effort"] == "high"
    assert telemetry["usage"]["prompt_tokens"] == 100
    assert telemetry["model_context_tokens"] == 272000


def test_zeta_responses_codex_requires_a_model_name() -> None:
    with pytest.raises(RuntimeError, match="model"):
        zeta_responses.codex_completion_messages(
            [{"role": "user", "content": "hi"}],
        )


def test_zeta_responses_codex_guards_truncated_tool_calls(monkeypatch) -> None:
    events = [
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "read",
                "arguments": '{"path":',
            },
        },
        {
            "type": "response.incomplete",
            "response": {"id": "resp_1", "status": "incomplete"},
        },
    ]

    monkeypatch.setattr(
        zeta_responses.urllib.request,
        "urlopen",
        lambda request, timeout=None: FakeStreamingResponse(sse_frames(events)),
    )
    monkeypatch.setattr(
        zeta_responses,
        "load_codex_credentials",
        lambda: codex_auth.CodexCredentials(access_token="tok-1", account_id="acct_1"),
    )

    with pytest.raises(RuntimeError, match="max_tokens"):
        zeta_responses.codex_completion_messages(
            [{"role": "user", "content": "hi"}],
            selected_model="gpt-5.5",
        )


def test_zeta_responses_codex_structured_output(monkeypatch) -> None:
    events = [
        {
            "type": "response.output_item.done",
            "item": {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "content": [{"type": "output_text", "text": '{"summary":"done"}'}],
            },
        },
        COMPLETED,
    ]
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float | None = None) -> Any:
        captured["body"] = json.loads(request.data)
        return FakeStreamingResponse(sse_frames(events))

    monkeypatch.setattr(zeta_responses.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        zeta_responses,
        "load_codex_credentials",
        lambda: codex_auth.CodexCredentials(access_token="tok-1", account_id="acct_1"),
    )
    schema = {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
    }

    data = zeta_responses.codex_structured_output(
        [{"role": "user", "content": "summarize"}],
        schema=schema,
        response_name="task_state",
        selected_model="gpt-5.5",
    )

    assert data == {"summary": "done"}
    assert captured["body"]["text"]["format"] == {
        "type": "json_schema",
        "name": "task_state",
        "strict": True,
        "schema": schema,
    }
