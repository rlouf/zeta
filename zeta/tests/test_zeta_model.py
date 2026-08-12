"""Model request, streaming, and model-selection tests."""

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import zeta.models.chat_completions as zeta_model
import zeta.models.codex_auth as zeta_codex_auth
import zeta.models.limits as zeta_model_limits
import zeta.models.profiles as zeta_models
import zeta.models.responses as zeta_responses
import zeta.models.sse as zeta_model_sse
import zeta.models.types as zeta_model_shapes
import zeta.models.types as zeta_models_api
from click.testing import CliRunner
from zeta.cli.main import cli as zeta_cli
from zeta.context.compaction.task_state import TASK_STATE_SCHEMA
from zeta_test_support import (
    DeltaSink,
    sse_lines,
    task_state_fixture,
    write_models_config,
)

AGENT_VECTORS_DIR = Path(__file__).resolve().parents[2] / "spec" / "vectors" / "agent"


def _drain(agen: Any) -> list[Any]:
    """Collect an async generator from a synchronous test."""

    async def run() -> list[Any]:
        return [item async for item in agen]

    return asyncio.run(run())


async def _aiter(items: Any) -> Any:
    """Feed a list of SSE frames to a reader that now takes an async iterator."""
    for item in items:
        yield item


def _read_stream(reader: Any, frames: Any, **kwargs: Any) -> Any:
    """Run an async SSE reader from a synchronous test."""
    return asyncio.run(reader(_aiter(frames), **kwargs))


def test_zeta_model_config_ignores_model_env_vars(monkeypatch) -> None:
    monkeypatch.delenv("ZETA_MODEL_IDLE_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setenv("ZETA_MODEL_URL", "http://zeta.invalid/v1/chat/completions")
    monkeypatch.setenv("ZETA_MODEL_NAME", "zeta-model")

    assert zeta_model.model_url() == zeta_models.DEFAULT_MODEL_URL
    assert zeta_model.model_name() == zeta_models.DEFAULT_MODEL_NAME
    assert (
        zeta_model_limits.model_idle_timeout()
        == zeta_model_limits.DEFAULT_MODEL_IDLE_TIMEOUT_SECONDS
    )
    assert zeta_model_limits.DEFAULT_MODEL_IDLE_TIMEOUT_SECONDS == 120.0

    monkeypatch.setenv("ZETA_MODEL_IDLE_TIMEOUT_SECONDS", "2.5")
    assert zeta_model_limits.model_idle_timeout() == 2.5

    monkeypatch.setenv("ZETA_MODEL_IDLE_TIMEOUT_SECONDS", "0")
    assert zeta_model_limits.model_idle_timeout() is None


def test_zeta_model_first_output_timeout_uses_zeta_env(monkeypatch) -> None:
    monkeypatch.delenv("ZETA_MODEL_FIRST_OUTPUT_TIMEOUT_SECONDS", raising=False)

    assert (
        zeta_model_limits.model_first_output_timeout()
        == zeta_model_limits.DEFAULT_MODEL_FIRST_OUTPUT_TIMEOUT_SECONDS
    )
    assert zeta_model_limits.DEFAULT_MODEL_FIRST_OUTPUT_TIMEOUT_SECONDS == 600.0

    monkeypatch.setenv("ZETA_MODEL_FIRST_OUTPUT_TIMEOUT_SECONDS", "45")
    assert zeta_model_limits.model_first_output_timeout() == 45.0

    monkeypatch.setenv("ZETA_MODEL_FIRST_OUTPUT_TIMEOUT_SECONDS", "0")
    assert zeta_model_limits.model_first_output_timeout() is None


def test_zeta_model_input_renders_existing_chat_completion_request() -> None:
    model_input = zeta_models_api.ModelInput(
        messages=[{"role": "user", "content": "hi"}],
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
        max_tokens=128,
        selected_model="unit-model",
        thinking="low",
    )

    assert zeta_model.chat_completion_request_from_input(model_input) == {
        "model": "unit-model",
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": 0.2,
        "max_tokens": 128,
        "stream_options": {"include_usage": True},
        "reasoning_effort": "low",
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "read",
                    "description": "Read a file.",
                    "parameters": {"type": "object"},
                },
            }
        ],
        "tool_choice": "auto",
    }


def test_zeta_model_output_from_chat_completion_preserves_message_usage_metadata() -> (
    None
):
    output = zeta_model.model_output_from_chat_completion(
        {
            "id": "chatcmpl-1",
            "model": "unit-model",
            "system_fingerprint": "fp-1",
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "total_tokens": 12,
            },
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "done",
                        "reasoning_content": "thinking",
                    },
                    "finish_reason": "stop",
                }
            ],
        }
    )

    assert output.message == {
        "role": "assistant",
        "content": "done",
        "reasoning_content": "thinking",
    }
    assert output.finish_reason == "stop"
    assert output.usage == zeta_models_api.ModelUsage(
        prompt_tokens=10,
        completion_tokens=2,
        total_tokens=12,
    )
    assert output.provider_metadata == {
        "id": "chatcmpl-1",
        "model": "unit-model",
        "system_fingerprint": "fp-1",
    }
    assert output.provider_replay_items == ()


def test_zeta_chat_completion_model_output_from_stream_payload() -> None:
    payload = _read_stream(
        zeta_model_sse.read_streamed_chat_completion,
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
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                },
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
    )

    output = zeta_model.model_output_from_chat_completion(payload)

    assert output.message == {"role": "assistant", "content": "hello"}
    assert output.finish_reason == "stop"
    assert output.usage == zeta_models_api.ModelUsage(
        prompt_tokens=3,
        completion_tokens=2,
        total_tokens=5,
    )
    assert output.provider_metadata == {"id": "chatcmpl-test"}


