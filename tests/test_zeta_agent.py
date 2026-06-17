"""Agent loop tests."""

from __future__ import annotations

import json
import threading
import tomllib
from collections.abc import Callable
from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest
from _zeta_helpers import (
    DeltaSink,
    assert_tool_result_derivation_graph,
    event_by_type,
    read_tool_call_response,
    read_tool_payload,
    required_stream_sink,
)
from click.testing import CliRunner

from sigil.agent_io import run_zeta_rpc_session
from sigil.cli import cli
from sigil.tools import ensure_builtin_tools_registered
from zeta import agent as zeta_agent
from zeta import cli as zeta_cli
from zeta import context as zeta_context
from zeta import events as zeta_events
from zeta import prompt as zeta_prompt
from zeta import rpc as zeta_rpc
from zeta import trace as zeta_trace
from zeta.models import chat_completions as zeta_model
from zeta.tools.base import ToolImpl, ToolSpec
from zeta.tools.registry import ToolRegistry

ensure_builtin_tools_registered()


def test_zeta_console_script_is_declared() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["scripts"]["zeta"] == "zeta.cli:main"


def test_zeta_agent_turn_carries_reasoning_into_event(monkeypatch) -> None:
    def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        **kwargs: object,
    ) -> dict[str, Any]:
        return {"content": "done", "reasoning_content": "weighing the options"}

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent, "chat_completion_messages", fake_chat_completion_messages
    )

    result = zeta_agent.run_agent_turn(
        "answer",
        [],
        zeta_agent.AgentConfig(allowed_tools=("read",), max_turns=1),
    )

    assert result.events[0]["reasoning"] == "weighing the options"
    assert result.events[0]["content"] == "done"


def test_zeta_tool_result_event_records_error_for_failed_content_result() -> None:
    event = zeta_agent.tool_result_event(
        "call-1",
        "grep",
        {
            "ok": False,
            "content": [
                {
                    "type": "text",
                    "text": "rg: missing: No such file or directory",
                }
            ],
            "metadata": {"status": 2},
        },
    )

    assert event["result"]["error"] == {
        "code": "grep-failed",
        "message": "rg: missing: No such file or directory",
    }
    assert event["result"]["content"][0]["text"].startswith("rg: missing")


def test_zeta_tool_result_event_records_bash_exception_summary() -> None:
    event = zeta_agent.tool_result_event(
        "call-1",
        "bash",
        {
            "ok": False,
            "content": [
                {
                    "type": "text",
                    "text": "$ run\nexit 1\nstderr:\nTraceback\nValueError: bad input",
                }
            ],
            "metadata": {"status": 1},
        },
    )

    assert event["result"]["error"] == {
        "code": "bash-failed",
        "message": "ValueError: bad input",
    }


def test_zeta_tool_result_event_preserves_explicit_error() -> None:
    event = zeta_agent.tool_result_event(
        "call-1",
        "read",
        {"ok": False, "error": {"code": "read-failed", "message": "missing"}},
    )

    assert event["result"]["error"] == {"code": "read-failed", "message": "missing"}


def rpc_messages(output: StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in output.getvalue().splitlines()]


def test_zeta_rpc_initialize_returns_server_metadata() -> None:
    input_stream = StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n"
    )
    output = StringIO()
    server = zeta_rpc.JsonRpcServer(input_stream, output)

    server.serve()

    assert rpc_messages(output) == [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"server": "zeta", "protocol": "0.1"},
        }
    ]


def test_zeta_rpc_unknown_method_returns_structured_error() -> None:
    input_stream = StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "missing.method"}) + "\n"
    )
    output = StringIO()
    server = zeta_rpc.JsonRpcServer(input_stream, output)

    server.serve()

    assert rpc_messages(output) == [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": -32601,
                "message": "Method not found",
                "data": {"code": "method_not_found", "method": "missing.method"},
            },
        }
    ]


def test_zeta_rpc_session_run_requires_objective() -> None:
    input_stream = StringIO(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session.run",
                "params": {"tools": []},
            }
        )
        + "\n"
    )
    output = StringIO()
    server = zeta_rpc.JsonRpcServer(
        input_stream,
        output,
        session_runner=lambda params: zeta_rpc.run_rpc_session(
            params,
            publish_event=lambda event: None,
        ),
    )

    server.serve()

    assert rpc_messages(output) == [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": -32602,
                "message": "Invalid params",
                "data": {
                    "code": "missing_objective",
                    "message": "session.run requires objective",
                },
            },
        }
    ]


def test_zeta_rpc_session_run_rejects_invalid_workflow() -> None:
    input_stream = StringIO(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session.run",
                "params": {"objective": "answer", "workflow": "ship"},
            }
        )
        + "\n"
    )
    output = StringIO()
    server = zeta_rpc.JsonRpcServer(
        input_stream,
        output,
        session_runner=lambda params: zeta_rpc.run_rpc_session(
            params,
            publish_event=lambda event: None,
        ),
    )

    server.serve()

    assert rpc_messages(output) == [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": -32602,
                "message": "Invalid params",
                "data": {
                    "code": "invalid_workflow",
                    "message": "workflow must be ask, propose, or do",
                    "workflow": "ship",
                },
            },
        }
    ]


def test_zeta_rpc_cli_serves_stdio_initialize() -> None:
    result = CliRunner().invoke(
        zeta_cli.cli,
        ["rpc", "--stdio"],
        input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n",
    )

    assert result.exit_code == 0
    assert [json.loads(line) for line in result.output.splitlines()] == [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"server": "zeta", "protocol": "0.1"},
        }
    ]


def test_sigil_zeta_rpc_cli_serves_stdio_initialize() -> None:
    result = CliRunner().invoke(
        cli,
        ["zeta", "rpc", "--stdio"],
        input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n",
    )

    assert result.exit_code == 0
    assert [json.loads(line) for line in result.output.splitlines()] == [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"server": "zeta", "protocol": "0.1"},
        }
    ]


