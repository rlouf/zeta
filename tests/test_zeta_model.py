"""Model request, streaming, and model-selection tests."""

from __future__ import annotations

import email.message
import io
import json
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from _zeta_helpers import (
    DeltaSink,
    FakeStreamingResponse,
    sse_lines,
    task_state_fixture,
    write_models_config,
)
from click.testing import CliRunner

from sigil.cli import cli as sigil_cli
from sigil.zeta import model as zeta_model
from sigil.zeta import models as zeta_models
from sigil.zeta import prompt as zeta_prompt


def test_zeta_model_config_uses_zeta_env(monkeypatch) -> None:
    monkeypatch.delenv("ZETA_MODEL_URL", raising=False)
    monkeypatch.delenv("ZETA_MODEL_NAME", raising=False)
    monkeypatch.delenv("ZETA_MODEL_IDLE_TIMEOUT_SECONDS", raising=False)

    assert zeta_model.model_url() == zeta_model.DEFAULT_MODEL_URL
    assert zeta_model.model_name() == zeta_model.DEFAULT_MODEL_NAME
    assert (
        zeta_model.model_idle_timeout() == zeta_model.DEFAULT_MODEL_IDLE_TIMEOUT_SECONDS
    )
    assert zeta_model.DEFAULT_MODEL_IDLE_TIMEOUT_SECONDS == 120.0

    monkeypatch.setenv("ZETA_MODEL_URL", "http://zeta.invalid/v1/chat/completions")
    monkeypatch.setenv("ZETA_MODEL_NAME", "zeta-model")
    monkeypatch.setenv("ZETA_MODEL_IDLE_TIMEOUT_SECONDS", "2.5")

    assert zeta_model.model_url() == "http://zeta.invalid/v1/chat/completions"
    assert zeta_model.model_name() == "zeta-model"
    assert zeta_model.model_idle_timeout() == 2.5

    monkeypatch.setenv("ZETA_MODEL_IDLE_TIMEOUT_SECONDS", "0")
    assert zeta_model.model_idle_timeout() is None


def test_zeta_model_first_output_timeout_uses_zeta_env(monkeypatch) -> None:
    monkeypatch.delenv("ZETA_MODEL_FIRST_OUTPUT_TIMEOUT_SECONDS", raising=False)

    assert (
        zeta_model.model_first_output_timeout()
        == zeta_model.DEFAULT_MODEL_FIRST_OUTPUT_TIMEOUT_SECONDS
    )
    assert zeta_model.DEFAULT_MODEL_FIRST_OUTPUT_TIMEOUT_SECONDS == 600.0

    monkeypatch.setenv("ZETA_MODEL_FIRST_OUTPUT_TIMEOUT_SECONDS", "45")
    assert zeta_model.model_first_output_timeout() == 45.0

    monkeypatch.setenv("ZETA_MODEL_FIRST_OUTPUT_TIMEOUT_SECONDS", "0")
    assert zeta_model.model_first_output_timeout() is None