def test_zeta_request_chat_completion_streams_final_message(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    events = sse_lines(
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

    async def fake_stream_json_sse(
        url: str,
        body: dict[str, Any],
        *,
        headers: dict[str, str],
        should_stop: object | None = None,
    ) -> Any:
        captured["url"] = url
        captured["body"] = body
        captured["accept"] = headers["Accept"]
        for _frame in events:
            yield _frame

    monkeypatch.setattr(zeta_model, "stream_json_sse", fake_stream_json_sse)
    body = {"model": "local-model", "messages": []}

    payload = asyncio.run(zeta_model.request_chat_completion(body))

    assert body == {"model": "local-model", "messages": []}
    assert captured["body"]["stream"] is True
    assert captured["accept"] == "text/event-stream"
    assert payload["id"] == "chatcmpl-test"
    assert payload["choices"][0]["message"] == {
        "role": "assistant",
        "content": "hello",
    }
    assert payload["choices"][0]["finish_reason"] == "stop"


def test_zeta_model_stream_timeout_uses_first_output_and_idle_timeouts() -> None:
    timeout = zeta_model_limits.model_stream_timeout(
        first_output_timeout=10.0,
        idle_timeout=2.5,
    )

    assert timeout.connect == 10.0
    assert timeout.write == 10.0
    assert timeout.pool == 10.0
    assert timeout.read == 2.5


def test_zeta_model_stream_timeout_can_disable_all_bounds() -> None:
    timeout = zeta_model_limits.model_stream_timeout(
        first_output_timeout=None,
        idle_timeout=None,
    )

    assert timeout.connect is None
    assert timeout.write is None
    assert timeout.pool is None
    assert timeout.read is None


def test_zeta_stream_forwards_reasoning_deltas_to_sink() -> None:
    sink = DeltaSink()

    payload = _read_stream(
        zeta_model_sse.read_streamed_chat_completion,
        sse_lines(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "reasoning_content": "think"},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"reasoning_content": "ing", "content": "done"},
                        "finish_reason": "stop",
                    }
                ],
            },
            "[DONE]",
        ),
        stream_sink=sink,
    )

    assert sink.reasoning_deltas == ["think", "ing"]
    assert sink.deltas == ["done"]
    message = payload["choices"][0]["message"]
    assert message["reasoning_content"] == "thinking"