def test_zeta_rpc_cli_runs_pure_session_without_sigil_turn(monkeypatch) -> None:
    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda *args: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        lambda *args, **kwargs: {"content": "done"},
    )

    result = CliRunner().invoke(
        zeta_cli.cli,
        ["rpc", "--stdio"],
        input=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session.run",
                "params": {"objective": "answer", "tools": [], "context": ""},
            }
        )
        + "\n",
    )

    assert result.exit_code == 0
    messages = [json.loads(line) for line in result.output.splitlines()]
    published = [
        message["params"]["event"]
        for message in messages
        if message.get("method") == "events.publish"
    ]
    assert [event["type"] for event in published] == ["user_message", "model"]
    assert messages[-1]["result"]["outcome"] == "answered"
    assert messages[-1]["result"]["final_text"] == "done"
    assert messages[-1]["result"]["run_id"].startswith("run_")
    assert {event["run_id"] for event in published} == {
        messages[-1]["result"]["run_id"]
    }


def test_zeta_rpc_session_uses_explicit_context(monkeypatch, tmp_path: Path) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    trace_store = zeta_trace.InMemoryStore()
    context = zeta_context.ZetaContext(
        session_id="ctx-session",
        event_sink=event_store,
        trace_store=trace_store,
        tool_registry=ToolRegistry(),
        state_dir=tmp_path,
        session_dir=tmp_path / "sessions" / "ctx-session",
    )
    published: list[dict[str, Any]] = []

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda *args: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        lambda *args, **kwargs: {"content": "done"},
    )

    result = zeta_rpc.run_rpc_session(
        {"objective": "answer", "tools": [], "context": ""},
        publish_event=published.append,
        runtime_context=context,
    )

    assert result["outcome"] == "answered"
    assert result["final_text"] == "done"
    assert result["run_id"].startswith("run_")
    assert result["final_event_cursor"] == "2"
    assert [event["session"] for event in published] == [
        "ctx-session",
        "ctx-session",
    ]
    assert [event["run_id"] for event in published] == [
        result["run_id"],
        result["run_id"],
    ]
    assert [event["cursor"] for event in published] == ["1", "2"]
    assert [event["turn_id"] for event in published] == [
        result["run_id"],
        result["run_id"],
    ]
    assert [
        event.event_type for event in event_store.list_events(zeta_events.Filter())
    ] == ["zeta.user_message", "zeta.model.called"]
    assert [
        event.turn_id
        for event in event_store.list_events(
            zeta_events.Filter(turn_id=result["run_id"])
        )
    ] == [result["run_id"], result["run_id"]]
    assert trace_store.objects(kind="run_event") == []
    assert "run/ctx-session/head" not in trace_store.refs()
    assert "run/ctx-session/event_head" not in trace_store.refs()


def test_zeta_rpc_sequential_runs_get_distinct_run_ids(
    monkeypatch,
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    context = zeta_context.ZetaContext(
        session_id="ctx-session",
        event_sink=event_store,
        trace_store=zeta_trace.InMemoryStore(),
        tool_registry=ToolRegistry(),
        state_dir=tmp_path,
        session_dir=tmp_path / "sessions" / "ctx-session",
    )

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda *args: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        lambda *args, **kwargs: {"content": "done"},
    )

    first = zeta_rpc.run_rpc_session(
        {"objective": "first", "tools": [], "context": ""},
        publish_event=lambda event: None,
        runtime_context=context,
    )
    second = zeta_rpc.run_rpc_session(
        {"objective": "second", "tools": [], "context": ""},
        publish_event=lambda event: None,
        runtime_context=context,
    )

    assert first["run_id"].startswith("run_")
    assert second["run_id"].startswith("run_")
    assert first["run_id"] != second["run_id"]
    assert first["final_event_cursor"] == "2"
    assert second["final_event_cursor"] == "4"


def test_zeta_rpc_session_returns_aborted_on_wall_clock_budget(
    monkeypatch,
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    context = zeta_context.ZetaContext(
        session_id="ctx-session",
        event_sink=event_store,
        trace_store=zeta_trace.InMemoryStore(),
        tool_registry=ToolRegistry(),
        state_dir=tmp_path,
        session_dir=tmp_path / "sessions" / "ctx-session",
    )
    published: list[dict[str, Any]] = []

    def fail_chat_completion_messages(*args: object, **kwargs: object) -> dict:
        raise AssertionError("expired turn must not request the model")

    monkeypatch.setattr(zeta_agent, "time_monotonic", lambda: 10.0)
    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda *args: True)
    monkeypatch.setattr(
        zeta_agent, "chat_completion_messages", fail_chat_completion_messages
    )

    result = zeta_rpc.run_rpc_session(
        {
            "objective": "answer",
            "tools": [],
            "context": "",
            "max_wall_seconds": 0.0,
        },
        publish_event=published.append,
        runtime_context=context,
    )

    assert result["outcome"] == "aborted"
    assert result["final_text"] == ""
    assert result["run_id"].startswith("run_")
    assert result["final_event_cursor"] == "2"
    assert [event["type"] for event in published] == [
        "user_message",
        "turn_aborted",
    ]
    assert [event["run_id"] for event in published] == [
        result["run_id"],
        result["run_id"],
    ]
    assert published[-1]["reason"] == "deadline_exceeded"
    assert [
        event.event_type for event in event_store.list_events(zeta_events.Filter())
    ] == ["zeta.user_message", "zeta.turn_aborted"]


def test_zeta_rpc_registers_client_tool_on_server_registry() -> None:
    registry = ToolRegistry()
    server = zeta_rpc.JsonRpcServer(StringIO(), StringIO(), tool_registry=registry)

    registered = server.register_client_tools(
        [
            {
                "name": "ctx_read",
                "description": "Read through the client.",
                "schema": {"type": "object"},
                "effects": ["read"],
            }
        ]
    )

    assert registered == ["ctx_read"]
    assert registry.get("ctx_read") is not None
    assert zeta_agent.tool_registry.get("ctx_read") is None


def test_zeta_rpc_rejects_duplicate_client_tool_registration() -> None:
    registry = ToolRegistry()
    registry.register(
        "ctx_read",
        ToolImpl(
            ToolSpec(
                "ctx_read",
                "Read through the host.",
                {"type": "object"},
                effects=("read",),
            ),
            lambda params: {"ok": True},
            lambda params: {"ok": True},
        ),
    )
    input_stream = StringIO(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools.register",
                "params": {
                    "tools": [
                        {
                            "name": "ctx_read",
                            "description": "Read through the client.",
                            "schema": {"type": "object"},
                            "effects": ["read"],
                        }
                    ]
                },
            }
        )
        + "\n"
    )
    output = StringIO()
    server = zeta_rpc.JsonRpcServer(
        input_stream,
        output,
        tool_registry=registry,
    )

    server.serve()

    assert rpc_messages(output) == [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": -32602,
                "message": "Invalid params",
                "data": {
                    "code": "duplicate_tool",
                    "message": "tool 'ctx_read' is already registered",
                    "tool": "ctx_read",
                },
            },
        }
    ]