def test_zeta_request_chat_completion_streams_final_message(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    response = FakeStreamingResponse(
        sse_lines(
            {
                "id": "chatcmpl-test",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "hel"},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "lo"},
                        "finish_reason": "stop",
                    }
                ],
            },
            "[DONE]",
        )
    )

    def fake_urlopen(req: Any, timeout: float | None = None) -> FakeStreamingResponse:
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["accept"] = req.get_header("Accept")
        captured["timeout"] = timeout
        return response

    monkeypatch.delenv("ZETA_MODEL_FIRST_OUTPUT_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(zeta_model.urllib.request, "urlopen", fake_urlopen)
    body = {"model": "local-model", "messages": []}

    payload = zeta_model.request_chat_completion(body)

    assert body == {"model": "local-model", "messages": []}
    assert captured["body"]["stream"] is True
    assert captured["accept"] == "text/event-stream"
    assert captured["timeout"] == zeta_model.DEFAULT_MODEL_FIRST_OUTPUT_TIMEOUT_SECONDS
    assert response.closed is True
    assert payload["id"] == "chatcmpl-test"
    assert payload["choices"][0]["message"] == {
        "role": "assistant",
        "content": "hello",
    }
    assert payload["choices"][0]["finish_reason"] == "stop"


def test_zeta_stream_replaces_invalid_utf8_bytes() -> None:
    chunk = (
        b'data: {"choices":[{"index":0,"delta":{"content":"caf\xff"},'
        b'"finish_reason":"stop"}]}\n'
    )
    lines = [chunk, b"\n", b"data: [DONE]\n"]

    payload = zeta_model.read_streamed_chat_completion(iter(lines))

    assert payload["choices"][0]["message"]["content"] == "caf�"


def test_zeta_stream_reassembles_chunks_split_mid_character() -> None:
    frame = (
        'data: {"choices":[{"index":0,"delta":{"content":"café"},'
        '"finish_reason":"stop"}]}\n'
    ).encode()
    split = frame.index(b"\xc3") + 1
    lines = [frame[:split], frame[split:], b"\n", b"data: [DONE]\n"]

    payload = zeta_model.read_streamed_chat_completion(iter(lines))

    assert payload["choices"][0]["message"]["content"] == "café"


def test_zeta_stream_emits_content_deltas_in_order() -> None:
    sink = DeltaSink()

    payload = zeta_model.read_streamed_chat_completion(
        sse_lines(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "hel"},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "lo"},
                        "finish_reason": "stop",
                    }
                ],
            },
            "[DONE]",
        ),
        stream_sink=sink,
    )

    assert sink.deltas == ["hel", "lo"]
    assert payload["choices"][0]["message"]["content"] == "hello"


def test_zeta_stream_preserves_usage_chunk() -> None:
    payload = zeta_model.read_streamed_chat_completion(
        sse_lines(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "hello"},
                        "finish_reason": "stop",
                    }
                ],
            },
            {
                "usage": {
                    "prompt_tokens": 123,
                    "completion_tokens": 4,
                    "total_tokens": 127,
                },
            },
            "[DONE]",
        )
    )

    assert payload["usage"] == {
        "prompt_tokens": 123,
        "completion_tokens": 4,
        "total_tokens": 127,
    }


def test_zeta_stream_sink_does_not_change_reconstructed_message() -> None:
    frames = sse_lines(
        {
            "id": "chatcmpl-test",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": "done"},
                    "finish_reason": "stop",
                }
            ],
        },
        "[DONE]",
    )
    sink = DeltaSink()

    without_sink = zeta_model.read_streamed_chat_completion(frames)
    with_sink = zeta_model.read_streamed_chat_completion(frames, stream_sink=sink)

    assert with_sink == without_sink
    assert sink.deltas == ["done"]


def test_zeta_stream_does_not_render_tool_call_fragments() -> None:
    sink = DeltaSink()

    payload = zeta_model.read_streamed_chat_completion(
        sse_lines(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-read",
                                    "type": "function",
                                    "function": {
                                        "name": "read",
                                        "arguments": '{"path"',
                                    },
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": ': "README.md"}'},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            },
            "[DONE]",
        ),
        stream_sink=sink,
    )

    assert sink.deltas == []
    assert payload["choices"][0]["message"]["tool_calls"][0]["function"] == {
        "name": "read",
        "arguments": '{"path": "README.md"}',
    }


