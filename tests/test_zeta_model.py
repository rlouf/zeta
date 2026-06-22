"""Model request, streaming, and model-selection tests."""

import json
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from _zeta_helpers import (
    DeltaSink,
    sse_lines,
    task_state_fixture,
    write_models_config,
)
from click.testing import CliRunner

from sigil.cli import cli as sigil_cli
from sigil.sessions import session_dir
from zeta.context.compaction import TASK_STATE_SCHEMA
from zeta.kernel import models as zeta_models_api
from zeta.models import chat_completions as zeta_model
from zeta.models import profiles as zeta_models


def test_zeta_model_config_ignores_model_env_vars(monkeypatch) -> None:
    monkeypatch.delenv("ZETA_MODEL_IDLE_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setenv("ZETA_MODEL_URL", "http://zeta.invalid/v1/chat/completions")
    monkeypatch.setenv("ZETA_MODEL_NAME", "zeta-model")

    assert zeta_model.model_url() == zeta_models.DEFAULT_MODEL_URL
    assert zeta_model.model_name() == zeta_models.DEFAULT_MODEL_NAME
    assert (
        zeta_model.model_idle_timeout() == zeta_model.DEFAULT_MODEL_IDLE_TIMEOUT_SECONDS
    )
    assert zeta_model.DEFAULT_MODEL_IDLE_TIMEOUT_SECONDS == 120.0

    monkeypatch.setenv("ZETA_MODEL_IDLE_TIMEOUT_SECONDS", "2.5")
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
    payload = zeta_model.read_streamed_chat_completion(
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
        )
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

    def fake_stream_json_sse(
        url: str,
        body: dict[str, Any],
        *,
        headers: dict[str, str],
    ) -> list[str]:
        captured["url"] = url
        captured["body"] = body
        captured["accept"] = headers["Accept"]
        return events

    monkeypatch.setattr(zeta_model, "stream_json_sse", fake_stream_json_sse)
    body = {"model": "local-model", "messages": []}

    payload = zeta_model.request_chat_completion(body)

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
    timeout = zeta_model.model_stream_timeout(
        first_output_timeout=10.0,
        idle_timeout=2.5,
    )

    assert timeout.connect == 10.0
    assert timeout.write == 10.0
    assert timeout.pool == 10.0
    assert timeout.read == 2.5


def test_zeta_model_stream_timeout_can_disable_all_bounds() -> None:
    timeout = zeta_model.model_stream_timeout(
        first_output_timeout=None,
        idle_timeout=None,
    )

    assert timeout.connect is None
    assert timeout.write is None
    assert timeout.pool is None
    assert timeout.read is None


def test_zeta_stream_forwards_reasoning_deltas_to_sink() -> None:
    sink = DeltaSink()

    payload = zeta_model.read_streamed_chat_completion(
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
    closed = False

    def fake_stream_json_sse(
        url: str,
        body: dict[str, Any],
        *,
        headers: dict[str, str],
    ) -> Any:
        del url, body, headers
        nonlocal closed
        try:
            yield from sse_lines({"error": {"message": "generation failed"}})
        finally:
            closed = True

    monkeypatch.setattr(zeta_model, "stream_json_sse", fake_stream_json_sse)

    with pytest.raises(RuntimeError, match="generation failed"):
        zeta_model.request_chat_completion({"model": "local-model", "messages": []})

    assert closed is True


def test_zeta_stream_rejects_malformed_events() -> None:
    with pytest.raises(RuntimeError, match="invalid JSON event"):
        zeta_model.read_streamed_chat_completion(["nope"])


def test_zeta_http_error_detail_surfaces_json_error_body() -> None:
    body = json.dumps(
        {"error": {"code": 500, "message": "Failed to parse tool call arguments"}}
    )
    request = httpx.Request("POST", "http://127.0.0.1:8080/v1/chat/completions")
    response = httpx.Response(500, content=body, request=request)
    error = httpx.HTTPStatusError("boom", request=request, response=response)

    message = zeta_model.http_error_detail(error)
    assert "boom" in message
    assert "Failed to parse tool call arguments" in message


def test_zeta_http_error_detail_surfaces_plain_error_body() -> None:
    request = httpx.Request("POST", "http://127.0.0.1:8080/v1/chat/completions")
    response = httpx.Response(502, content=b"upstream exploded", request=request)
    error = httpx.HTTPStatusError("boom", request=request, response=response)

    message = zeta_model.http_error_detail(error)
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
    assert zeta_models.active_model_profile(session_dir=session_dir()) == "fast"

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
    assert zeta_models.active_model_profile(session_dir=session_dir()) is None


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
    monkeypatch.setenv("SIGIL_SESSION_ID", "resolution-session")
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
        profile="default",
        model=zeta_models.DEFAULT_MODEL_NAME,
        url=zeta_models.DEFAULT_MODEL_URL,
    )


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
    monkeypatch.setenv("SIGIL_SESSION_ID", "default-profile-session")

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
    monkeypatch.setenv("SIGIL_SESSION_ID", "selection-beats-default")
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
    monkeypatch.setenv("SIGIL_SESSION_ID", "stale-session")
    zeta_models.set_active_model_profile("gone")

    resolution = zeta_models.resolve_active_model()

    assert resolution.source == "builtin"
    assert resolution.selection.profile == "default"
    assert resolution.stale_profile == "gone"


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
    assert zeta_models.active_model_profile(session_dir=session_dir()) is None


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


def test_zeta_model_context_tokens_reads_model_context_length(
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

    monkeypatch.setattr(zeta_model, "request_model_metadata", fake_metadata)

    tokens = zeta_model.model_context_tokens(
        "http://127.0.0.1:8000/v1/chat/completions",
        "deepseek-v4-flash",
    )

    assert tokens == 100_000


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


def test_zeta_stream_json_sse_accepts_missing_content_type(monkeypatch) -> None:
    class FakeStreamResponse:
        def __enter__(self) -> "FakeStreamResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        def iter_lines(self) -> list[str]:
            return [
                "event: response.output_text.delta",
                'data: {"type":"response.output_text.delta","delta":"ok"}',
                "",
                "data: [DONE]",
                "",
            ]

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def stream(self, *args: object, **kwargs: object) -> FakeStreamResponse:
            return FakeStreamResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)

    events = list(
        zeta_model.stream_json_sse(
            "https://chatgpt.com/backend-api/codex/responses",
            {"model": "gpt-5.5"},
            headers={"Accept": "text/event-stream"},
        )
    )

    assert events == [
        '{"type":"response.output_text.delta","delta":"ok"}',
        "[DONE]",
    ]


def test_zeta_chat_completion_messages_accepts_request_model(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_request(
        body: dict[str, Any],
        *,
        selected_url: str | None = None,
        **kwargs: Any,
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

    monkeypatch.setattr(zeta_model, "request_chat_completion", fake_request)
    monkeypatch.setattr(
        zeta_model,
        "model_output_from_chat_completion",
        fake_model_output,
    )

    message = zeta_model.chat_completion_messages([{"role": "user", "content": "hi"}])

    assert message == {"role": "assistant", "content": "converted"}
    assert converted == [payload]


def test_zeta_chat_completion_messages_sends_native_tools(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_request(body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
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
        schema=TASK_STATE_SCHEMA,
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
    assert body["response_format"]["json_schema"]["schema"] == TASK_STATE_SCHEMA
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
            schema=TASK_STATE_SCHEMA,
            response_name="zeta_task_state",
        )


def test_zeta_chat_completion_messages_reports_model_telemetry(
    monkeypatch,
) -> None:
    telemetry: list[dict[str, Any]] = []

    def fake_request(body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
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

    def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        **options: Any,
    ) -> dict[str, Any]:
        captured["messages"] = messages
        captured["options"] = options
        return {"role": "assistant", "content": "ok"}

    monkeypatch.setattr(
        zeta_model, "chat_completion_messages", fake_chat_completion_messages
    )

    message = models_pkg.chat_completion_messages(
        [{"role": "user", "content": "hi"}],
        thinking="low",
    )

    assert message == {"role": "assistant", "content": "ok"}
    assert captured["messages"] == [{"role": "user", "content": "hi"}]
    assert captured["options"] == {"thinking": "low"}


def test_zeta_models_package_routes_codex_api_to_responses(monkeypatch) -> None:
    from zeta import models as models_pkg
    from zeta.models import responses as zeta_responses

    captured: dict[str, Any] = {}

    def fake_completion(messages: list[dict[str, Any]], **options: Any) -> dict:
        captured["completion"] = (messages, options)
        return {"role": "assistant", "content": "ok"}

    def fake_structured(messages: list[dict[str, Any]], **options: Any) -> dict:
        captured["structured"] = (messages, options)
        return {"state": "done"}

    monkeypatch.setattr(zeta_responses, "codex_completion_messages", fake_completion)
    monkeypatch.setattr(zeta_responses, "codex_structured_output", fake_structured)

    message = models_pkg.chat_completion_messages(
        [{"role": "user", "content": "hi"}], api="codex-responses", thinking="low"
    )
    data = models_pkg.chat_structured_output(
        [{"role": "user", "content": "hi"}],
        schema={"type": "object"},
        response_name="state",
        api="codex-responses",
    )

    assert message == {"role": "assistant", "content": "ok"}
    assert data == {"state": "done"}
    assert captured["completion"][1] == {"thinking": "low"}
    assert captured["structured"][1]["response_name"] == "state"


def test_zeta_models_package_rejects_unknown_api() -> None:
    from zeta import models as models_pkg

    with pytest.raises(ValueError, match="grpc"):
        models_pkg.chat_completion_messages(
            [{"role": "user", "content": "hi"}], api="grpc"
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

    result = CliRunner().invoke(sigil_cli, ["model", "list"])

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
    monkeypatch.setenv("SIGIL_SESSION_ID", "list-active-session")
    zeta_models.set_active_model_profile("fast", session_dir=session_dir())

    result = CliRunner().invoke(sigil_cli, ["model", "list"])

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
    monkeypatch.setenv("SIGIL_SESSION_ID", "show-source-session")

    result = CliRunner().invoke(sigil_cli, ["model", "show"])

    assert result.exit_code == 0, result.output
    assert (
        "model: codex -> gpt-5.5 @ https://chatgpt.com/backend-api (config)"
        in result.output
    )