def test_zeta_rpc_registers_client_tools_and_calls_client() -> None:
    input_stream = StringIO(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "tools.respond",
                "params": {
                    "id": "client-call-1",
                    "result": {"ok": True, "content": [{"type": "text", "text": "ok"}]},
                },
            }
        )
        + "\n"
    )
    output = StringIO()
    server = zeta_rpc.JsonRpcServer(input_stream, output)
    server.register_client_tools(
        [
            {
                "name": "client.echo",
                "description": "Echo from the client.",
                "schema": {"type": "object"},
                "effects": ["read"],
            }
        ]
    )

    result = server.call_client_tool(
        "client-call-1",
        "client.echo",
        {"text": "hello"},
    )

    assert result == {"ok": True, "content": [{"type": "text", "text": "ok"}]}
    assert rpc_messages(output) == [
        {
            "jsonrpc": "2.0",
            "method": "tools.call",
            "params": {
                "id": "client-call-1",
                "name": "client.echo",
                "arguments": {"text": "hello"},
            },
        }
    ]


def test_zeta_rpc_session_run_streams_events_and_returns_turn(
    monkeypatch,
) -> None:
    input_stream = StringIO(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session.run",
                "params": {"objective": "answer", "tools": []},
            }
        )
        + "\n"
    )
    output = StringIO()

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda *args: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        lambda *args, **kwargs: {"content": "done"},
    )

    server = zeta_rpc.JsonRpcServer(input_stream, output)
    server.session_runner = lambda params: run_zeta_rpc_session(
        params,
        publish_event=server.publish_event,
    )

    server.serve()

    messages = rpc_messages(output)
    published = [
        message["params"]["event"]
        for message in messages
        if message.get("method") == "events.publish"
    ]
    response = messages[-1]

    assert [event["type"] for event in published] == [
        "user_message",
        "model",
        "sigil.turn.completed",
    ]
    assert response["id"] == 1
    assert response["result"]["outcome"] == "answered"
    assert response["result"]["turn_id"]


def test_zeta_rpc_events_list_pages_in_append_order(tmp_path: Path) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    for content in ("one", "two", "three"):
        event_store.accept(
            zeta_events.durable_event_draft(
                "zeta.user_message",
                "zeta",
                payload={
                    "_timeline_type": "user_message",
                    "content": content,
                    "run_id": "run_1",
                },
                session_id="session-1",
                turn_id="run_1",
                caused_by=None,
                event_id=None,
                idempotency_key=None,
                timestamp_micros=None,
            )
        )
    input_stream = StringIO(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "events.list",
                "params": {"session_id": "session-1", "run_id": "run_1", "limit": 2},
            }
        )
        + "\n"
        + json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "events.list",
                "params": {
                    "session_id": "session-1",
                    "run_id": "run_1",
                    "after": "2",
                },
            }
        )
        + "\n"
    )
    output = StringIO()
    server = zeta_rpc.JsonRpcServer(input_stream, output, event_reader=event_store)

    server.serve()

    first, second = rpc_messages(output)
    assert [event["content"] for event in first["result"]["events"]] == ["one", "two"]
    assert [event["cursor"] for event in first["result"]["events"]] == ["1", "2"]
    assert first["result"]["next_cursor"] == "2"
    assert [event["content"] for event in second["result"]["events"]] == ["three"]
    assert second["result"]["next_cursor"] == "3"


def test_zeta_rpc_events_list_filters_by_session_and_run(tmp_path: Path) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    for session_id, run_id, content in (
        ("session-1", "run_1", "one"),
        ("session-2", "run_2", "two"),
        ("session-1", "run_1", "three"),
    ):
        event_store.accept(
            zeta_events.durable_event_draft(
                "zeta.user_message",
                "zeta",
                payload={
                    "_timeline_type": "user_message",
                    "content": content,
                    "run_id": run_id,
                },
                session_id=session_id,
                turn_id=run_id,
                caused_by=None,
                event_id=None,
                idempotency_key=None,
                timestamp_micros=None,
            )
        )
    input_stream = StringIO(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "events.list",
                "params": {"session_id": "session-1", "run_id": "run_1"},
            }
        )
        + "\n"
    )
    output = StringIO()
    server = zeta_rpc.JsonRpcServer(input_stream, output, event_reader=event_store)

    server.serve()

    assert [event["content"] for event in rpc_messages(output)[0]["result"]["events"]] == [
        "one",
        "three",
    ]


def test_zeta_agent_turn_uses_explicit_tool_registry(monkeypatch) -> None:
    registry = ToolRegistry()
    registry.register(
        "ctx_echo",
        ToolImpl(
            ToolSpec(
                "ctx_echo",
                "Echo text.",
                {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
                effects=("read",),
            ),
            lambda params: {
                "ok": True,
                "content": [{"type": "text", "text": str(params["text"])}],
            },
        ),
    )
    responses = iter(
        [
            {
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "ctx_echo",
                            "arguments": '{"text":"hello"}',
                        },
                    }
                ]
            },
            {"content": "done"},
        ]
    )

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        lambda *args, **kwargs: next(responses),
    )

    result = zeta_agent.run_agent_turn(
        "echo",
        [],
        zeta_agent.AgentConfig(allowed_tools=("ctx_echo",), max_turns=2),
        tool_registry=registry,
    )

    assert zeta_agent.tool_registry.get("ctx_echo") is None
    assert result.final_text == "done"
    assert [event.get("name") for event in result.events if "name" in event] == [
        "ctx_echo",
        "ctx_echo",
    ]