def test_zeta_stream_mixed_content_and_tool_call_exposes_completed_call() -> None:
    sink = DeltaSink()

    payload = zeta_model.read_streamed_chat_completion(
        sse_lines(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "content": "I'll inspect README.",
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-read",
                                    "type": "function",
                                    "function": {
                                        "name": "read",
                                        "arguments": '{"path":"README.md"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            },
            "[DONE]",
        ),
        stream_sink=sink,
    )

    message = payload["choices"][0]["message"]
    assert sink.deltas == ["I'll inspect README."]
    assert message["content"] == "I'll inspect README."
    assert message["tool_calls"][0]["function"]["name"] == "read"


def test_zeta_stream_reconstructs_split_tool_calls() -> None:
    payload = zeta_model.read_streamed_chat_completion(
        sse_lines(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-read",
                                    "type": "function",
                                    "function": {
                                        "name": "read",
                                        "arguments": '{"path"',
                                    },
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": ': "README.md"}'},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            },
            "[DONE]",
        )
    )

    message = payload["choices"][0]["message"]
    assert message["tool_calls"] == [
        {
            "id": "call-read",
            "type": "function",
            "function": {
                "name": "read",
                "arguments": '{"path": "README.md"}',
            },
        }
    ]
    assert payload["choices"][0]["finish_reason"] == "tool_calls"


def test_zeta_stream_orders_multiple_tool_calls_by_index() -> None:
    payload = zeta_model.read_streamed_chat_completion(
        sse_lines(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 1,
                                    "id": "call-ls",
                                    "type": "function",
                                    "function": {
                                        "name": "ls",
                                        "arguments": '{"path":"."}',
                                    },
                                },
                                {
                                    "index": 0,
                                    "id": "call-read",
                                    "type": "function",
                                    "function": {
                                        "name": "read",
                                        "arguments": '{"path":"README.md"}',
                                    },
                                },
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            },
            "[DONE]",
        )
    )

    calls = payload["choices"][0]["message"]["tool_calls"]
    assert [call["id"] for call in calls] == ["call-read", "call-ls"]


def test_zeta_request_chat_completion_closes_stream_on_error(monkeypatch) -> None:
    response = FakeStreamingResponse(
        sse_lines({"error": {"message": "generation failed"}})
    )

    def fake_urlopen(req: Any, timeout: float | None = None) -> FakeStreamingResponse:
        return response

    monkeypatch.setattr(zeta_model.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError, match="generation failed"):
        zeta_model.request_chat_completion({"model": "local-model", "messages": []})

    assert response.closed is True


def test_zeta_stream_rejects_malformed_events() -> None:
    with pytest.raises(RuntimeError, match="invalid JSON event"):
        zeta_model.read_streamed_chat_completion([b"data: nope\n", b"\n"])