def test_zeta_stream_emits_content_deltas_in_order() -> None:
    sink = DeltaSink()

    payload = _read_stream(
        zeta_model_sse.read_streamed_chat_completion,
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
    payload = _read_stream(
        zeta_model_sse.read_streamed_chat_completion,
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
        ),
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

    without_sink = _read_stream(zeta_model_sse.read_streamed_chat_completion, frames)
    with_sink = _read_stream(
        zeta_model_sse.read_streamed_chat_completion, frames, stream_sink=sink
    )

    assert with_sink == without_sink
    assert sink.deltas == ["done"]


def test_zeta_stream_does_not_render_tool_call_fragments() -> None:
    sink = DeltaSink()

    payload = _read_stream(
        zeta_model_sse.read_streamed_chat_completion,
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

    payload = _read_stream(
        zeta_model_sse.read_streamed_chat_completion,
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
    payload = _read_stream(
        zeta_model_sse.read_streamed_chat_completion,
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
    payload = _read_stream(
        zeta_model_sse.read_streamed_chat_completion,
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
        ),
    )

    calls = payload["choices"][0]["message"]["tool_calls"]
    assert [call["id"] for call in calls] == ["call-read", "call-ls"]


def test_zeta_request_chat_completion_closes_stream_on_error(monkeypatch) -> None:
    closed = False

    async def fake_stream_json_sse(
        url: str,
        body: dict[str, Any],
        *,
        headers: dict[str, str],
        should_stop: object | None = None,
    ) -> Any:
        del url, body, headers, should_stop
        nonlocal closed
        try:
            for line in sse_lines({"error": {"message": "generation failed"}}):
                yield line
        finally:
            closed = True

    monkeypatch.setattr(zeta_model, "stream_json_sse", fake_stream_json_sse)

    with pytest.raises(RuntimeError, match="generation failed"):
        asyncio.run(
            zeta_model.request_chat_completion({"model": "local-model", "messages": []})
        )

    assert closed is True


def test_zeta_stream_rejects_malformed_events() -> None:
    with pytest.raises(RuntimeError, match="invalid JSON event"):
        _read_stream(zeta_model_sse.read_streamed_chat_completion, ["nope"])


def test_zeta_http_error_detail_surfaces_json_error_body() -> None:
    body = json.dumps(
        {"error": {"code": 500, "message": "Failed to parse tool call arguments"}}
    )
    request = httpx.Request("POST", "http://127.0.0.1:8080/v1/chat/completions")
    response = httpx.Response(500, content=body, request=request)
    error = httpx.HTTPStatusError("boom", request=request, response=response)

    message = zeta_model_sse.http_error_detail(error)
    assert "boom" in message
    assert "Failed to parse tool call arguments" in message


def test_zeta_http_error_detail_surfaces_plain_error_body() -> None:
    request = httpx.Request("POST", "http://127.0.0.1:8080/v1/chat/completions")
    response = httpx.Response(502, content=b"upstream exploded", request=request)
    error = httpx.HTTPStatusError("boom", request=request, response=response)

    message = zeta_model_sse.http_error_detail(error)
    assert "boom" in message
    assert "upstream exploded" in message


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
        url=zeta_models.DEFAULT_MODEL_URL,
    )


def test_zeta_request_body_leaves_thinking_to_the_model_by_default() -> None:
    body = zeta_model.chat_completion_request_body([{"role": "user", "content": "hi"}])

    assert "chat_template_kwargs" not in body
    assert "reasoning_effort" not in body


def test_zeta_request_body_disables_thinking_for_none() -> None:
    body = zeta_model.chat_completion_request_body(
        [{"role": "user", "content": "hi"}],
        thinking="none",
    )

    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert "reasoning_effort" not in body


def test_zeta_request_body_sends_reasoning_effort() -> None:
    body = zeta_model.chat_completion_request_body(
        [{"role": "user", "content": "hi"}],
        thinking="high",
    )

    assert body["reasoning_effort"] == "high"
    assert "chat_template_kwargs" not in body


def test_zeta_model_profiles_read_thinking(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    write_models_config(
        home,
        """
[[models]]
name = "quiet"
model = "quiet-model"
thinking = "none"

[[models]]
name = "deep"
model = "deep-model"
thinking = "high"

[[models]]
name = "default"
model = "default-model"
""",
    )
    monkeypatch.setenv("HOME", str(home))

    catalog = zeta_models.load_model_profiles()
    quiet = zeta_models.resolve_model_profile("quiet", catalog=catalog)
    deep = zeta_models.resolve_model_profile("deep", catalog=catalog)
    default = zeta_models.resolve_model_profile("default", catalog=catalog)

    assert catalog.diagnostics == []
    assert quiet is not None and quiet.thinking == "none"
    assert deep is not None and deep.thinking == "high"
    assert default is not None and default.thinking is None


def test_zeta_model_profiles_reject_unknown_thinking(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    write_models_config(
        home,
        """
[[models]]
name = "quiet"
model = "quiet-model"
thinking = "off"
""",
    )
    monkeypatch.setenv("HOME", str(home))

    catalog = zeta_models.load_model_profiles()

    assert catalog.profiles == {}
    assert len(catalog.diagnostics) == 1
    assert "thinking" in catalog.diagnostics[0].message
    assert "none" in catalog.diagnostics[0].message


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


def test_zeta_model_profiles_report_missing_model(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    write_models_config(
        home,
        """
[[models]]
name = "missing-model"
""",
    )
    monkeypatch.setenv("HOME", str(home))

    catalog = zeta_models.load_model_profiles()

    assert catalog.profiles == {}
    assert len(catalog.diagnostics) == 1
    assert "model" in catalog.diagnostics[0].message


def test_zeta_models_resolve_active_model_reports_session_source(
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
    monkeypatch.setenv("COMMAS_SESSION_ID", "resolution-session")
    zeta_models.set_active_model_profile("fast")

    resolution = zeta_models.resolve_active_model()

    assert resolution.source == "session"
    assert resolution.stale_profile is None
    assert resolution.selection == zeta_models.ModelSelection(
        profile="fast",
        model="fast-model",
        url="http://127.0.0.1:8081/v1/chat/completions",
    )


def test_zeta_models_resolve_active_model_falls_back_to_builtin(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    resolution = zeta_models.resolve_active_model()

    assert resolution.source == "builtin"
    assert resolution.stale_profile is None
    assert resolution.selection == zeta_models.ModelSelection(
        profile=zeta_models.DEFAULT_CODEX_PROFILE_NAME,
        model=zeta_models.DEFAULT_CODEX_MODEL_NAME,
        url=zeta_models.DEFAULT_CODEX_BASE_URL,
        api="codex-responses",
        tool_profile="codex",
    )
    assert zeta_models.active_model_selection() == resolution.selection


def test_zeta_model_cli_lists_builtin_codex(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ZETA_STATE_DIR", str(tmp_path / "state"))

    result = CliRunner().invoke(zeta_cli, ["models", "list"])

    assert result.exit_code == 0, result.output
    assert "codex  gpt-5.6-sol  chatgpt.com  (active)" in result.output
    assert "no profiles configured; using built-in Codex" in result.output


def test_zeta_models_default_profile_resolves_without_selection(
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

[[models]]
name = "codex"
model = "gpt-5.5"
api = "codex-responses"
default = true
""",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("COMMAS_SESSION_ID", "default-profile-session")

    resolution = zeta_models.resolve_active_model()
    selection = zeta_models.active_model_selection()

    assert resolution.source == "config"
    assert resolution.selection.profile == "codex"
    assert selection is not None and selection.profile == "codex"


def test_zeta_models_session_selection_beats_default_profile(
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

[[models]]
name = "codex"
model = "gpt-5.5"
api = "codex-responses"
default = true
""",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("COMMAS_SESSION_ID", "selection-beats-default")
    zeta_models.set_active_model_profile("fast")

    resolution = zeta_models.resolve_active_model()

    assert resolution.source == "session"
    assert resolution.selection.profile == "fast"


def test_zeta_models_rejects_multiple_default_profiles(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    write_models_config(
        home,
        """
[[models]]
name = "one"
model = "one-model"
default = true

[[models]]
name = "two"
model = "two-model"
default = true
""",
    )
    monkeypatch.setenv("HOME", str(home))

    catalog = zeta_models.load_model_profiles()

    assert catalog.default_profile == "one"
    assert len(catalog.diagnostics) == 1
    assert "default" in catalog.diagnostics[0].message


def test_zeta_models_preserves_truthy_default_without_selecting_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    write_models_config(
        home,
        """
[[models]]
name = "one"
model = "one-model"
default = "yes"
""",
    )
    monkeypatch.setenv("HOME", str(home))

    catalog = zeta_models.load_model_profiles()

    assert catalog.profiles["one"].default == "yes"
    assert catalog.default_profile is None
    assert catalog.diagnostics == []


def test_zeta_models_resolve_active_model_survives_vanished_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    write_models_config(home, "")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("COMMAS_SESSION_ID", "stale-session")
    zeta_models.set_active_model_profile("gone")

    resolution = zeta_models.resolve_active_model()

    assert resolution.source == "builtin"
    assert resolution.selection.profile == zeta_models.DEFAULT_CODEX_PROFILE_NAME
    assert resolution.stale_profile == "gone"


def test_zeta_model_context_tokens_prefers_props(monkeypatch) -> None:
    zeta_model_limits._MODEL_CONTEXT_TOKENS_CACHE.clear()
    calls: list[str] = []

    def fake_metadata(
        path: str,
        *,
        selected_url: str | None = None,
    ) -> dict[str, Any] | None:
        del selected_url
        calls.append(path)
        return {"default_generation_settings": {"n_ctx": 262_144}}

    monkeypatch.setattr(zeta_model_limits, "request_model_metadata", fake_metadata)

    tokens = zeta_model_limits.model_context_tokens(
        "http://127.0.0.1:8080/v1/chat/completions",
        "local-model",
    )

    assert tokens == 262_144
    assert calls == ["/props"]


def test_zeta_model_context_tokens_falls_back_to_selected_model(
    monkeypatch,
) -> None:
    zeta_model_limits._MODEL_CONTEXT_TOKENS_CACHE.clear()

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

    monkeypatch.setattr(zeta_model_limits, "request_model_metadata", fake_metadata)

    tokens = zeta_model_limits.model_context_tokens(
        "http://127.0.0.1:8080/v1/chat/completions",
        "fast",
    )

    assert tokens == 65_536


def test_zeta_model_context_tokens_reads_model_context_length(
    monkeypatch,
) -> None:
    zeta_model_limits._MODEL_CONTEXT_TOKENS_CACHE.clear()

    def fake_metadata(
        path: str,
        *,
        selected_url: str | None = None,
    ) -> dict[str, Any] | None:
        del selected_url
        if path == "/props":
            return {"error": {"message": "unknown endpoint"}}
        return {
            "data": [
                {
                    "id": "deepseek-v4-flash",
                    "context_length": 100_000,
                    "top_provider": {"context_length": 100_000},
                }
            ]
        }

    monkeypatch.setattr(zeta_model_limits, "request_model_metadata", fake_metadata)

    tokens = zeta_model_limits.model_context_tokens(
        "http://127.0.0.1:8000/v1/chat/completions",
        "deepseek-v4-flash",
    )

    assert tokens == 100_000


def test_zeta_model_context_tokens_returns_none_when_unavailable(
    monkeypatch,
) -> None:
    zeta_model_limits._MODEL_CONTEXT_TOKENS_CACHE.clear()
    monkeypatch.setattr(
        zeta_model_limits,
        "request_model_metadata",
        lambda *args, **kwargs: {},
    )

    tokens = zeta_model_limits.model_context_tokens(
        "http://127.0.0.1:8080/v1/chat/completions",
        "local-model",
    )

    assert tokens is None


def test_zeta_stream_json_sse_accepts_missing_content_type(monkeypatch) -> None:
    class FakeStreamResponse:
        async def __aenter__(self) -> "FakeStreamResponse":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        async def aiter_lines(self) -> Any:
            for _line in [
                "event: response.output_text.delta",
                'data: {"type":"response.output_text.delta","delta":"ok"}',
                "",
                "data: [DONE]",
                "",
            ]:
                yield _line

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def stream(self, *args: object, **kwargs: object) -> FakeStreamResponse:
            return FakeStreamResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    events = _drain(
        zeta_model_sse.stream_json_sse(
            "https://chatgpt.com/backend-api/codex/responses",
            {"model": "gpt-5.5"},
            headers={"Accept": "text/event-stream"},
        )
    )

    assert events == [
        '{"type":"response.output_text.delta","delta":"ok"}',
        "[DONE]",
    ]


def test_zeta_stream_json_sse_preserves_error_body(monkeypatch) -> None:
    class FakeStreamResponse:
        is_error = True

        def __init__(self) -> None:
            self._content = b""

        async def __aenter__(self) -> "FakeStreamResponse":
            return self

        async def __aexit__(self, *args: object) -> None:
            self._content = b""

        async def aread(self) -> bytes:
            self._content = (
                b'{"error":{"message":"tool schema was rejected by provider"}}'
            )
            return self._content

        @property
        def text(self) -> str:
            return self._content.decode()

        def raise_for_status(self) -> None:
            request = httpx.Request("POST", "https://example.test/v1/chat")
            response = httpx.Response(
                400,
                content=self._content,
                request=request,
            )
            raise httpx.HTTPStatusError(
                "bad request", request=request, response=response
            )

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def stream(self, *args: object, **kwargs: object) -> FakeStreamResponse:
            return FakeStreamResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    with pytest.raises(RuntimeError, match="tool schema was rejected"):
        _drain(
            zeta_model_sse.stream_json_sse(
                "https://example.test/v1/chat",
                {"model": "test"},
                headers={},
            )
        )


def test_zeta_chat_completion_messages_accepts_request_model(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_request(
        body: dict[str, Any],
        *,
        selected_url: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        captured["body"] = body
        captured["selected_url"] = selected_url
        return {"choices": [{"message": {"content": "done"}}]}

    monkeypatch.setattr(zeta_model, "request_chat_completion", fake_request)

    message = asyncio.run(
        zeta_model.chat_completion_messages(
            [{"role": "user", "content": "hi"}],
            zeta_model_shapes.ModelRequest(
                model="fast-model",
                url="http://127.0.0.1:8081/v1/chat/completions",
            ),
        )
    )

    assert message == {"content": "done"}
    body = cast(dict[str, Any], captured["body"])
    assert body["model"] == "fast-model"
    assert body["stream_options"] == {"include_usage": True}
    assert captured["selected_url"] == "http://127.0.0.1:8081/v1/chat/completions"


def test_zeta_chat_completion_messages_returns_adapter_message(monkeypatch) -> None:
    payload = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "raw"},
                "finish_reason": "stop",
            }
        ]
    }
    converted: list[dict[str, Any]] = []

    async def fake_request(body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
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

    monkeypatch.setattr(zeta_model, "request_chat_completion", fake_request)
    monkeypatch.setattr(
        zeta_model,
        "model_output_from_chat_completion",
        fake_model_output,
    )

    message = asyncio.run(
        zeta_model.chat_completion_messages(
            [{"role": "user", "content": "hi"}], zeta_model_shapes.ModelRequest()
        )
    )

    assert message == {"role": "assistant", "content": "converted"}
    assert converted == [payload]


def test_zeta_chat_completion_messages_sends_native_tools(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_request(body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        captured["body"] = body
        return {"choices": [{"message": {"content": "done"}}]}

    monkeypatch.setattr(zeta_model, "request_chat_completion", fake_request)

    message = asyncio.run(
        zeta_model.chat_completion_messages(
            [{"role": "user", "content": "hi"}],
            zeta_model_shapes.ModelRequest(),
            tools=[
                {
                    "type": "function",
                    "function": {"name": "read", "description": "", "parameters": {}},
                }
            ],
        )
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

    async def fake_request(body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        captured["body"] = body
        return {"choices": [{"message": {"content": "done"}}]}

    monkeypatch.setattr(zeta_model, "request_chat_completion", fake_request)

    asyncio.run(
        zeta_model.chat_completion_messages(
            [{"role": "user", "content": "hi"}], zeta_model_shapes.ModelRequest()
        )
    )

    body = cast(dict[str, Any], captured["body"])
    assert body["max_tokens"] == zeta_model.DEFAULT_MAX_COMPLETION_TOKENS
    assert zeta_model.DEFAULT_MAX_COMPLETION_TOKENS == 8192


def test_zeta_chat_completion_messages_rejects_tool_calls_cut_by_max_tokens(
    monkeypatch,
) -> None:
    async def fake_request(body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
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
        asyncio.run(
            zeta_model.chat_completion_messages(
                [{"role": "user", "content": "hi"}], zeta_model_shapes.ModelRequest()
            )
        )


def test_zeta_chat_completion_messages_keeps_text_cut_by_max_tokens(
    monkeypatch,
) -> None:
    async def fake_request(body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "partial answer"},
                    "finish_reason": "length",
                }
            ]
        }

    monkeypatch.setattr(zeta_model, "request_chat_completion", fake_request)

    message = asyncio.run(
        zeta_model.chat_completion_messages(
            [{"role": "user", "content": "hi"}], zeta_model_shapes.ModelRequest()
        )
    )

    assert message["content"] == "partial answer"


def test_zeta_chat_structured_output_sends_json_schema(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    state = task_state_fixture(objective="extract task state")

    async def fake_request(
        body: dict[str, Any],
        *,
        selected_url: str | None = None,
    ) -> dict[str, Any]:
        captured["body"] = body
        captured["selected_url"] = selected_url
        return {"choices": [{"message": {"content": json.dumps(state)}}]}

    monkeypatch.setattr(zeta_model, "request_chat_completion", fake_request)

    extracted = asyncio.run(
        zeta_model.chat_structured_output(
            [{"role": "user", "content": "history"}],
            zeta_model_shapes.ModelRequest(
                model="state-model", url="http://127.0.0.1:8081/v1/chat/completions"
            ),
            schema=TASK_STATE_SCHEMA,
            response_name="zeta_task_state",
        )
    )

    assert extracted == state
    body = cast(dict[str, Any], captured["body"])
    assert body["model"] == "state-model"
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["name"] == "zeta_task_state"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"] == TASK_STATE_SCHEMA
    assert captured["selected_url"] == "http://127.0.0.1:8081/v1/chat/completions"


def test_zeta_chat_structured_output_rejects_invalid_json_schema(
    monkeypatch,
) -> None:
    async def fake_request(
        body: dict[str, Any],
        *,
        selected_url: str | None = None,
    ) -> dict[str, Any]:
        del body
        del selected_url
        return {"choices": [{"message": {"content": "{}"}}]}

    monkeypatch.setattr(zeta_model, "request_chat_completion", fake_request)

    with pytest.raises(RuntimeError, match="validation"):
        asyncio.run(
            zeta_model.chat_structured_output(
                [{"role": "user", "content": "history"}],
                zeta_model_shapes.ModelRequest(),
                schema=TASK_STATE_SCHEMA,
                response_name="zeta_task_state",
            )
        )


def test_zeta_chat_completion_messages_reports_model_telemetry(
    monkeypatch,
) -> None:
    telemetry: list[dict[str, Any]] = []

    async def fake_request(body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
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

    message = asyncio.run(
        zeta_model.chat_completion_messages(
            [{"role": "user", "content": "hi"}],
            zeta_model_shapes.ModelRequest(),
            telemetry_sink=telemetry.append,
        )
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


def test_zeta_model_profiles_read_api(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    write_models_config(
        home,
        """
[[models]]
name = "codex"
model = "gpt-5.5"
api = "codex-responses"

[[models]]
name = "local"
model = "local-model"
""",
    )
    monkeypatch.setenv("HOME", str(home))

    catalog = zeta_models.load_model_profiles()
    codex = zeta_models.resolve_model_profile("codex", catalog=catalog)
    local = zeta_models.resolve_model_profile("local", catalog=catalog)

    assert catalog.diagnostics == []
    assert codex is not None and codex.api == "codex-responses"
    assert codex.url == zeta_models.DEFAULT_CODEX_BASE_URL
    assert local is not None and local.api == "chat-completions"


def test_zeta_model_profiles_reject_unknown_api(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    write_models_config(
        home,
        """
[[models]]
name = "bad"
model = "bad-model"
api = "grpc"
""",
    )
    monkeypatch.setenv("HOME", str(home))

    catalog = zeta_models.load_model_profiles()

    assert catalog.profiles == {}
    assert len(catalog.diagnostics) == 1
    assert "api" in catalog.diagnostics[0].message


def test_zeta_models_package_dispatches_default_api_to_chat_completions(
    monkeypatch,
) -> None:
    from zeta import models as models_pkg

    captured: dict[str, Any] = {}

    async def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        request: Any = None,
        **options: Any,
    ) -> dict[str, Any]:
        captured["messages"] = messages
        captured["options"] = options
        captured["request"] = request
        return {"role": "assistant", "content": "ok"}

    monkeypatch.setattr(
        zeta_model, "chat_completion_messages", fake_chat_completion_messages
    )

    message = asyncio.run(
        models_pkg.chat_completion_messages(
            [{"role": "user", "content": "hi"}],
            zeta_model_shapes.ModelRequest(thinking="low"),
        )
    )

    assert message == {"role": "assistant", "content": "ok"}
    assert captured["messages"] == [{"role": "user", "content": "hi"}]
    assert captured["options"] == {}
    assert captured["request"].thinking == "low"


def test_zeta_models_package_routes_codex_api_to_responses(monkeypatch) -> None:
    import zeta.models.responses as zeta_responses

    from zeta import models as models_pkg

    captured: dict[str, Any] = {}

    async def fake_completion(
        messages: list[dict[str, Any]], request=None, **options: Any
    ) -> dict:
        captured["completion"] = (messages, options)
        return {"role": "assistant", "content": "ok"}

    async def fake_structured(
        messages: list[dict[str, Any]], request=None, **options: Any
    ) -> dict:
        captured["structured"] = (messages, options)
        return {"state": "done"}

    monkeypatch.setattr(zeta_responses, "codex_completion_messages", fake_completion)
    monkeypatch.setattr(zeta_responses, "codex_structured_output", fake_structured)

    message = asyncio.run(
        models_pkg.chat_completion_messages(
            [{"role": "user", "content": "hi"}],
            zeta_model_shapes.ModelRequest(api="codex-responses", thinking="low"),
        )
    )
    data = asyncio.run(
        models_pkg.chat_structured_output(
            [{"role": "user", "content": "hi"}],
            zeta_model_shapes.ModelRequest(api="codex-responses"),
            schema={"type": "object"},
            response_name="state",
        )
    )

    assert message == {"role": "assistant", "content": "ok"}
    assert data == {"state": "done"}
    assert captured["completion"][1] == {}
    assert captured["structured"][1]["response_name"] == "state"


def test_zeta_models_package_omits_session_id_for_chat_structured_output(
    monkeypatch,
) -> None:
    from zeta import models as models_pkg

    captured: dict[str, Any] = {}

    async def fake_structured(
        messages: list[dict[str, Any]],
        request: Any = None,
        **options: Any,
    ) -> dict[str, Any]:
        captured["messages"] = messages
        captured["options"] = options
        captured["request"] = request
        return {"state": "done"}

    monkeypatch.setattr(zeta_model, "chat_structured_output", fake_structured)

    data = asyncio.run(
        models_pkg.chat_structured_output(
            [{"role": "user", "content": "hi"}],
            zeta_model_shapes.ModelRequest(session_id="agent/session"),
            schema={"type": "object"},
            response_name="state",
        )
    )

    assert data == {"state": "done"}
    assert captured["options"] == {
        "schema": {"type": "object"},
        "response_name": "state",
    }


def test_zeta_default_model_gateway_passes_one_request_to_every_backend(
    monkeypatch,
) -> None:
    """Both protocols receive the same request; neither needs a special case."""
    from zeta import models as models_pkg

    captured: dict[str, Any] = {}

    async def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        request: zeta_model_shapes.ModelRequest,
        **options: Any,
    ) -> dict[str, Any]:
        captured["messages"] = messages
        captured["request"] = request
        captured["options"] = options
        captured["request"] = request
        return {"role": "assistant", "content": "ok"}

    monkeypatch.setattr(
        models_pkg, "chat_completion_messages", fake_chat_completion_messages
    )

    for api in (None, zeta_models.CODEX_RESPONSES_API):
        output = asyncio.run(
            models_pkg.DefaultModelGateway().generate(
                zeta_models_api.ModelInput(
                    messages=[{"role": "user", "content": "hi"}],
                    tools=[],
                    tool_choice="auto",
                ),
                zeta_model_shapes.ModelRequest(
                    api=api,
                    model="unit-model",
                    url="http://model.invalid/v1/chat/completions",
                    thinking="none",
                    session_id="agent/session",
                ),
            )
        )

        assert output.message == {"role": "assistant", "content": "ok"}
        # session_id rides on the request for every protocol, so the gateway
        # adds nothing and strips nothing.
        assert captured["request"].session_id == "agent/session"
        assert captured["request"].api == api
        assert captured["options"] == {
            "tools": [],
            "tool_choice": "auto",
            "stream_sink": None,
            "telemetry_sink": None,
            "should_stop": None,
        }


def test_zeta_models_package_rejects_unknown_api() -> None:
    from zeta import models as models_pkg

    with pytest.raises(ValueError, match="grpc"):
        asyncio.run(
            models_pkg.chat_completion_messages(
                [{"role": "user", "content": "hi"}],
                zeta_model_shapes.ModelRequest(api="grpc"),
            )
        )


def test_zeta_model_cli_list_resolves_urls_and_marks_active_config_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    write_models_config(
        home,
        """
[[models]]
name = "codex"
model = "gpt-5.5"
api = "codex-responses"
default = true

[[models]]
name = "fast"
model = "fast-model"
""",
    )
    monkeypatch.setenv("HOME", str(home))

    result = CliRunner().invoke(zeta_cli, ["models", "list"])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert "codex  gpt-5.5     chatgpt.com     (active)" in lines
    assert "fast   fast-model  127.0.0.1:8080" in lines
    assert lines[0].index("chatgpt.com") == lines[1].index("127.0.0.1")


def test_zeta_model_cli_list_marks_session_profile_active(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    write_models_config(
        home,
        """
[[models]]
name = "codex"
model = "gpt-5.5"
api = "codex-responses"
default = true

[[models]]
name = "fast"
model = "fast-model"
""",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ZETA_STATE_DIR", str(tmp_path / ".zeta"))
    monkeypatch.setenv("ZETA_SESSION_ID", "list-active-session")
    zeta_models.set_active_model_profile("fast")

    result = CliRunner().invoke(zeta_cli, ["models", "list"])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert "codex  gpt-5.5     chatgpt.com" in lines
    assert "fast   fast-model  127.0.0.1:8080  (active)" in lines
    assert lines[0].index("chatgpt.com") == lines[1].index("127.0.0.1")


def test_zeta_model_cli_show_reports_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    write_models_config(
        home,
        """
[[models]]
name = "codex"
model = "gpt-5.5"
api = "codex-responses"
default = true
""",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("COMMAS_SESSION_ID", "show-source-session")

    result = CliRunner().invoke(zeta_cli, ["models", "show"])

    assert result.exit_code == 0, result.output
    assert (
        "model: codex -> gpt-5.5 @ https://chatgpt.com/backend-api (config)"
        in result.output
    )


async def test_zeta_stream_json_sse_stops_between_frames_when_the_run_aborts(
    monkeypatch,
) -> None:
    """A cancelled run must not wait for the whole generation."""
    frames = ["a", "b", "c", "d"]
    delivered: list[str] = []

    async def fake_parse_sse_lines(lines: Any) -> Any:
        for frame in frames:
            yield frame

    class FakeResponse:
        is_error = False

        def raise_for_status(self) -> None:
            return None

        async def aiter_lines(self) -> Any:
            for line in ():  # the fake stream yields no raw lines
                yield line

    class FakeStream:
        async def __aenter__(self) -> FakeResponse:
            return FakeResponse()

        async def __aexit__(self, *_: object) -> bool:
            return False

    class FakeClient:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_: object) -> bool:
            return False

        def stream(self, *_: object, **__: object) -> FakeStream:
            return FakeStream()

    monkeypatch.setattr(zeta_model_sse, "parse_sse_lines", fake_parse_sse_lines)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_: FakeClient())

    def should_stop() -> str | None:
        return "cancelled" if len(delivered) >= 2 else None

    with pytest.raises(zeta_model_shapes.ModelRequestAborted, match="cancelled"):
        async for frame in zeta_model_sse.stream_json_sse(
            "http://127.0.0.1:8080/v1/chat/completions",
            {},
            headers={},
            should_stop=should_stop,
        ):
            delivered.append(frame)

    assert delivered == ["a", "b"]  # stopped mid-stream, not after all four


def compact_model_event(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def captured_runtime_error(call: Any) -> str:
    try:
        call()
    except RuntimeError as exc:
        return str(exc)
    raise AssertionError("expected RuntimeError")


def python_model_vectors() -> dict[str, Any]:
    tool = {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Read a file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    }
    forced_read = {"type": "function", "function": {"name": "read"}}
    chat_requests = []
    chat_request_cases: list[tuple[str, dict[str, Any]]] = [
        (
            "tools_and_reasoning",
            {
                "messages": [
                    {"role": "system", "content": "Be exact."},
                    {"role": "user", "content": "Read README.md."},
                ],
                "tools": [tool],
                "tool_choice": forced_read,
                "max_tokens": 512,
                "selected_model": "unit-chat-model",
                "thinking": "high",
            },
        ),
        (
            "thinking_disabled",
            {
                "messages": [{"role": "user", "content": "Answer."}],
                "tools": None,
                "tool_choice": "auto",
                "max_tokens": 64,
                "selected_model": "unit-chat-model",
                "thinking": "none",
            },
        ),
    ]
    for name, inputs in chat_request_cases:
        chat_requests.append(
            {
                "name": name,
                "input": inputs,
                "expected": zeta_model.chat_completion_request_body(**inputs),
            }
        )

    chat_events = [
        {
            "id": "chatcmpl-vector",
            "object": "chat.completion.chunk",
            "created": 1786400000,
            "model": "unit-chat-model",
            "system_fingerprint": "fp-vector",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "reasoning_content": "inspect ",
                        "content": "I will ",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-read",
                                "type": "function",
                                "function": {
                                    "name": "re",
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
                        "reasoning_content": "then answer",
                        "content": "read it.",
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {
                                    "name": "ad",
                                    "arguments": ':"README.md"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 21,
                "completion_tokens": 8,
                "total_tokens": 29,
            },
        },
    ]
    chat_frames = [*(compact_model_event(event) for event in chat_events), "[DONE]"]
    chat_sink = DeltaSink()
    chat_expected = _read_stream(
        zeta_model_sse.read_streamed_chat_completion,
        chat_frames,
        stream_sink=chat_sink,
    )

    sse_lines_input = [
        ": keepalive",
        'data: {"part":"first",',
        'data: "value":1}',
        "",
        "data: [DONE]",
        "",
    ]
    sse_frames_expected = _drain(
        zeta_model_sse.parse_sse_lines(_aiter(sse_lines_input))
    )

    chat_failures = []
    for name, frames in (
        ("malformed_json", ['{"choices":']),
        (
            "provider_error",
            [compact_model_event({"error": {"message": "provider unavailable"}})],
        ),
        (
            "missing_done",
            [
                compact_model_event(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": "partial"},
                                "finish_reason": None,
                            }
                        ]
                    }
                )
            ],
        ),
    ):
        chat_failures.append(
            {
                "name": name,
                "sse": frames,
                "expected_error": captured_runtime_error(
                    lambda frames=frames: _read_stream(
                        zeta_model_sse.read_streamed_chat_completion,
                        frames,
                    )
                ),
            }
        )

    request = httpx.Request("POST", "https://model.invalid/v1/chat/completions")
    response = httpx.Response(
        429,
        request=request,
        json={"error": {"message": "quota exceeded"}},
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        http_failure = zeta_model_sse.http_error_detail(error)
    else:
        raise AssertionError("expected an HTTP failure")

    responses_requests = []
    full_messages = [
        {"role": "system", "content": "Be exact."},
        {"role": "system", "content": "Use tools when needed."},
        {"role": "user", "content": "Read README.md."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-read",
                    "type": "function",
                    "function": {
                        "name": "read",
                        "arguments": '{"path":"README.md"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-read",
            "content": '{"ok":true}',
        },
        {"role": "assistant", "content": "The file is ready."},
    ]
    full_inputs: dict[str, Any] = {
        "messages": full_messages,
        "model": "unit-responses-model",
        "tools": [tool],
        "tool_choice": forced_read,
        "max_tokens": 512,
        "thinking": "minimal",
        "session_id": "session-vector",
    }
    responses_requests.append(
        {
            "name": "converted_history_and_tools",
            "input": full_inputs,
            "expected": zeta_responses.responses_request_body(**full_inputs),
        }
    )
    replay_messages = [
        {"role": "system", "content": "Replay provider state."},
        {"role": "user", "content": "Continue."},
        {
            "role": "assistant",
            "content": "ignored when replay items exist",
            "_responses_items": [
                {
                    "type": "reasoning",
                    "id": "rs-vector",
                    "encrypted_content": "opaque-vector",
                },
                {
                    "type": "function_call",
                    "id": "fc-vector",
                    "call_id": "call-vector",
                    "name": "read",
                    "arguments": '{"path":"notes.md"}',
                },
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-vector",
            "content": '{"ok":true,"value":"ready"}',
        },
    ]
    replay_inputs: dict[str, Any] = {
        "messages": replay_messages,
        "model": "unit-responses-model",
        "tools": None,
        "tool_choice": "auto",
        "max_tokens": 128,
        "thinking": "medium",
        "session_id": "session-vector",
    }
    responses_requests.append(
        {
            "name": "provider_replay_items",
            "input": replay_inputs,
            "expected": zeta_responses.responses_request_body(**replay_inputs),
        }
    )

    responses_events = [
        {"type": "response.created", "response": {"id": "resp-vector"}},
        {"type": "response.reasoning_summary_text.delta", "delta": "inspect"},
        {"type": "response.reasoning_summary_part.done"},
        {"type": "response.reasoning_text.delta", "delta": "choose"},
        {"type": "response.output_text.delta", "delta": "draft"},
        {
            "type": "response.output_item.done",
            "item": {
                "type": "reasoning",
                "id": "rs-vector",
                "encrypted_content": "opaque-vector",
            },
        },
        {
            "type": "response.output_item.done",
            "item": {
                "type": "message",
                "id": "msg-vector",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Final."}],
            },
        },
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "fc-vector",
                "call_id": "call-vector",
                "name": "read",
                "arguments": '{"path":"README.md"}',
            },
        },
        {
            "type": "response.completed",
            "response": {
                "id": "resp-vector",
                "status": "completed",
                "usage": {
                    "input_tokens": 34,
                    "output_tokens": 13,
                    "total_tokens": 47,
                    "input_tokens_details": {"cached_tokens": 21},
                },
            },
        },
    ]
    responses_frames = [
        *(compact_model_event(event) for event in responses_events),
        "[DONE]",
    ]
    responses_sink = DeltaSink()
    responses_expected = _read_stream(
        zeta_responses.read_streamed_responses,
        responses_frames,
        stream_sink=responses_sink,
    )
    incomplete_event = {
        "type": "response.incomplete",
        "response": {"id": "resp-short", "status": "incomplete"},
    }
    incomplete_frames = [compact_model_event(incomplete_event), "[DONE]"]

    responses_failures = []
    for name, frames in (
        (
            "error_event",
            [
                compact_model_event(
                    {
                        "type": "error",
                        "code": "server_error",
                        "message": "provider failed",
                    }
                )
            ],
        ),
        (
            "failed_response",
            [
                compact_model_event(
                    {
                        "type": "response.failed",
                        "response": {
                            "id": "resp-failed",
                            "status": "failed",
                            "error": {
                                "code": "invalid_request",
                                "message": "request rejected",
                            },
                        },
                    }
                )
            ],
        ),
        (
            "missing_terminal",
            [
                compact_model_event(
                    {"type": "response.output_text.delta", "delta": "partial"}
                ),
                "[DONE]",
            ],
        ),
    ):
        responses_failures.append(
            {
                "name": name,
                "sse": frames,
                "expected_error": captured_runtime_error(
                    lambda frames=frames: _read_stream(
                        zeta_responses.read_streamed_responses,
                        frames,
                    )
                ),
            }
        )

    timeout = zeta_model_limits.model_stream_timeout(
        first_output_timeout=10.0,
        idle_timeout=2.5,
    )
    headers = zeta_responses.codex_request_headers(
        zeta_codex_auth.CodexCredentials(
            access_token="<redacted-access-token>",
            account_id="<redacted-account-id>",
        ),
        "session-vector",
    )
    return {
        "version": 0,
        "chat_completions": {
            "requests": chat_requests,
            "sse_parser": {
                "lines": sse_lines_input,
                "expected_frames": sse_frames_expected,
            },
            "streams": [
                {
                    "name": "fragmented_content_reasoning_tool_and_usage",
                    "sse": chat_frames,
                    "expected": chat_expected,
                    "expected_observations": {
                        "content": chat_sink.deltas,
                        "reasoning": chat_sink.reasoning_deltas,
                    },
                }
            ],
            "failures": chat_failures,
            "http_failures": [
                {
                    "status": 429,
                    "url": str(request.url),
                    "body": {"error": {"message": "quota exceeded"}},
                    "expected": http_failure,
                }
            ],
            "timeouts": [
                {
                    "first_output_seconds": 10.0,
                    "idle_seconds": 2.5,
                    "expected": {
                        "connect": timeout.connect,
                        "read": timeout.read,
                        "write": timeout.write,
                        "pool": timeout.pool,
                    },
                }
            ],
        },
        "responses": {
            "requests": responses_requests,
            "streams": [
                {
                    "name": "reasoning_text_tool_usage_and_replay_items",
                    "sse": responses_frames,
                    "expected": responses_expected,
                    "expected_observations": {
                        "content": responses_sink.deltas,
                        "reasoning": responses_sink.reasoning_deltas,
                    },
                },
                {
                    "name": "incomplete_response",
                    "sse": incomplete_frames,
                    "expected": _read_stream(
                        zeta_responses.read_streamed_responses,
                        incomplete_frames,
                    ),
                },
            ],
            "failures": responses_failures,
            "codex_headers": {
                "credentials": {
                    "access_token": "<redacted-access-token>",
                    "account_id": "<redacted-account-id>",
                },
                "session": "session-vector",
                "expected": headers,
            },
        },
    }


def test_agent_model_vectors_match_python_ground_truth() -> None:
    expected = json.loads(
        (AGENT_VECTORS_DIR / "models.json").read_text(encoding="utf-8")
    )
    assert python_model_vectors() == expected