def test_zeta_agent_turn_passes_thinking_to_the_model(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        **kwargs: object,
    ) -> dict[str, Any]:
        captured["kwargs"] = kwargs
        return {"content": "done"}

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent, "chat_completion_messages", fake_chat_completion_messages
    )

    zeta_agent.run_agent_turn(
        "answer",
        [],
        zeta_agent.AgentConfig(allowed_tools=("read",), max_turns=1, thinking="none"),
    )

    kwargs = cast(dict[str, Any], captured["kwargs"])
    assert kwargs["thinking"] == "none"


def test_zeta_agent_event_omits_empty_reasoning() -> None:
    event = zeta_agent.model_event({"content": "done", "reasoning_content": ""})

    assert "reasoning" not in event


def test_zeta_agent_tool_call_is_caused_by_assistant_event(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "README.md"
    target.write_text("hello\n", encoding="utf-8")
    store = zeta_trace.InMemoryStore()

    def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        **kwargs: object,
    ) -> dict[str, Any]:
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "read",
                        "arguments": json.dumps({"path": str(target)}),
                    },
                }
            ],
        }

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent, "chat_completion_messages", fake_chat_completion_messages
    )

    result = zeta_agent.run_agent_turn(
        "read",
        [],
        zeta_agent.AgentConfig(allowed_tools=("read",), max_turns=1),
        prompt_builder=zeta_prompt.PromptBuilder(store=store),
        caused_by="prompt-event",
    )

    assistant = event_by_type(result.events, "model")
    tool_call = event_by_type(result.events, "tool_call")
    tool_result = event_by_type(result.events, "tool_result")
    assert assistant["id"]
    assert assistant["caused_by"] == "prompt-event"
    assert tool_call["caused_by"] == assistant["id"]
    assert tool_result["caused_by"] == assistant["id"]
    assert assistant["tool_call_object_ids"] == [tool_call["tool_call_object_id"]]


def test_zeta_agent_turn_finalizes_text(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    store = zeta_trace.InMemoryStore()

    def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        **kwargs: object,
    ) -> dict[str, Any]:
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return {"content": "done"}

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent, "chat_completion_messages", fake_chat_completion_messages
    )

    result = zeta_agent.run_agent_turn(
        "answer",
        [],
        zeta_agent.AgentConfig(allowed_tools=("read",), max_turns=1),
        trace_store=store,
    )

    assert result.final_text == "done"
    assert result.events[0]["type"] == "model"
    assert result.events[0]["content"] == "done"
    assert result.events[0]["prompt_trace"]["prompt_object_id"]
    assert len(result.prompt_traces) == 1
    kwargs = cast(dict[str, Any], captured["kwargs"])
    assert kwargs["tools"][0]["function"]["name"] == "read"


def test_zeta_agent_turn_stores_prompt_and_assistant_trace(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    store = zeta_trace.InMemoryStore()

    def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        **kwargs: object,
    ) -> dict[str, Any]:
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return {"content": "done"}

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent, "chat_completion_messages", fake_chat_completion_messages
    )

    result = zeta_agent.run_agent_turn(
        "answer",
        [{"role": "user", "content": "prior"}],
        zeta_agent.AgentConfig(
            allowed_tools=("read",),
            max_turns=1,
            model_name="unit-model",
        ),
        context="Project context",
        prompt_builder=zeta_prompt.PromptBuilder(store=store),
    )

    assert len(result.prompt_traces) == 1
    trace = result.prompt_traces[0]
    prompt = store.get_object(trace.prompt_object_id)
    assert prompt is not None
    kwargs = cast(dict[str, Any], captured["kwargs"])
    assert prompt.data["payload_sha256"] == zeta_prompt.builder.payload_sha256(
        zeta_model.chat_completion_request_body(
            cast(list[dict[str, Any]], captured["messages"]),
            tools=cast(list[dict[str, Any]], kwargs["tools"]),
            tool_choice=cast(str, kwargs["tool_choice"]),
            selected_model="unit-model",
        )
    )
    assistant = store.get_object(cast(str, trace.assistant_message_object_id))
    assert assistant is not None
    assert assistant.kind == "assistant_message"
    assert assistant.links == (trace.prompt_object_id,)
    assert assistant.data["message"] == {"content": "done"}
    assert result.events[0]["prompt_trace"]["assistant_message_object_id"] == (
        trace.assistant_message_object_id
    )


def test_zeta_agent_turn_captures_model_telemetry(monkeypatch) -> None:
    def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        **kwargs: object,
    ) -> dict[str, Any]:
        del messages
        telemetry_sink = cast(
            "Callable[[dict[str, Any]], None]", kwargs["telemetry_sink"]
        )
        telemetry_sink(
            {
                "usage": {
                    "prompt_tokens": 123,
                    "completion_tokens": 4,
                    "total_tokens": 127,
                },
                "model_context_tokens": 262_144,
            }
        )
        return {"content": "done"}

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent, "chat_completion_messages", fake_chat_completion_messages
    )

    result = zeta_agent.run_agent_turn(
        "answer",
        [],
        zeta_agent.AgentConfig(allowed_tools=("read",), max_turns=1),
    )

    assert result.final_text == "done"
    assert result.model_telemetry == {
        "usage": {
            "prompt_tokens": 123,
            "completion_tokens": 4,
            "total_tokens": 127,
        },
        "model_context_tokens": 262_144,
    }


def test_zeta_agent_turn_attaches_model_telemetry_to_first_tool_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    first = tmp_path / "README.md"
    second = tmp_path / "pyproject.toml"
    first.write_text("README\n", encoding="utf-8")
    second.write_text("[project]\n", encoding="utf-8")
    tool_telemetry = {
        "usage": {"prompt_tokens": 123, "completion_tokens": 8, "total_tokens": 131},
        "model_context_tokens": 262_144,
    }
    final_telemetry = {
        "usage": {"prompt_tokens": 456, "completion_tokens": 4, "total_tokens": 460},
        "model_context_tokens": 262_144,
    }
    responses = iter(
        [
            (
                tool_telemetry,
                {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "read",
                                "arguments": json.dumps({"path": str(first)}),
                            },
                        },
                        {
                            "id": "call-2",
                            "type": "function",
                            "function": {
                                "name": "read",
                                "arguments": json.dumps({"path": str(second)}),
                            },
                        },
                    ],
                },
            ),
            (final_telemetry, {"content": "done"}),
        ]
    )

    def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        **kwargs: object,
    ) -> dict[str, Any]:
        del messages
        telemetry, response = next(responses)
        telemetry_sink = cast(
            "Callable[[dict[str, Any]], None]", kwargs["telemetry_sink"]
        )
        telemetry_sink(telemetry)
        return response

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent, "chat_completion_messages", fake_chat_completion_messages
    )

    result = zeta_agent.run_agent_turn(
        "inspect",
        [],
        zeta_agent.AgentConfig(allowed_tools=("read",), max_turns=2),
    )

    tool_results = [
        event for event in result.events if event.get("type") == "tool_result"
    ]
    assert tool_results[0]["model_telemetry"] == tool_telemetry
    assert "model_telemetry" not in tool_results[1]
    assert result.model_telemetry == final_telemetry