def test_zeta_stream_tightens_socket_timeout_after_first_chunk(monkeypatch) -> None:
    class SocketSpy:
        def __init__(self) -> None:
            self.timeouts: list[float | None] = []

        def settimeout(self, timeout: float | None) -> None:
            self.timeouts.append(timeout)

    sock = SocketSpy()
    response = FakeStreamingResponse(
        sse_lines(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "hi"},
                        "finish_reason": "stop",
                    }
                ],
            },
            "[DONE]",
        ),
        fp=SimpleNamespace(raw=SimpleNamespace(_sock=sock)),
    )

    def fake_urlopen(req: Any, timeout: float | None = None) -> FakeStreamingResponse:
        return response

    monkeypatch.delenv("ZETA_MODEL_IDLE_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(zeta_model.urllib.request, "urlopen", fake_urlopen)

    zeta_model.request_chat_completion({"model": "local-model", "messages": []})

    assert sock.timeouts == [zeta_model.DEFAULT_MODEL_IDLE_TIMEOUT_SECONDS]


def test_zeta_request_chat_completion_surfaces_http_error_body(monkeypatch) -> None:
    body = json.dumps(
        {"error": {"code": 500, "message": "Failed to parse tool call arguments"}}
    ).encode("utf-8")

    def fake_urlopen(req: Any, timeout: float | None = None) -> FakeStreamingResponse:
        raise urllib.error.HTTPError(
            "http://127.0.0.1:8080/v1/chat/completions",
            500,
            "Internal Server Error",
            email.message.Message(),
            io.BytesIO(body),
        )

    monkeypatch.setattr(zeta_model.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError) as excinfo:
        zeta_model.request_chat_completion({"model": "local-model", "messages": []})

    message = str(excinfo.value)
    assert "500" in message
    assert "Failed to parse tool call arguments" in message


def test_zeta_request_chat_completion_surfaces_plain_http_error_body(
    monkeypatch,
) -> None:
    def fake_urlopen(req: Any, timeout: float | None = None) -> FakeStreamingResponse:
        raise urllib.error.HTTPError(
            "http://127.0.0.1:8080/v1/chat/completions",
            502,
            "Bad Gateway",
            email.message.Message(),
            io.BytesIO(b"upstream exploded"),
        )

    monkeypatch.setattr(zeta_model.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError, match="upstream exploded"):
        zeta_model.request_chat_completion({"model": "local-model", "messages": []})


def test_zeta_model_profiles_load_user_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    write_models_config(
        home,
        """
[[models]]
name = "fast"
model = "fast-model"
url = "http://127.0.0.1:8081/v1/chat/completions"

[[models]]
name = "default-url"
model = "default-url-model"
""",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ZETA_MODEL_URL", "http://env.invalid/v1/chat/completions")

    catalog = zeta_models.load_model_profiles()
    fast = zeta_models.resolve_model_profile("fast", catalog=catalog)
    default_url = zeta_models.resolve_model_profile("default-url", catalog=catalog)

    assert catalog.diagnostics == []
    assert fast == zeta_models.ModelSelection(
        profile="fast",
        model="fast-model",
        url="http://127.0.0.1:8081/v1/chat/completions",
    )
    assert default_url == zeta_models.ModelSelection(
        profile="default-url",
        model="default-url-model",
        url="http://env.invalid/v1/chat/completions",
    )


def test_zeta_model_profiles_report_invalid_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    write_models_config(
        home,
        """
[[models]]
name = "Bad_Name"
model = "bad"
""",
    )
    monkeypatch.setenv("HOME", str(home))

    catalog = zeta_models.load_model_profiles()

    assert catalog.profiles == {}
    assert len(catalog.diagnostics) == 1
    assert "lowercase letters" in catalog.diagnostics[0].message


def test_sigil_model_cli_switches_model_per_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    write_models_config(
        home,
        """
[[models]]
name = "fast"
model = "fast-model"
url = "http://127.0.0.1:8081/v1/chat/completions"
""",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SIGIL_SESSION_ID", "one")

    use = CliRunner().invoke(sigil_cli, ["model", "use", "fast"])

    assert use.exit_code == 0, use.output
    assert "model: fast -> fast-model" in use.output
    assert zeta_models.active_model_profile() == "fast"

    show = CliRunner().invoke(sigil_cli, ["model", "show"])
    assert show.exit_code == 0, show.output
    assert "model: fast -> fast-model" in show.output

    monkeypatch.setenv("SIGIL_SESSION_ID", "two")
    other_session = CliRunner().invoke(sigil_cli, ["model", "show"])
    assert other_session.exit_code == 0, other_session.output
    assert "model: default ->" in other_session.output

    monkeypatch.setenv("SIGIL_SESSION_ID", "one")
    clear = CliRunner().invoke(sigil_cli, ["model", "clear"])
    assert clear.exit_code == 0, clear.output
    assert zeta_models.active_model_profile() is None


def test_sigil_model_cli_rejects_unknown_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    write_models_config(home, "")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SIGIL_SESSION_ID", "model-test")

    result = CliRunner().invoke(sigil_cli, ["model", "use", "missing"])

    assert result.exit_code != 0
    assert "unknown model profile: missing" in result.output
    assert zeta_models.active_model_profile() is None


def test_zeta_model_context_tokens_prefers_props(monkeypatch) -> None:
    zeta_model._MODEL_CONTEXT_TOKENS_CACHE.clear()
    calls: list[str] = []

    def fake_metadata(
        path: str,
        *,
        selected_url: str | None = None,
    ) -> dict[str, Any] | None:
        del selected_url
        calls.append(path)
        return {"default_generation_settings": {"n_ctx": 262_144}}

    monkeypatch.setattr(zeta_model, "request_model_metadata", fake_metadata)

    tokens = zeta_model.model_context_tokens(
        "http://127.0.0.1:8080/v1/chat/completions",
        "local-model",
    )

    assert tokens == 262_144
    assert calls == ["/props"]


def test_zeta_model_context_tokens_falls_back_to_selected_model(
    monkeypatch,
) -> None:
    zeta_model._MODEL_CONTEXT_TOKENS_CACHE.clear()

    def fake_metadata(
        path: str,
        *,
        selected_url: str | None = None,
    ) -> dict[str, Any] | None:
        del selected_url
        if path == "/props":
            return {}
        return {
            "data": [
                {"id": "other-model", "meta": {"n_ctx": 8_192}},
                {
                    "id": "fast-model",
                    "aliases": ["fast"],
                    "meta": {"n_ctx": 65_536},
                },
            ]
        }

    monkeypatch.setattr(zeta_model, "request_model_metadata", fake_metadata)

    tokens = zeta_model.model_context_tokens(
        "http://127.0.0.1:8080/v1/chat/completions",
        "fast",
    )

    assert tokens == 65_536


def test_zeta_model_context_tokens_returns_none_when_unavailable(
    monkeypatch,
) -> None:
    zeta_model._MODEL_CONTEXT_TOKENS_CACHE.clear()
    monkeypatch.setattr(
        zeta_model,
        "request_model_metadata",
        lambda *args, **kwargs: {},
    )

    tokens = zeta_model.model_context_tokens(
        "http://127.0.0.1:8080/v1/chat/completions",
        "local-model",
    )

    assert tokens is None


def test_zeta_chat_completion_messages_accepts_request_model(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_request(
        body: dict[str, Any],
        *,
        selected_url: str | None = None,
    ) -> dict[str, Any]:
        captured["body"] = body
        captured["selected_url"] = selected_url
        return {"choices": [{"message": {"content": "done"}}]}

    monkeypatch.setattr(zeta_model, "request_chat_completion", fake_request)

    message = zeta_model.chat_completion_messages(
        [{"role": "user", "content": "hi"}],
        selected_model="fast-model",
        selected_url="http://127.0.0.1:8081/v1/chat/completions",
    )

    assert message == {"content": "done"}
    body = cast(dict[str, Any], captured["body"])
    assert body["model"] == "fast-model"
    assert body["stream_options"] == {"include_usage": True}
    assert captured["selected_url"] == "http://127.0.0.1:8081/v1/chat/completions"


def test_zeta_chat_completion_messages_sends_native_tools(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_request(body: dict[str, Any]) -> dict[str, Any]:
        captured["body"] = body
        return {"choices": [{"message": {"content": "done"}}]}

    monkeypatch.setattr(zeta_model, "request_chat_completion", fake_request)

    message = zeta_model.chat_completion_messages(
        [{"role": "user", "content": "hi"}],
        tools=[
            {
                "type": "function",
                "function": {"name": "read", "description": "", "parameters": {}},
            }
        ],
    )

    assert message == {"content": "done"}
    body = cast(dict[str, Any], captured["body"])
    assert body["tools"][0]["function"]["name"] == "read"
    assert body["tool_choice"] == "auto"
    assert body["stream_options"] == {"include_usage": True}
    assert "response_format" not in body


def test_zeta_chat_completion_messages_defaults_to_large_max_tokens(
    monkeypatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_request(body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        captured["body"] = body
        return {"choices": [{"message": {"content": "done"}}]}

    monkeypatch.setattr(zeta_model, "request_chat_completion", fake_request)

    zeta_model.chat_completion_messages([{"role": "user", "content": "hi"}])

    body = cast(dict[str, Any], captured["body"])
    assert body["max_tokens"] == zeta_model.DEFAULT_MAX_COMPLETION_TOKENS
    assert zeta_model.DEFAULT_MAX_COMPLETION_TOKENS == 8192


def test_zeta_chat_completion_messages_rejects_tool_calls_cut_by_max_tokens(
    monkeypatch,
) -> None:
    def fake_request(body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-0",
                                "type": "function",
                                "function": {
                                    "name": "write",
                                    "arguments": '{"path": "doc.md", "content": "trunca',
                                },
                            }
                        ],
                    },
                    "finish_reason": "length",
                }
            ]
        }

    monkeypatch.setattr(zeta_model, "request_chat_completion", fake_request)

    with pytest.raises(RuntimeError, match="max_tokens"):
        zeta_model.chat_completion_messages([{"role": "user", "content": "hi"}])


def test_zeta_chat_completion_messages_keeps_text_cut_by_max_tokens(
    monkeypatch,
) -> None:
    def fake_request(body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "partial answer"},
                    "finish_reason": "length",
                }
            ]
        }

    monkeypatch.setattr(zeta_model, "request_chat_completion", fake_request)

    message = zeta_model.chat_completion_messages([{"role": "user", "content": "hi"}])

    assert message["content"] == "partial answer"