def test_zeta_agent_turn_records_one_prompt_trace_per_model_request(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "README.md"
    target.write_text("README\n", encoding="utf-8")
    store = zeta_trace.InMemoryStore()
    responses = iter([read_tool_call_response(target), {"content": "done"}])

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        lambda messages, **kwargs: next(responses),
    )
    monkeypatch.setattr(
        zeta_agent,
        "run_tool",
        lambda name, params: read_tool_payload(target),
    )

    result = zeta_agent.run_agent_turn(
        "inspect",
        [],
        zeta_agent.AgentConfig(allowed_tools=("read",), max_turns=2),
        prompt_builder=zeta_prompt.PromptBuilder(store=store),
    )

    assert result.final_text == "done"
    assert len(result.prompt_traces) == 2
    assert result.prompt_traces[0].prompt_object_id != (
        result.prompt_traces[1].prompt_object_id
    )
    second_prompt = store.get_object(result.prompt_traces[1].prompt_object_id)
    assert second_prompt is not None
    second_messages = [
        obj.data["message"]
        for obj in (
            store.get_object(component_id) for component_id in second_prompt.links
        )
        if obj is not None and "message" in obj.data
    ]
    assert [message["role"] for message in second_messages][-2:] == [
        "assistant",
        "tool",
    ]


def test_zeta_agent_turn_records_tool_result_derivation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "README.md"
    target.write_text("README\n", encoding="utf-8")
    store = zeta_trace.InMemoryStore()
    responses = iter([read_tool_call_response(target), {"content": "done"}])

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        lambda messages, **kwargs: next(responses),
    )
    monkeypatch.setattr(
        zeta_agent,
        "run_tool",
        lambda name, params: read_tool_payload(target),
    )

    result = zeta_agent.run_agent_turn(
        "inspect",
        [],
        zeta_agent.AgentConfig(allowed_tools=("read",), max_turns=2),
        prompt_builder=zeta_prompt.PromptBuilder(store=store),
    )

    assert_tool_result_derivation_graph(
        store,
        result,
        event_by_type(result.events, "tool_call"),
        event_by_type(result.events, "tool_result"),
    )


def test_zeta_agent_turn_wraps_model_request_in_status(monkeypatch) -> None:
    events: list[str] = []

    class Status:
        def __enter__(self) -> object:
            events.append("start")
            return self

        def __exit__(self, *exc: object) -> bool:
            events.append("stop")
            return False

    def fake_chat_completion_messages(
        *args: object, **kwargs: object
    ) -> dict[str, Any]:
        del args, kwargs
        assert events == ["start"]
        return {"content": "done"}

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        fake_chat_completion_messages,
    )

    result = zeta_agent.run_agent_turn(
        "answer",
        [],
        zeta_agent.AgentConfig(),
        model_status=Status,
    )

    assert result.final_text == "done"
    assert events == ["start", "stop"]


def test_zeta_agent_turn_forwards_content_deltas_and_marks_final(monkeypatch) -> None:
    sink = DeltaSink()

    def fake_chat_completion_messages(
        *args: object,
        **kwargs: object,
    ) -> dict[str, Any]:
        del args
        stream_sink = required_stream_sink(kwargs)
        stream_sink.content_delta("hel")
        stream_sink.content_delta("lo")
        return {"content": "hello"}

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        fake_chat_completion_messages,
    )

    result = zeta_agent.run_agent_turn(
        "answer",
        [],
        zeta_agent.AgentConfig(max_turns=1),
        stream_sink=sink,
    )

    assert sink.deltas == ["hel", "lo"]
    assert result.final_text == "hello"
    assert result.final_text_streamed is True


def test_zeta_agent_reasoning_deltas_feed_status_without_closing_it(
    monkeypatch,
) -> None:
    events: list[str] = []

    class Status:
        def __enter__(self) -> object:
            events.append("start")
            return self

        def __exit__(self, *exc: object) -> bool:
            events.append("stop")
            return False

        def reasoning_delta(self, text: str) -> None:
            assert "stop" not in events
            events.append(f"reasoning:{text}")

    def fake_chat_completion_messages(
        *args: object,
        **kwargs: object,
    ) -> dict[str, Any]:
        del args
        stream_sink = required_stream_sink(kwargs)
        stream_sink.reasoning_delta("mull")
        stream_sink.content_delta("done")
        return {"content": "done"}

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        fake_chat_completion_messages,
    )

    result = zeta_agent.run_agent_turn(
        "answer",
        [],
        zeta_agent.AgentConfig(max_turns=1),
        stream_sink=DeltaSink(),
        model_status=Status,
    )

    assert result.final_text == "done"
    assert events == ["start", "reasoning:mull", "stop"]


def test_zeta_agent_reasoning_deltas_are_dropped_without_status(monkeypatch) -> None:
    # A bare sink (no status renderer) must not receive reasoning text in
    # its answer stream, and nothing may crash.
    sink = DeltaSink()

    def fake_chat_completion_messages(
        *args: object,
        **kwargs: object,
    ) -> dict[str, Any]:
        del args
        stream_sink = required_stream_sink(kwargs)
        stream_sink.reasoning_delta("mull")
        stream_sink.content_delta("done")
        return {"content": "done"}

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        fake_chat_completion_messages,
    )

    result = zeta_agent.run_agent_turn(
        "answer",
        [],
        zeta_agent.AgentConfig(max_turns=1),
        stream_sink=sink,
    )

    assert result.final_text == "done"
    assert sink.deltas == ["done"]
    assert sink.reasoning_deltas == []


def test_zeta_agent_turn_stops_status_before_first_stream_delta(monkeypatch) -> None:
    events: list[str] = []

    class Status:
        def __enter__(self) -> object:
            events.append("start")
            return self

        def __exit__(self, *exc: object) -> bool:
            events.append("stop")
            return False

    class AssertingSink:
        def content_delta(self, text: str) -> None:
            assert text == "done"
            assert events == ["start", "stop"]
            events.append("delta")

        def reasoning_delta(self, text: str) -> None:
            del text

    def fake_chat_completion_messages(
        *args: object,
        **kwargs: object,
    ) -> dict[str, Any]:
        del args
        stream_sink = required_stream_sink(kwargs)
        stream_sink.content_delta("done")
        return {"content": "done"}

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        fake_chat_completion_messages,
    )

    result = zeta_agent.run_agent_turn(
        "answer",
        [],
        zeta_agent.AgentConfig(max_turns=1),
        model_status=Status,
        stream_sink=AssertingSink(),
    )

    assert result.final_text == "done"
    assert events == ["start", "stop", "delta"]


def test_zeta_agent_turn_uses_request_model(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_model_endpoint_open(selected_url: str | None = None) -> bool:
        captured["endpoint_url"] = selected_url
        return True

    def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        **kwargs: object,
    ) -> dict[str, Any]:
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return {"content": "done"}

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", fake_model_endpoint_open)
    monkeypatch.setattr(
        zeta_agent, "chat_completion_messages", fake_chat_completion_messages
    )

    result = zeta_agent.run_agent_turn(
        "answer",
        [],
        zeta_agent.AgentConfig(
            allowed_tools=("read",),
            max_turns=1,
            model_name="fast-model",
            model_url="http://127.0.0.1:8081/v1/chat/completions",
        ),
    )

    assert result.final_text == "done"
    assert captured["endpoint_url"] == "http://127.0.0.1:8081/v1/chat/completions"
    kwargs = cast(dict[str, Any], captured["kwargs"])
    assert kwargs["selected_model"] == "fast-model"
    assert kwargs["selected_url"] == "http://127.0.0.1:8081/v1/chat/completions"


def test_zeta_agent_turn_runs_multiple_read_only_tools_in_order(monkeypatch) -> None:
    responses = iter(
        [
            {
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "read",
                            "arguments": '{"path":"README.md"}',
                        },
                    },
                    {
                        "id": "call-2",
                        "type": "function",
                        "function": {
                            "name": "ls",
                            "arguments": '{"path":"src"}',
                        },
                    },
                ]
            },
            {"content": "done"},
        ]
    )
    ran: list[tuple[str, dict[str, Any]]] = []

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        lambda *args, **kwargs: next(responses),
    )

    def fake_run_tool(
        name: str, params: dict[str, Any], **kwargs: object
    ) -> dict[str, Any]:
        ran.append((name, params))
        return {"ok": True, "content": [{"type": "text", "text": name}]}

    monkeypatch.setattr(zeta_agent, "run_tool", fake_run_tool)

    result = zeta_agent.run_agent_turn(
        "inspect",
        [],
        zeta_agent.AgentConfig(allowed_tools=("read", "ls"), max_turns=2),
        caused_by="prompt-event",
    )

    assert ran == [
        ("read", {"path": "README.md"}),
        ("ls", {"path": "src"}),
    ]
    assert result.final_text == "done"
    assert [
        event["name"] for event in result.events if event.get("type") == "tool_call"
    ] == ["read", "ls"]
    model_events = [event for event in result.events if event.get("type") == "model"]
    tool_results = [
        event for event in result.events if event.get("type") == "tool_result"
    ]
    assert model_events[0]["caused_by"] == "prompt-event"
    assert tool_results[0]["caused_by"] == model_events[0]["id"]
    assert tool_results[1]["caused_by"] == model_events[0]["id"]
    assert model_events[1]["caused_by"] == tool_results[1]["id"]


def test_zeta_agent_turn_streams_text_between_tool_turns(monkeypatch) -> None:
    sink = DeltaSink()
    responses = iter(
        [
            {
                "content": "I'll inspect README.",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "read",
                            "arguments": '{"path":"README.md"}',
                        },
                    }
                ],
            },
            {"content": "It is a README."},
        ]
    )

    def fake_chat_completion_messages(
        *args: object,
        **kwargs: object,
    ) -> dict[str, Any]:
        del args
        response = next(responses)
        stream_sink = kwargs.get("stream_sink")
        if response.get("content") and stream_sink is not None:
            stream_sink = cast(zeta_model.ChatCompletionStreamSink, stream_sink)
            stream_sink.content_delta(str(response["content"]))
        return response

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        fake_chat_completion_messages,
    )
    monkeypatch.setattr(
        zeta_agent,
        "run_tool",
        lambda name, params: {
            "ok": True,
            "content": [{"type": "text", "text": "README"}],
        },
    )

    result = zeta_agent.run_agent_turn(
        "inspect",
        [],
        zeta_agent.AgentConfig(allowed_tools=("read",), max_turns=2),
        stream_sink=sink,
    )

    assert sink.deltas == ["I'll inspect README.", "It is a README."]
    assert result.final_text == "It is a README."
    assert result.final_text_streamed is True
    assert result.events[0]["content"] == "I'll inspect README."


def test_zeta_agent_turn_does_not_duplicate_current_objective(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        **kwargs: object,
    ) -> dict[str, Any]:
        del kwargs
        captured["messages"] = messages
        return {"content": "done"}

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        fake_chat_completion_messages,
    )

    result = zeta_agent.run_agent_turn(
        "inspect the repo",
        [],
        zeta_agent.AgentConfig(allowed_tools=("read",), max_turns=1),
    )

    assert result.final_text == "done"
    messages = cast(list[dict[str, Any]], captured["messages"])
    prompt_messages = [
        message
        for message in messages
        if message.get("role") == "user"
        and "inspect the repo\n\ncwd:" in str(message.get("content"))
    ]
    assert len(prompt_messages) == 1