def test_zeta_chat_structured_output_sends_json_schema(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    state = task_state_fixture(objective="extract task state")

    def fake_request(
        body: dict[str, Any],
        *,
        selected_url: str | None = None,
    ) -> dict[str, Any]:
        captured["body"] = body
        captured["selected_url"] = selected_url
        return {"choices": [{"message": {"content": json.dumps(state)}}]}

    monkeypatch.setattr(zeta_model, "request_chat_completion", fake_request)

    extracted = zeta_model.chat_structured_output(
        [{"role": "user", "content": "history"}],
        schema=zeta_prompt.TASK_STATE_SCHEMA,
        response_name="zeta_task_state",
        selected_model="state-model",
        selected_url="http://127.0.0.1:8081/v1/chat/completions",
    )

    assert extracted == state
    body = cast(dict[str, Any], captured["body"])
    assert body["model"] == "state-model"
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["name"] == "zeta_task_state"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert (
        body["response_format"]["json_schema"]["schema"]
        == zeta_prompt.TASK_STATE_SCHEMA
    )
    assert captured["selected_url"] == "http://127.0.0.1:8081/v1/chat/completions"


def test_zeta_chat_structured_output_rejects_invalid_json_schema(
    monkeypatch,
) -> None:
    def fake_request(
        body: dict[str, Any],
        *,
        selected_url: str | None = None,
    ) -> dict[str, Any]:
        del body
        del selected_url
        return {"choices": [{"message": {"content": "{}"}}]}

    monkeypatch.setattr(zeta_model, "request_chat_completion", fake_request)

    with pytest.raises(RuntimeError, match="validation"):
        zeta_model.chat_structured_output(
            [{"role": "user", "content": "history"}],
            schema=zeta_prompt.TASK_STATE_SCHEMA,
            response_name="zeta_task_state",
        )


def test_zeta_chat_completion_messages_reports_model_telemetry(
    monkeypatch,
) -> None:
    telemetry: list[dict[str, Any]] = []

    def fake_request(body: dict[str, Any]) -> dict[str, Any]:
        del body
        return {
            "usage": {
                "prompt_tokens": 123,
                "completion_tokens": 4,
                "total_tokens": 127,
            },
            "choices": [{"message": {"content": "done"}}],
        }

    monkeypatch.setattr(zeta_model, "model_context_tokens", lambda *args: 262_144)
    monkeypatch.setattr(zeta_model, "request_chat_completion", fake_request)

    message = zeta_model.chat_completion_messages(
        [{"role": "user", "content": "hi"}],
        telemetry_sink=telemetry.append,
    )

    assert message == {"content": "done"}
    assert telemetry == [
        {
            "usage": {
                "prompt_tokens": 123,
                "completion_tokens": 4,
                "total_tokens": 127,
            },
            "model_context_tokens": 262_144,
        }
    ]


def test_zeta_ensure_server_banner_respects_non_tty(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        zeta_model, "model_endpoint_open", lambda selected_url=None: False
    )

    assert zeta_model.ensure_server() is False

    err = capsys.readouterr().err
    assert "no OpenAI-compatible endpoint reachable" in err
    assert "\x1b[" not in err


def test_zeta_model_use_without_shell_session_notes_default_scope(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    write_models_config(
        home,
        """
[[models]]
name = "fast"
model = "fast-model"
url = "http://127.0.0.1:8081/v1/chat/completions"
""",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("SIGIL_SESSION_ID", raising=False)

    result = CliRunner().invoke(sigil_cli, ["model", "use", "fast"])

    assert result.exit_code == 0, result.output
    assert "model: fast -> fast-model" in result.stdout
    assert 'applies to session "default"' in result.stderr

    monkeypatch.setenv("SIGIL_SESSION_ID", "bound")
    bound = CliRunner().invoke(sigil_cli, ["model", "use", "fast"])
    assert bound.exit_code == 0, bound.output
    assert "default" not in bound.stderr