def test_zeta_agent_turn_orders_prior_timeline_before_current_events(
    monkeypatch,
) -> None:
    captured: list[list[dict[str, Any]]] = []
    responses = iter(
        [
            {
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "read",
                            "arguments": '{"path":"DECISIONS.md"}',
                        },
                    }
                ]
            },
            {"content": "Improve the decision log."},
        ]
    )

    def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        **kwargs: object,
    ) -> dict[str, Any]:
        del kwargs
        captured.append(messages)
        return next(responses)

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        fake_chat_completion_messages,
    )
    monkeypatch.setattr(
        zeta_agent,
        "run_tool",
        lambda name, params: {
            "ok": True,
            "content": [{"type": "text", "text": "Decision log"}],
            "metadata": {"path": "DECISIONS.md"},
        },
    )

    result = zeta_agent.run_agent_turn(
        "How would you improve it?",
        [
            {"role": "user", "content": "What is this vault about?"},
            {"role": "assistant", "content": "It is a CEO vault."},
        ],
        zeta_agent.AgentConfig(allowed_tools=("read",), max_turns=2),
    )

    assert result.final_text == "Improve the decision log."
    second_turn = captured[1]
    assert [message["role"] for message in second_turn] == [
        "system",
        "user",
        "assistant",
        "user",
        "assistant",
        "tool",
    ]
    assert second_turn[1]["content"] == "What is this vault about?"
    assert second_turn[2]["content"] == "It is a CEO vault."
    assert "How would you improve it?\n\ncwd:" in second_turn[3]["content"]
    assert second_turn[4]["tool_calls"][0]["id"] == "call-1"
    assert second_turn[5]["tool_call_id"] == "call-1"


def test_zeta_agent_turn_streams_tool_call_before_running_tool(monkeypatch) -> None:
    streamed: list[dict[str, Any]] = []

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        lambda *args, **kwargs: {
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "read",
                        "arguments": '{"path":"README.md"}',
                    },
                }
            ]
        },
    )

    def fake_run_tool(
        name: str, params: dict[str, Any], **kwargs: object
    ) -> dict[str, Any]:
        del name, params, kwargs
        assert [event.get("type") for event in streamed] == [
            "model",
            "tool_call",
        ]
        return {"ok": True, "content": [{"type": "text", "text": "README"}]}

    monkeypatch.setattr(zeta_agent, "run_tool", fake_run_tool)

    result = zeta_agent.run_agent_turn(
        "inspect",
        [],
        zeta_agent.AgentConfig(allowed_tools=("read",), max_turns=1),
        event_sink=streamed.append,
    )

    assert result.events == streamed
    assert [event.get("type") for event in streamed] == [
        "model",
        "tool_call",
        "tool_result",
    ]


def test_zeta_agent_turn_stops_after_staged_tool(monkeypatch) -> None:
    requests = 0

    def fake_chat_completion_messages(
        *args: object, **kwargs: object
    ) -> dict[str, Any]:
        nonlocal requests
        requests += 1
        return {
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "bash",
                        "arguments": '{"command":"uv run pytest"}',
                    },
                }
            ]
        }

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent, "chat_completion_messages", fake_chat_completion_messages
    )
    monkeypatch.setattr(
        zeta_agent,
        "run_tool",
        lambda name, params, **kwargs: {
            "ok": True,
            "effect": {
                "kind": "command",
                "status": "proposed",
                "command": "uv run pytest",
                "reason": "Run tests.",
            },
        },
    )

    result = zeta_agent.run_agent_turn(
        "test",
        [],
        zeta_agent.AgentConfig(allowed_tools=("bash",), max_turns=3),
    )

    assert requests == 1
    assert result.staged_effect == {
        "kind": "command",
        "status": "proposed",
        "command": "uv run pytest",
        "reason": "Run tests.",
    }


def test_zeta_agent_direct_mode_continues_after_bash(monkeypatch) -> None:
    requests = 0
    responses = iter(
        [
            {
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "bash",
                            "arguments": '{"command":"printf direct-bash"}',
                        },
                    }
                ]
            },
            {"content": "done"},
        ]
    )

    def fake_chat_completion_messages(
        *args: object, **kwargs: object
    ) -> dict[str, Any]:
        nonlocal requests
        requests += 1
        return next(responses)

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent, "chat_completion_messages", fake_chat_completion_messages
    )

    result = zeta_agent.run_agent_turn(
        "test",
        [],
        zeta_agent.AgentConfig(
            allowed_tools=("bash",),
            execution_mode="direct",
            max_turns=3,
        ),
    )

    assert requests == 2
    assert result.staged_effect is None
    assert result.final_text == "done"
    tool_result = next(
        event for event in result.events if event.get("type") == "tool_result"
    )
    assert "direct-bash" in tool_result["result"]["content"][0]["text"]


def test_zeta_agent_turn_stops_after_default_max_turns(monkeypatch) -> None:
    requests = 0

    def fake_chat_completion_messages(*args: object, **kwargs: object) -> dict:
        del args, kwargs
        nonlocal requests
        requests += 1
        return {
            "tool_calls": [
                {
                    "id": f"call-{requests}",
                    "type": "function",
                    "function": {"name": "ls", "arguments": '{"path":"."}'},
                }
            ]
        }

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent, "chat_completion_messages", fake_chat_completion_messages
    )
    monkeypatch.setattr(
        zeta_agent, "run_tool", lambda name, params, **kwargs: {"ok": True}
    )

    result = zeta_agent.run_agent_turn(
        "test",
        [],
        zeta_agent.AgentConfig(allowed_tools=("ls",)),
    )

    assert requests == zeta_agent.DEFAULT_MAX_TURNS
    assert result.final_text == ""


def test_zeta_agent_turn_aborts_before_model_when_cancelled(monkeypatch) -> None:
    cancellation = threading.Event()
    cancellation.set()
    events: list[dict[str, Any]] = []

    def fail_chat_completion_messages(*args: object, **kwargs: object) -> dict:
        raise AssertionError("cancelled turn must not request the model")

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent, "chat_completion_messages", fail_chat_completion_messages
    )

    with pytest.raises(zeta_agent.AgentTurnAborted) as raised:
        zeta_agent.run_agent_turn(
            "test",
            [],
            zeta_agent.AgentConfig(allowed_tools=("ls",), max_turns=1),
            event_sink=events.append,
            cancellation_event=cancellation,
            caused_by="prompt-event",
        )

    assert raised.value.reason == "cancelled"
    assert raised.value.result.events == events
    assert events == [
        {
            "type": "turn_aborted",
            "id": events[0]["id"],
            "reason": "cancelled",
            "content": "(turn aborted: cancelled)",
            "caused_by": "prompt-event",
        }
    ]


def test_zeta_agent_turn_aborts_on_deadline_between_model_turns(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "README.md"
    target.write_text("README\n", encoding="utf-8")
    responses = iter([read_tool_call_response(target), {"content": "too late"}])
    events: list[dict[str, Any]] = []
    monotonic = iter([0.0, 0.0, 0.0, 2.0])

    monkeypatch.setattr(zeta_agent, "time_monotonic", lambda: next(monotonic))
    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        lambda *args, **kwargs: next(responses),
    )
    monkeypatch.setattr(
        zeta_agent,
        "run_tool",
        lambda name, params, **kwargs: read_tool_payload(target),
    )

    with pytest.raises(zeta_agent.AgentTurnAborted) as raised:
        zeta_agent.run_agent_turn(
            "test",
            [],
            zeta_agent.AgentConfig(
                allowed_tools=("read",),
                max_turns=2,
                max_wall_seconds=1.0,
            ),
            event_sink=events.append,
        )

    assert raised.value.reason == "deadline_exceeded"
    assert [event["type"] for event in events] == [
        "model",
        "tool_call",
        "tool_result",
        "turn_aborted",
    ]
    assert events[-1]["reason"] == "deadline_exceeded"
    assert events[-1]["caused_by"] == events[-2]["id"]


def test_zeta_agent_turn_converts_tool_crash_to_error_result(monkeypatch) -> None:
    responses = iter(
        [
            {
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "read",
                            "arguments": '{"path":"x"}',
                        },
                    }
                ]
            },
            {"content": "recovered"},
        ]
    )

    def crash_run_tool(name: str, params: dict[str, Any], **kwargs: object) -> dict:
        raise ValueError("boom")

    def fake_chat_completion_messages(*args: object, **kwargs: object) -> dict:
        del args, kwargs
        return next(responses)

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent, "chat_completion_messages", fake_chat_completion_messages
    )
    monkeypatch.setattr(zeta_agent, "run_tool", crash_run_tool)

    result = zeta_agent.run_agent_turn(
        "test",
        [],
        zeta_agent.AgentConfig(allowed_tools=("read",), max_turns=3),
    )

    assert result.final_text == "recovered"
    tool_result = next(
        event for event in result.events if event.get("type") == "tool_result"
    )
    assert tool_result["result"]["ok"] is False
    assert tool_result["result"]["error"]["code"] == "tool-crashed"
    assert "boom" in tool_result["result"]["error"]["message"]


def test_zeta_agent_turn_rejects_schema_mismatch_before_running(monkeypatch) -> None:
    ran = False

    def fail_run_tool(name: str, params: dict[str, Any]) -> dict[str, Any]:
        nonlocal ran
        ran = True
        return {"ok": True}

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        lambda *args, **kwargs: {
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "read",
                        "arguments": '{"path":"README.md","unexpected":true}',
                    },
                }
            ]
        },
    )
    monkeypatch.setattr(zeta_agent, "run_tool", fail_run_tool)

    result = zeta_agent.run_agent_turn(
        "inspect",
        [],
        zeta_agent.AgentConfig(allowed_tools=("read",), max_turns=1),
    )

    assert ran is False
    tool_result = next(
        event for event in result.events if event.get("type") == "tool_result"
    )
    assert tool_result["result"]["ok"] is False
    assert tool_result["result"]["error"]["code"] == "schema-mismatch"


def test_zeta_agent_turn_rejects_disallowed_tool_before_running(monkeypatch) -> None:
    ran = False

    def fail_run_tool(name: str, params: dict[str, Any]) -> dict[str, Any]:
        nonlocal ran
        ran = True
        return {"ok": True}

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        lambda *args, **kwargs: {
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "bash",
                        "arguments": '{"command":"uv run pytest"}',
                    },
                }
            ]
        },
    )
    monkeypatch.setattr(zeta_agent, "run_tool", fail_run_tool)

    result = zeta_agent.run_agent_turn(
        "inspect",
        [],
        zeta_agent.AgentConfig(allowed_tools=("read",), max_turns=1),
    )

    assert ran is False
    tool_result = next(
        event for event in result.events if event.get("type") == "tool_result"
    )
    assert tool_result["result"]["ok"] is False
    assert tool_result["result"]["error"]["code"] == "disallowed-tool"


def test_zeta_agent_direct_mode_continues_after_edit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "a.txt"
    target.write_text("old\n", encoding="utf-8")
    requests = 0

    responses = iter(
        [
            {
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "edit",
                            "arguments": json.dumps(
                                {
                                    "location": str(target),
                                    "old": "old\n",
                                    "new": "new\n",
                                }
                            ),
                        },
                    }
                ]
            },
            {"content": "done"},
        ]
    )

    def fake_chat_completion_messages(
        *args: object,
        **kwargs: object,
    ) -> dict[str, Any]:
        nonlocal requests
        requests += 1
        return next(responses)

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        fake_chat_completion_messages,
    )

    result = zeta_agent.run_agent_turn(
        "edit",
        [],
        zeta_agent.AgentConfig(
            allowed_tools=("edit",),
            execution_mode="direct",
            max_turns=3,
        ),
    )

    assert requests == 2
    assert result.final_text == "done"
    assert target.read_text(encoding="utf-8") == "new\n"


def test_zeta_agent_codex_api_skips_endpoint_probe(monkeypatch) -> None:
    def fail_probe(url: str | None = None) -> bool:
        raise AssertionError("codex profiles must not probe a local endpoint")

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", fail_probe)

    config = zeta_agent.AgentConfig(model_api="codex-responses")

    assert zeta_agent.agent_model_endpoint_open(config) is True


def test_zeta_agent_turn_passes_api_to_the_model(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        **kwargs: object,
    ) -> dict[str, Any]:
        captured.update(kwargs)
        return {"content": "done"}

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent, "chat_completion_messages", fake_chat_completion_messages
    )

    zeta_agent.run_agent_turn(
        "answer",
        [],
        zeta_agent.AgentConfig(allowed_tools=("read",), max_turns=1),
    )

    assert captured["api"] is None
