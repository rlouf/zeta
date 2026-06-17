"""Agent loop tests."""

from __future__ import annotations

import json
import threading
import time
import tomllib
from collections.abc import Callable, Iterable
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
from zeta import models as zeta_models_api
from zeta import prompt as zeta_prompt
from zeta import rpc as zeta_rpc
from zeta import trace as zeta_trace
from zeta.models import chat_completions as zeta_model
from zeta.tools.base import (
    Capability,
    CapabilityId,
    CapabilityPolicy,
    CapabilitySpec,
    EffectKind,
    InProcessCapabilityExecutor,
    TrustLevel,
)
from zeta.tools.registry import CapabilityRegistry

ensure_builtin_tools_registered()


def _test_capability(
    name: str,
    *,
    provider: str = "test",
    schema: dict[str, Any] | None = None,
    effects: tuple[EffectKind, ...] = ("read",),
    aliases: tuple[str, ...] | None = None,
    run_result: dict[str, Any] | None = None,
    supports_staging: bool = False,
    supports_direct: bool = True,
    trust: TrustLevel = "host",
) -> Capability:
    return Capability(
        CapabilitySpec(
            CapabilityId(provider, name),
            f"{name} test capability.",
            schema or {"type": "object"},
            effects=effects,
            aliases=aliases or (name,),
        ),
        CapabilityPolicy(
            supports_staging=supports_staging,
            supports_direct=supports_direct,
            trust=trust,
        ),
        InProcessCapabilityExecutor(
            lambda params: (
                run_result or {"ok": True, "content": [{"type": "text", "text": "ok"}]}
            ),
            (lambda params: {"ok": True, "effect": {"status": "proposed"}})
            if supports_staging
            else None,
        ),
    )


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
        zeta_agent.AgentConfig(allowed_capabilities=("read",), max_turns=1),
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


def test_zeta_model_tool_call_round_trips_provider_payload_to_event() -> None:
    record = zeta_agent.ModelToolCall.from_provider(
        {
            "id": "call-1",
            "type": "function",
            "function": {
                "name": "read",
                "arguments": '{"path": "README.md"}',
            },
        },
        index=0,
    )

    assert record is not None
    assert record == zeta_agent.ModelToolCall(
        call_id="call-1",
        name="read",
        raw_arguments='{"path": "README.md"}',
        params={"path": "README.md"},
    )
    assert record.event(caused_by="assistant-1") == {
        "type": "tool_call",
        "id": "call-1",
        "tool_call_id": "call-1",
        "status": "pending",
        "name": "read",
        "input": {"path": "README.md"},
        "arguments": '{"path": "README.md"}',
        "caused_by": "assistant-1",
    }


def test_zeta_model_tool_call_rejects_missing_function_payload() -> None:
    assert zeta_agent.ModelToolCall.from_provider({"id": "call-1"}, index=0) is None
    assert (
        zeta_agent.model_tool_call_event(
            {"id": "call-1"},
            index=0,
            caused_by="assistant-1",
        )
        == {}
    )


def test_zeta_model_tool_call_preserves_invalid_json_error() -> None:
    tool_call = {
        "id": "call-1",
        "type": "function",
        "function": {"name": "read", "arguments": '{"path":'},
    }

    record = zeta_agent.ModelToolCall.from_provider(tool_call, index=0)
    invocation = zeta_agent.tool_call_invocation(
        tool_call,
        index=0,
        caused_by="assistant-1",
    )

    assert record is not None
    assert invocation is not None
    assert record.parse_error == "Expecting value: line 1 column 9 (char 8)"
    assert invocation.parse_error == record.parse_error
    assert invocation.call_event == record.event(caused_by="assistant-1")


def test_zeta_model_runtime_event_round_trips_to_current_dict_shape() -> None:
    record = zeta_agent.ModelRuntimeEvent.from_assistant(
        {
            "content": "done",
            "reasoning_content": "thinking",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "read", "arguments": "{}"},
                }
            ],
        }
    )

    assert record.to_event() == {
        "type": "model",
        "reasoning": "thinking",
        "content": "done",
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "read", "arguments": "{}"},
            }
        ],
    }


def test_zeta_tool_call_runtime_event_round_trips_to_current_dict_shape() -> None:
    model_tool_call = zeta_agent.ModelToolCall(
        call_id="call-1",
        name="read",
        raw_arguments="{}",
        params={},
    )

    event = zeta_agent.ToolCallRuntimeEvent(
        tool_call=model_tool_call,
        caused_by="assistant-1",
    )

    assert event.to_event() == {
        "type": "tool_call",
        "id": "call-1",
        "tool_call_id": "call-1",
        "status": "pending",
        "name": "read",
        "input": {},
        "arguments": "{}",
        "caused_by": "assistant-1",
    }


def test_zeta_tool_result_runtime_event_round_trips_to_current_dict_shape() -> None:
    event = zeta_agent.ToolResultRuntimeEvent(
        event_id="result-1",
        call_id="call-1",
        name="read",
        result={"ok": True, "content": [{"type": "text", "text": "done"}]},
        capability_id="builtin.read",
        model_telemetry={"input_tokens": 1},
        prompt_trace={"session_id": "session-1"},
    )

    assert event.to_event() == {
        "type": "tool_result",
        "tool_call_id": "call-1",
        "status": "completed",
        "name": "read",
        "result": {"ok": True, "content": [{"type": "text", "text": "done"}]},
        "id": "result-1",
        "capability_id": "builtin.read",
        "model_telemetry": {"input_tokens": 1},
        "prompt_trace": {"session_id": "session-1"},
    }


def test_zeta_turn_aborted_runtime_event_round_trips_to_current_dict_shape() -> None:
    event = zeta_agent.TurnAbortedRuntimeEvent(
        event_id="abort-1",
        reason="deadline_exceeded",
        caused_by="tool-result-1",
    )

    assert event.to_event() == {
        "type": "turn_aborted",
        "id": "abort-1",
        "reason": "deadline_exceeded",
        "content": "(turn aborted: deadline exceeded)",
        "caused_by": "tool-result-1",
    }


def test_zeta_record_model_event_sends_same_dict_to_sink() -> None:
    events: list[dict[str, Any]] = []
    sink_events: list[dict[str, Any]] = []

    event_id, tool_calls = zeta_agent.record_model_event(
        {"content": "done"},
        events,
        prompt_trace=None,
        prompt_builder=cast(Any, None),
        event_sink=sink_events.append,
        caused_by="parent-1",
    )

    assert isinstance(event_id, str)
    assert tool_calls == []
    assert sink_events == events
    assert sink_events[0] is events[0]
    assert events[0]["content"] == "done"
    assert events[0]["caused_by"] == "parent-1"


def test_zeta_assistant_message_round_trips_content_to_model_event() -> None:
    assistant = zeta_agent.AssistantMessage.from_provider({"content": "done"})

    assert assistant.content == "done"
    assert assistant.reasoning_content == ""
    assert assistant.tool_calls == ()
    assert assistant.to_provider() == {"content": "done"}
    assert zeta_agent.model_event(assistant.to_provider()) == {
        "type": "model",
        "content": "done",
    }


def test_zeta_assistant_message_round_trips_tool_calls() -> None:
    provider_payload = {
        "content": "",
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "read", "arguments": "{}"},
            },
            "ignored",
        ],
    }

    assistant = zeta_agent.AssistantMessage.from_provider(provider_payload)

    assert assistant.tool_calls == (
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "read", "arguments": "{}"},
        },
    )
    assert zeta_agent.assistant_tool_calls(assistant.to_provider()) == [
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "read", "arguments": "{}"},
        }
    ]


def test_zeta_assistant_message_preserves_reasoning_content() -> None:
    assistant = zeta_agent.AssistantMessage.from_provider(
        {"content": "done", "reasoning_content": "thinking"}
    )

    assert assistant.reasoning_content == "thinking"
    assert zeta_agent.model_event(assistant.to_provider()) == {
        "type": "model",
        "reasoning": "thinking",
        "content": "done",
    }


def test_zeta_model_turn_carries_typed_assistant_message() -> None:
    assistant = zeta_agent.AssistantMessage.from_provider({"content": "done"})
    turn = zeta_agent.ModelTurn(
        assistant=assistant,
        streamed_content=True,
        model_telemetry={"input_tokens": 1},
        prompt_trace=None,
    )

    assert turn.assistant is assistant
    assert turn.assistant.to_provider() == {"content": "done"}
    assert turn.assistant.content == "done"


def test_zeta_request_assistant_message_returns_model_output(monkeypatch) -> None:
    def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        **kwargs: object,
    ) -> dict[str, Any]:
        del messages
        telemetry_sink = cast(
            "Callable[[dict[str, Any]], None]", kwargs["telemetry_sink"]
        )
        telemetry_sink({"usage": {"prompt_tokens": 1}})
        return {"role": "assistant", "content": "done"}

    monkeypatch.setattr(
        zeta_agent, "chat_completion_messages", fake_chat_completion_messages
    )

    output, streamed_content, telemetry = zeta_agent.request_assistant_message(
        [{"role": "user", "content": "hi"}],
        tools=[],
        tool_choice="auto",
        config=zeta_agent.AgentConfig(),
        model_status=None,
        stream_sink=None,
    )

    assert output == zeta_models_api.ModelOutput(
        message={"role": "assistant", "content": "done"}
    )
    assert streamed_content is False
    assert telemetry == {"usage": {"prompt_tokens": 1}}


def test_zeta_request_model_turn_builds_assistant_from_model_output(
    monkeypatch,
) -> None:
    class PlanOnlyPromptBuilder(zeta_prompt.PromptBuilder):
        planned = False
        committed = False

        def build(self, *args: object, **kwargs: object) -> zeta_prompt.PreparedPrompt:
            raise AssertionError("request_model_turn should use explicit prompt phases")

        def plan_prompt(
            self,
            objective: str,
            timeline: list[dict[str, Any]],
            *,
            system: str | None = None,
            allowed_capabilities: Iterable[str] | None = None,
            context: str = "",
            current_events: Iterable[dict[str, Any]] = (),
            tools: list[dict[str, Any]] | None = None,
            tool_choice: str | dict[str, Any] = "auto",
            max_tokens: int = zeta_model.DEFAULT_MAX_COMPLETION_TOKENS,
            selected_model: str | None = None,
            thinking: str | None = None,
        ) -> zeta_prompt.PromptPlan:
            self.planned = True
            return super().plan_prompt(
                objective,
                timeline,
                system=system,
                allowed_capabilities=allowed_capabilities,
                context=context,
                current_events=current_events,
                tools=tools,
                tool_choice=tool_choice,
                max_tokens=max_tokens,
                selected_model=selected_model,
                thinking=thinking,
            )

        def commit_prompt_plan(
            self,
            plan: zeta_prompt.PromptPlan,
        ) -> zeta_prompt.StoredPrompt:
            self.committed = True
            return super().commit_prompt_plan(plan)

    def fake_request_assistant_message(
        messages: list[dict[str, Any]],
        **kwargs: object,
    ) -> tuple[zeta_models_api.ModelOutput, bool, dict[str, Any]]:
        del messages
        del kwargs
        return (
            zeta_models_api.ModelOutput(
                message={
                    "role": "assistant",
                    "content": "done",
                    "reasoning_content": "thinking",
                }
            ),
            True,
            {"usage": {"prompt_tokens": 1}},
        )

    monkeypatch.setattr(
        zeta_agent,
        "request_assistant_message",
        fake_request_assistant_message,
    )
    state = zeta_agent.AgentTurnState()
    builder = PlanOnlyPromptBuilder()

    turn = zeta_agent.request_model_turn(
        "answer",
        [],
        config=zeta_agent.AgentConfig(),
        allowed_capabilities=(),
        context="",
        tools=[],
        state=state,
        builder=builder,
        model_status=None,
        stream_sink=None,
    )

    assert builder.planned
    assert builder.committed
    assert turn.assistant.content == "done"
    assert turn.assistant.reasoning_content == "thinking"
    assert turn.assistant.to_provider() == {
        "role": "assistant",
        "content": "done",
        "reasoning_content": "thinking",
    }
    assert turn.streamed_content is True
    assert turn.model_telemetry == {"usage": {"prompt_tokens": 1}}


def test_zeta_build_prompt_step_returns_committed_model_input() -> None:
    store = zeta_trace.InMemoryStore()
    state = zeta_agent.RunState()

    built = zeta_agent.build_prompt_step(
        "answer",
        [{"role": "user", "content": "prior"}],
        config=zeta_agent.AgentConfig(model_name="unit-model"),
        allowed_capabilities=(),
        context="Project context",
        current_events=[],
        tools=[],
        state=state,
        builder=zeta_prompt.PromptBuilder(store=store),
    )

    assert [step.step for step in state.steps] == ["build_prompt"]
    assert built.prepared_prompt.prompt_object_id is not None
    assert built.model_input == zeta_models_api.ModelInput(
        messages=built.prepared_prompt.messages,
        tools=[],
        tool_choice="auto",
        max_tokens=zeta_model.DEFAULT_MAX_COMPLETION_TOKENS,
        selected_model="unit-model",
    )


def test_zeta_call_model_step_returns_output_and_telemetry(monkeypatch) -> None:
    def fake_request_assistant_message(
        messages: list[dict[str, Any]],
        **kwargs: object,
    ) -> tuple[zeta_models_api.ModelOutput, bool, dict[str, Any]]:
        assert messages == [{"role": "user", "content": "answer"}]
        assert kwargs["tools"] == []
        return (
            zeta_models_api.ModelOutput(message={"content": "done"}),
            True,
            {"usage": {"prompt_tokens": 1}},
        )

    monkeypatch.setattr(
        zeta_agent,
        "request_assistant_message",
        fake_request_assistant_message,
    )
    state = zeta_agent.RunState()

    called = zeta_agent.call_model_step(
        zeta_models_api.ModelInput(
            messages=[{"role": "user", "content": "answer"}],
            tools=[],
            tool_choice="auto",
        ),
        config=zeta_agent.AgentConfig(),
        state=state,
        model_status=None,
        stream_sink=None,
    )

    assert [step.step for step in state.steps] == ["call_model"]
    assert called.model_output == zeta_models_api.ModelOutput(
        message={"content": "done"}
    )
    assert called.streamed_content is True
    assert called.model_telemetry == {"usage": {"prompt_tokens": 1}}


def test_zeta_record_assistant_step_links_output_to_prompt() -> None:
    store = zeta_trace.InMemoryStore()
    state = zeta_agent.RunState()
    builder = zeta_prompt.PromptBuilder(store=store)
    built = zeta_agent.build_prompt_step(
        "answer",
        [],
        config=zeta_agent.AgentConfig(),
        allowed_capabilities=(),
        context="",
        current_events=[],
        tools=[],
        state=state,
        builder=builder,
    )

    recorded = zeta_agent.record_assistant_step(
        built.prepared_prompt,
        zeta_models_api.ModelOutput(message={"content": "done"}),
        {"usage": {"prompt_tokens": 1}},
        state=state,
        builder=builder,
    )

    assert [step.step for step in state.steps] == [
        "build_prompt",
        "record_assistant",
    ]
    assert recorded.assistant.content == "done"
    assert recorded.prompt_trace is not None
    assert state.prompt_traces == [recorded.prompt_trace]
    assert state.latest_model_telemetry == {"usage": {"prompt_tokens": 1}}


def test_zeta_run_capability_step_records_call_execution_and_result(
    monkeypatch,
) -> None:
    state = zeta_agent.RunState()
    registry = CapabilityRegistry()
    projection = registry.project(())
    tool_call = {"id": "call-1", "function": {"name": "read", "arguments": "{}"}}

    def fake_handle_tool_call(
        received: dict[str, Any],
        **kwargs: object,
    ) -> zeta_agent.CapabilityCallResult:
        assert received == tool_call
        assert kwargs["index"] == 0
        return zeta_agent.CapabilityCallResult(
            events=[
                {"type": "tool_call", "tool_call_id": "call-1"},
                {"type": "tool_result", "tool_call_id": "call-1"},
            ]
        )

    monkeypatch.setattr(zeta_agent, "handle_tool_call", fake_handle_tool_call)

    result = zeta_agent.run_capability_step(
        tool_call,
        index=0,
        config=zeta_agent.AgentConfig(),
        allowed_capabilities=(),
        projection=projection,
        model_telemetry={},
        prompt_trace=None,
        builder=zeta_prompt.PromptBuilder(),
        event_sink=None,
        tool_registry=registry,
        assistant_event_id="assistant-1",
        state=state,
        cancellation_event=None,
        deadline=None,
    )

    assert [step.step for step in state.steps] == [
        "check_budget",
        "record_capability_call",
        "execute_capability",
        "record_capability_result",
    ]
    assert result.events == [
        {"type": "tool_call", "tool_call_id": "call-1"},
        {"type": "tool_result", "tool_call_id": "call-1"},
    ]


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
        tool_registry=CapabilityRegistry(),
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


def test_zeta_rpc_session_result_returns_prompt_trace_refs(
    monkeypatch,
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    trace_store = zeta_trace.InMemoryStore()
    context = zeta_context.ZetaContext(
        session_id="ctx-session",
        event_sink=event_store,
        trace_store=trace_store,
        tool_registry=CapabilityRegistry(),
        state_dir=tmp_path,
        session_dir=tmp_path / "sessions" / "ctx-session",
    )

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda *args: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        lambda *args, **kwargs: {"content": "done"},
    )

    result = zeta_rpc.run_rpc_session(
        {"objective": "answer", "tools": [], "context": ""},
        publish_event=lambda event: None,
        runtime_context=context,
    )

    trace = result["trace"]
    assert len(trace["prompt_ids"]) == 1
    assert len(trace["assistant_message_ids"]) == 1
    assert len(trace["model_event_ids"]) == 1
    assert trace["tool_call_ids"] == []
    assert trace["tool_result_ids"] == []
    assert trace_store.get_object(trace["prompt_ids"][0]) is not None
    assert trace_store.get_object(trace["assistant_message_ids"][0]) is not None


def test_zeta_rpc_tool_run_result_returns_tool_trace_refs(
    monkeypatch,
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    trace_store = zeta_trace.InMemoryStore()
    registry = CapabilityRegistry()
    registry.register(
        _test_capability(
            "ctx_echo",
            run_result={"ok": True, "content": [{"type": "text", "text": "ok"}]},
        )
    )
    context = zeta_context.ZetaContext(
        session_id="ctx-session",
        event_sink=event_store,
        trace_store=trace_store,
        tool_registry=registry,
        state_dir=tmp_path,
        session_dir=tmp_path / "sessions" / "ctx-session",
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
                            "arguments": json.dumps({"text": "hello"}),
                        },
                    }
                ],
            },
            {"content": "done"},
        ]
    )

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda *args: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        lambda *args, **kwargs: next(responses),
    )

    result = zeta_rpc.run_rpc_session(
        {"objective": "answer", "tools": ["ctx_echo"], "context": "", "max_steps": 2},
        publish_event=lambda event: None,
        runtime_context=context,
    )

    trace = result["trace"]
    assert len(trace["prompt_ids"]) == 2
    assert len(trace["assistant_message_ids"]) == 2
    assert len(trace["model_event_ids"]) == 2
    assert len(trace["tool_event_ids"]) == 2
    assert len(trace["tool_call_ids"]) == 1
    assert len(trace["tool_result_ids"]) == 1
    assert trace_store.get_object(trace["tool_call_ids"][0]) is not None
    assert trace_store.get_object(trace["tool_result_ids"][0]) is not None


def test_zeta_rpc_session_trace_refs_degrade_when_trace_data_is_missing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    context = zeta_context.ZetaContext(
        session_id="ctx-session",
        event_sink=event_store,
        trace_store=zeta_trace.InMemoryStore(),
        tool_registry=CapabilityRegistry(),
        state_dir=tmp_path,
        session_dir=tmp_path / "sessions" / "ctx-session",
    )

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda *args: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        lambda *args, **kwargs: {"content": "done"},
    )
    monkeypatch.setattr(
        zeta_prompt.PromptBuilder,
        "record_assistant_message",
        lambda self, prepared, assistant: None,
    )

    result = zeta_rpc.run_rpc_session(
        {"objective": "answer", "tools": [], "context": ""},
        publish_event=lambda event: None,
        runtime_context=context,
    )

    trace = result["trace"]
    assert trace["prompt_ids"] == []
    assert trace["assistant_message_ids"] == []
    assert len(trace["model_event_ids"]) == 1
    assert trace["tool_event_ids"] == []
    assert trace["tool_call_ids"] == []
    assert trace["tool_result_ids"] == []


def test_zeta_rpc_sequential_runs_get_distinct_run_ids(
    monkeypatch,
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    context = zeta_context.ZetaContext(
        session_id="ctx-session",
        event_sink=event_store,
        trace_store=zeta_trace.InMemoryStore(),
        tool_registry=CapabilityRegistry(),
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
        tool_registry=CapabilityRegistry(),
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


def test_zeta_rpc_cancel_unknown_and_completed_runs() -> None:
    server = zeta_rpc.JsonRpcServer(StringIO(), StringIO())

    assert server.cancel_session({"run_id": "run_missing"}) == {
        "cancelled": False,
        "run_id": "run_missing",
        "status": "unknown",
    }
    server.runs["run_done"] = zeta_rpc.RpcRunState(
        run_id="run_done",
        request_id=1,
        cancellation_event=threading.Event(),
        status="completed",
    )

    assert server.cancel_session({"run_id": "run_done"}) == {
        "cancelled": False,
        "run_id": "run_done",
        "status": "completed",
    }


def test_zeta_rpc_session_cancel_aborts_active_run(
    monkeypatch,
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    context = zeta_context.ZetaContext(
        session_id="ctx-session",
        event_sink=event_store,
        trace_store=zeta_trace.InMemoryStore(),
        tool_registry=CapabilityRegistry(),
        state_dir=tmp_path,
        session_dir=tmp_path / "sessions" / "ctx-session",
    )
    input_stream = StringIO(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session.run",
                "params": {"objective": "answer", "tools": [], "context": ""},
            }
        )
        + "\n"
        + json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session.cancel",
                "params": {"run_id": "run_cancel", "reason": "user_request"},
            }
        )
        + "\n"
    )
    output = StringIO()
    server = zeta_rpc.JsonRpcServer(input_stream, output)
    server.session_runner = lambda params: zeta_rpc.run_rpc_session(
        params,
        publish_event=server.publish_event,
        runtime_context=context,
    )

    def wait_for_cancel(*args: object, **kwargs: object) -> dict[str, str]:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            run = server.runs.get("run_cancel")
            if run is not None and run.cancellation_event.is_set():
                return {"content": "cancel observed"}
            time.sleep(0.001)
        raise AssertionError("cancel request was not delivered")

    monkeypatch.setattr(zeta_rpc, "rpc_run_id", lambda: "run_cancel")
    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda *args: True)
    monkeypatch.setattr(zeta_agent, "chat_completion_messages", wait_for_cancel)

    server.serve()

    messages = rpc_messages(output)
    cancel_response = next(message for message in messages if message.get("id") == 2)
    run_response = next(message for message in messages if message.get("id") == 1)
    published = [
        message["params"]["event"]
        for message in messages
        if message.get("method") == "events.publish"
    ]

    assert cancel_response["result"] == {
        "cancelled": True,
        "run_id": "run_cancel",
    }
    assert run_response["result"]["outcome"] == "aborted"
    assert run_response["result"]["run_id"] == "run_cancel"
    assert published[-1]["type"] == "turn_aborted"
    assert published[-1]["reason"] == "cancelled"
    assert [
        event.event_type for event in event_store.list_events(zeta_events.Filter())
    ] == ["zeta.user_message", "zeta.turn_aborted"]


def test_zeta_rpc_registers_client_tool_on_server_registry() -> None:
    registry = CapabilityRegistry()
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

    assert registered == [
        {
            "id": "rpc.ctx_read",
            "provider": "rpc",
            "name": "ctx_read",
            "aliases": ["ctx_read"],
            "description": "Read through the client.",
            "input_schema": {"type": "object"},
            "interactive": True,
            "effects": ["read"],
            "supports_staging": False,
            "supports_direct": True,
            "trust": "client",
        }
    ]
    assert registry.get("rpc.ctx_read") is not None
    assert registry.get_by_alias("ctx_read") is not None
    assert zeta_agent.tool_registry.get("ctx_read") is None


def test_zeta_rpc_registered_client_tool_exposes_capability_metadata() -> None:
    registry = CapabilityRegistry()
    server = zeta_rpc.JsonRpcServer(StringIO(), StringIO(), tool_registry=registry)

    registered = server.register_client_tools(
        [
            {
                "name": "client.write",
                "description": "Write through the client.",
                "schema": {"type": "object"},
                "effects": ["write"],
                "supports_staging": True,
                "supports_direct": False,
                "aliases": ["write", "client_write"],
                "timeout_sec": 2.5,
            }
        ]
    )

    capability = registry.get("rpc.client.write")
    assert registered == [
        {
            "id": "rpc.client.write",
            "provider": "rpc",
            "name": "client.write",
            "aliases": ["write", "client_write"],
            "description": "Write through the client.",
            "input_schema": {"type": "object"},
            "interactive": True,
            "effects": ["write"],
            "supports_staging": True,
            "supports_direct": False,
            "trust": "client",
            "timeout_sec": 2.5,
        }
    ]
    assert capability is not None
    assert capability.spec.metadata() == {
        "id": "rpc.client.write",
        "provider": "rpc",
        "name": "client.write",
        "aliases": ["write", "client_write"],
        "description": "Write through the client.",
        "input_schema": {"type": "object"},
        "interactive": True,
        "effects": ["write"],
    }
    assert capability.policy.supports_staging is True
    assert capability.policy.supports_direct is False
    assert capability.policy.timeout_seconds == 2.5


def test_zeta_rpc_rejects_missing_client_tool_schema() -> None:
    input_stream = StringIO(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools.register",
                "params": {
                    "tools": [
                        {
                            "name": "client.bad",
                            "description": "Bad client schema.",
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
        input_stream, output, tool_registry=CapabilityRegistry()
    )

    server.serve()

    message = rpc_messages(output)[0]
    assert message["error"]["code"] == -32602
    assert message["error"]["message"] == "Invalid params"
    assert message["error"]["data"]["code"] == "missing_tool_schema"
    assert message["error"]["data"]["tool"] == "client.bad"


def test_zeta_rpc_rejects_invalid_client_tool_schema() -> None:
    input_stream = StringIO(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools.register",
                "params": {
                    "tools": [
                        {
                            "name": "client.bad",
                            "description": "Bad client schema.",
                            "schema": {"type": "definitely-not-json-schema"},
                        }
                    ]
                },
            }
        )
        + "\n"
    )
    output = StringIO()
    server = zeta_rpc.JsonRpcServer(
        input_stream, output, tool_registry=CapabilityRegistry()
    )

    server.serve()

    message = rpc_messages(output)[0]
    assert message["error"]["code"] == -32602
    assert message["error"]["message"] == "Invalid params"
    assert message["error"]["data"]["code"] == "invalid_tool_schema"
    assert message["error"]["data"]["tool"] == "client.bad"


def test_zeta_rpc_mutating_client_tool_without_staging_is_refused_in_propose() -> None:
    registry = CapabilityRegistry()
    server = zeta_rpc.JsonRpcServer(StringIO(), StringIO(), tool_registry=registry)
    server.register_client_tools(
        [
            {
                "name": "client.write",
                "description": "Write through the client.",
                "schema": {"type": "object"},
                "effects": ["write"],
                "supports_direct": True,
            }
        ]
    )

    result = registry.invoke("client.write", {}, execution_mode="stage")

    assert result["ok"] is False
    assert result["error"]["code"] == "staging-unsupported"


def test_zeta_rpc_mutating_client_tool_requires_direct_execution_opt_in() -> None:
    registry = CapabilityRegistry()
    server = zeta_rpc.JsonRpcServer(StringIO(), StringIO(), tool_registry=registry)
    server.register_client_tools(
        [
            {
                "name": "client.write",
                "description": "Write through the client.",
                "schema": {"type": "object"},
                "effects": ["write"],
                "supports_staging": True,
            }
        ]
    )

    result = registry.invoke("client.write", {}, execution_mode="direct")

    assert result == {
        "ok": False,
        "error": {
            "code": "direct-execution-disallowed",
            "message": "capability rpc.client.write does not allow direct execution",
        },
    }


def test_zeta_rpc_allows_client_alias_to_coexist_with_other_provider() -> None:
    registry = CapabilityRegistry()
    registry.register(_test_capability("ctx_read", run_result={"ok": True}))
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
            "result": {
                "registered": [
                    {
                        "id": "rpc.ctx_read",
                        "provider": "rpc",
                        "name": "ctx_read",
                        "aliases": ["ctx_read"],
                        "description": "Read through the client.",
                        "input_schema": {"type": "object"},
                        "interactive": True,
                        "effects": ["read"],
                        "supports_staging": False,
                        "supports_direct": True,
                        "trust": "client",
                    }
                ]
            },
        }
    ]
    assert registry.get("test.ctx_read") is not None
    assert registry.get("rpc.ctx_read") is not None


def test_zeta_rpc_client_alias_collision_is_rejected_at_projection_time() -> None:
    registry = CapabilityRegistry()
    registry.register(_test_capability("read", provider="sigil", aliases=("read",)))
    server = zeta_rpc.JsonRpcServer(StringIO(), StringIO(), tool_registry=registry)

    server.register_client_tools(
        [
            {
                "name": "read",
                "description": "Read through the client.",
                "schema": {"type": "object"},
                "effects": ["read"],
                "aliases": ["read"],
            }
        ]
    )

    assert registry.get("sigil.read") is not None
    assert registry.get("rpc.read") is not None
    with pytest.raises(ValueError, match="ambiguous capability alias 'read'"):
        registry.project(("sigil.read", "rpc.read"))


def test_zeta_agent_auto_enabled_capabilities_omit_low_trust_mutating_tools() -> None:
    registry = CapabilityRegistry()
    registry.register(_test_capability("read", provider="host", aliases=("read",)))
    registry.register(
        _test_capability(
            "write",
            provider="rpc",
            effects=("write",),
            aliases=("write",),
            supports_staging=True,
            supports_direct=True,
            trust="client",
        )
    )

    assert zeta_agent.registered_capabilities(None, tool_registry=registry) == (
        "host.read",
    )
    assert zeta_agent.registered_capabilities(("write",), tool_registry=registry) == (
        "rpc.write",
    )


def test_zeta_rpc_rejects_privileged_client_tool_trust() -> None:
    input_stream = StringIO(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools.register",
                "params": {
                    "tools": [
                        {
                            "name": "client.read",
                            "description": "Spoof host read.",
                            "schema": {"type": "object"},
                            "effects": ["read"],
                            "trust": "host",
                        }
                    ]
                },
            }
        )
        + "\n"
    )
    output = StringIO()
    server = zeta_rpc.JsonRpcServer(
        input_stream, output, tool_registry=CapabilityRegistry()
    )

    server.serve()

    message = rpc_messages(output)[0]
    assert message["error"]["code"] == -32602
    assert message["error"]["message"] == "Invalid params"
    assert message["error"]["data"]["code"] == "invalid_tool_trust"
    assert message["error"]["data"]["tool"] == "client.read"


def test_zeta_rpc_rejects_non_rpc_client_tool_provider() -> None:
    input_stream = StringIO(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools.register",
                "params": {
                    "tools": [
                        {
                            "provider": "sigil",
                            "name": "read",
                            "description": "Spoof builtin read.",
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
        input_stream, output, tool_registry=CapabilityRegistry()
    )

    server.serve()

    message = rpc_messages(output)[0]
    assert message["error"]["code"] == -32602
    assert message["error"]["message"] == "Invalid params"
    assert message["error"]["data"]["code"] == "invalid_tool_provider"
    assert message["error"]["data"]["tool"] == "read"


def test_zeta_rpc_rejects_reregistering_client_owned_tool() -> None:
    input_stream = StringIO(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools.register",
                "params": {
                    "tools": [
                        {
                            "name": "client.echo",
                            "description": "Echo from the client.",
                            "schema": {"type": "object"},
                            "effects": ["read"],
                        },
                        {
                            "name": "client.echo",
                            "description": "Echo from the client.",
                            "schema": {"type": "object"},
                            "effects": ["read"],
                        },
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
        tool_registry=CapabilityRegistry(),
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
                    "message": "tool 'client.echo' is already registered",
                    "tool": "client.echo",
                },
            },
        }
    ]


def test_zeta_rpc_registers_client_tools_and_calls_client(monkeypatch) -> None:
    monkeypatch.setattr(zeta_rpc.uuid, "uuid4", lambda: "client-call-1")
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
    capability = server.tool_registry.get("rpc.client.echo")
    assert capability is not None
    assert isinstance(capability.executor, zeta_rpc.RpcClientCapabilityExecutor)

    result = server.tool_registry.invoke("client.echo", {"text": "hello"})

    assert result == {"ok": True, "content": [{"type": "text", "text": "ok"}]}
    messages = rpc_messages(output)
    assert len(messages) == 1
    assert messages[0]["jsonrpc"] == "2.0"
    assert messages[0]["method"] == "tools.call"
    assert messages[0]["params"]["name"] == "client.echo"
    assert messages[0]["params"]["arguments"] == {"text": "hello"}
    assert messages[0]["params"]["status"] == "requested"
    assert server.tool_calls["client-call-1"].status == "responded"


def test_zeta_rpc_client_tool_call_rejects_malformed_response() -> None:
    input_stream = StringIO(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "tools.respond",
                "params": {"id": "client-call-1", "result": {"content": []}},
            }
        )
        + "\n"
    )
    output = StringIO()
    server = zeta_rpc.JsonRpcServer(input_stream, output)

    result = server.call_client_tool("client-call-1", "client.echo", {})

    assert result == {
        "ok": False,
        "error": {
            "code": "invalid-tool-response",
            "message": "tool response result must include boolean ok",
        },
    }
    assert server.tool_calls["client-call-1"].status == "failed"


def test_zeta_rpc_client_tool_call_records_cancellation() -> None:
    input_stream = StringIO(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "tools.respond",
                "params": {"id": "client-call-1", "cancelled": True},
            }
        )
        + "\n"
    )
    output = StringIO()
    server = zeta_rpc.JsonRpcServer(input_stream, output)

    result = server.call_client_tool("client-call-1", "client.echo", {})

    assert result == {
        "ok": False,
        "error": {
            "code": "client-cancelled",
            "message": "client cancelled tool call client-call-1",
        },
    }
    assert server.tool_calls["client-call-1"].status == "cancelled"


def test_zeta_rpc_client_tool_call_records_disconnect_failure() -> None:
    server = zeta_rpc.JsonRpcServer(StringIO(), StringIO())

    result = server.call_client_tool("client-call-1", "client.echo", {})

    assert result == {
        "ok": False,
        "error": {"code": "client-disconnected", "message": "client.echo"},
    }
    assert server.tool_calls["client-call-1"].status == "failed"


def test_zeta_rpc_client_tool_call_times_out(monkeypatch) -> None:
    server = zeta_rpc.JsonRpcServer(StringIO(), StringIO())

    def slow_read_message() -> None:
        time.sleep(0.02)
        return None

    monkeypatch.setattr(server, "read_message", slow_read_message)

    result = server.call_client_tool(
        "client-call-1",
        "client.echo",
        {},
        timeout_sec=0.001,
    )

    assert result == {
        "ok": False,
        "error": {
            "code": "client-tool-timeout",
            "message": "client tool client.echo timed out after 0.001s",
        },
    }
    assert server.tool_calls["client-call-1"].status == "timed_out"


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

    assert [
        event["content"] for event in rpc_messages(output)[0]["result"]["events"]
    ] == [
        "one",
        "three",
    ]


def test_zeta_rpc_events_subscribe_filters_after_cursor() -> None:
    input_stream = StringIO(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "events.subscribe",
                "params": {"after": "1"},
            }
        )
        + "\n"
    )
    output = StringIO()
    server = zeta_rpc.JsonRpcServer(input_stream, output)

    server.serve()
    server.publish_event({"type": "user_message", "content": "old", "cursor": "1"})
    server.publish_event({"type": "user_message", "content": "new", "cursor": "2"})

    messages = rpc_messages(output)
    assert messages[0]["result"]["subscription_id"].startswith("sub_")
    assert [
        message["params"]["event"]["content"]
        for message in messages
        if message.get("method") == "events.publish"
    ] == ["new"]


def test_zeta_rpc_events_subscribe_filters_by_session_and_run() -> None:
    input_stream = StringIO(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "events.subscribe",
                "params": {"session_id": "session-1", "run_id": "run_1"},
            }
        )
        + "\n"
    )
    output = StringIO()
    server = zeta_rpc.JsonRpcServer(input_stream, output)

    server.serve()
    server.publish_event(
        {
            "type": "user_message",
            "content": "wrong-session",
            "session": "session-2",
            "run_id": "run_1",
            "cursor": "1",
        }
    )
    server.publish_event(
        {
            "type": "user_message",
            "content": "wrong-run",
            "session": "session-1",
            "run_id": "run_2",
            "cursor": "2",
        }
    )
    server.publish_event(
        {
            "type": "user_message",
            "content": "match",
            "session": "session-1",
            "run_id": "run_1",
            "cursor": "3",
        }
    )

    assert [
        message["params"]["event"]["content"]
        for message in rpc_messages(output)
        if message.get("method") == "events.publish"
    ] == ["match"]


def test_zeta_rpc_publish_event_keeps_default_stream_without_subscription() -> None:
    output = StringIO()
    server = zeta_rpc.JsonRpcServer(StringIO(), output)

    server.publish_event({"type": "user_message", "content": "live", "cursor": "1"})

    assert rpc_messages(output) == [
        {
            "jsonrpc": "2.0",
            "method": "events.publish",
            "params": {
                "event": {"type": "user_message", "content": "live", "cursor": "1"}
            },
        }
    ]


def test_zeta_agent_turn_uses_explicit_tool_registry(monkeypatch) -> None:
    registry = CapabilityRegistry()
    registry.register(
        _test_capability(
            "ctx_echo",
            schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
            run_result={
                "ok": True,
                "content": [{"type": "text", "text": "from ctx"}],
            },
        )
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
        zeta_agent.AgentConfig(allowed_capabilities=("ctx_echo",), max_turns=2),
        tool_registry=registry,
    )

    assert zeta_agent.tool_registry.get("ctx_echo") is None
    assert result.final_text == "done"
    assert [event.get("name") for event in result.events if "name" in event] == [
        "ctx_echo",
        "ctx_echo",
    ]


def test_zeta_agent_turn_resolves_model_alias_through_projection(monkeypatch) -> None:
    registry = CapabilityRegistry()
    registry.register(_test_capability("read", provider="host", aliases=("read",)))
    registry.register(_test_capability("read", provider="rpc", aliases=("read",)))
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
                    }
                ]
            },
            {"content": "done"},
        ]
    )
    invoked: list[tuple[str, dict[str, Any]]] = []

    def fake_invoke(
        capability_id: str,
        params: dict[str, Any],
        **kwargs: object,
    ) -> dict[str, Any]:
        invoked.append((capability_id, params))
        return {"ok": True, "content": [{"type": "text", "text": "ok"}]}

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        lambda *args, **kwargs: next(responses),
    )
    monkeypatch.setattr(zeta_agent, "invoke_capability", fake_invoke)

    result = zeta_agent.run_agent_turn(
        "read",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("host.read",), max_turns=2),
        tool_registry=registry,
    )

    assert result.final_text == "done"
    assert invoked == [("host.read", {"path": "README.md"})]
    tool_call = next(event for event in result.events if event["type"] == "tool_call")
    tool_result = next(
        event for event in result.events if event["type"] == "tool_result"
    )
    assert tool_call["name"] == "read"
    assert tool_call["capability_id"] == "host.read"
    assert tool_result["name"] == "read"
    assert tool_result["capability_id"] == "host.read"


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
        zeta_agent.AgentConfig(
            allowed_capabilities=("read",), max_turns=1, thinking="none"
        ),
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
        zeta_agent.AgentConfig(allowed_capabilities=("read",), max_turns=1),
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
        zeta_agent.AgentConfig(allowed_capabilities=("read",), max_turns=1),
        trace_store=store,
    )

    assert result.final_text == "done"
    assert result.events[0]["type"] == "model"
    assert result.events[0]["content"] == "done"
    assert result.events[0]["prompt_trace"]["prompt_object_id"]
    assert [step.step for step in result.steps] == [
        "check_budget",
        "build_prompt",
        "call_model",
        "record_assistant",
        "finish_run",
    ]
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
            allowed_capabilities=("read",),
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
        zeta_agent.AgentConfig(allowed_capabilities=("read",), max_turns=1),
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
        zeta_agent.AgentConfig(allowed_capabilities=("read",), max_turns=2),
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
        "invoke_capability",
        lambda name, params: read_tool_payload(target),
    )

    result = zeta_agent.run_agent_turn(
        "inspect",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("read",), max_turns=2),
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
        "invoke_capability",
        lambda name, params: read_tool_payload(target),
    )

    result = zeta_agent.run_agent_turn(
        "inspect",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("read",), max_turns=2),
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
            allowed_capabilities=("read",),
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

    def fake_invoke(
        name: str, params: dict[str, Any], **kwargs: object
    ) -> dict[str, Any]:
        ran.append((name, params))
        return {"ok": True, "content": [{"type": "text", "text": name}]}

    monkeypatch.setattr(zeta_agent, "invoke_capability", fake_invoke)

    result = zeta_agent.run_agent_turn(
        "inspect",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("read", "ls"), max_turns=2),
        caused_by="prompt-event",
    )

    assert ran == [
        ("sigil.read", {"path": "README.md"}),
        ("sigil.ls", {"path": "src"}),
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
        "invoke_capability",
        lambda name, params: {
            "ok": True,
            "content": [{"type": "text", "text": "README"}],
        },
    )

    result = zeta_agent.run_agent_turn(
        "inspect",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("read",), max_turns=2),
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
        zeta_agent.AgentConfig(allowed_capabilities=("read",), max_turns=1),
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
        "invoke_capability",
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
        zeta_agent.AgentConfig(allowed_capabilities=("read",), max_turns=2),
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

    def fake_invoke(
        name: str, params: dict[str, Any], **kwargs: object
    ) -> dict[str, Any]:
        del name, params, kwargs
        assert [event.get("type") for event in streamed] == [
            "model",
            "tool_call",
        ]
        return {"ok": True, "content": [{"type": "text", "text": "README"}]}

    monkeypatch.setattr(zeta_agent, "invoke_capability", fake_invoke)

    result = zeta_agent.run_agent_turn(
        "inspect",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("read",), max_turns=1),
        event_sink=streamed.append,
    )

    assert result.events == streamed
    assert [event.get("type") for event in streamed] == [
        "model",
        "tool_call",
        "tool_result",
    ]
    assert [step.step for step in result.steps] == [
        "check_budget",
        "build_prompt",
        "call_model",
        "record_assistant",
        "check_budget",
        "record_capability_call",
        "execute_capability",
        "record_capability_result",
        "finish_run",
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
        "invoke_capability",
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
        zeta_agent.AgentConfig(allowed_capabilities=("bash",), max_turns=3),
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
            allowed_capabilities=("bash",),
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
        zeta_agent, "invoke_capability", lambda name, params, **kwargs: {"ok": True}
    )

    result = zeta_agent.run_agent_turn(
        "test",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("ls",)),
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
            zeta_agent.AgentConfig(allowed_capabilities=("ls",), max_turns=1),
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
        "invoke_capability",
        lambda name, params, **kwargs: read_tool_payload(target),
    )

    with pytest.raises(zeta_agent.AgentTurnAborted) as raised:
        zeta_agent.run_agent_turn(
            "test",
            [],
            zeta_agent.AgentConfig(
                allowed_capabilities=("read",),
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

    def crash_invoke(name: str, params: dict[str, Any], **kwargs: object) -> dict:
        raise ValueError("boom")

    def fake_chat_completion_messages(*args: object, **kwargs: object) -> dict:
        del args, kwargs
        return next(responses)

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent, "chat_completion_messages", fake_chat_completion_messages
    )
    monkeypatch.setattr(zeta_agent, "invoke_capability", crash_invoke)

    result = zeta_agent.run_agent_turn(
        "test",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("read",), max_turns=3),
    )

    assert result.final_text == "recovered"
    tool_result = next(
        event for event in result.events if event.get("type") == "tool_result"
    )
    assert tool_result["result"]["ok"] is False
    assert tool_result["result"]["error"]["code"] == "tool-crashed"
    assert "boom" in tool_result["result"]["error"]["message"]
    assert tool_result["status"] == "failed"


def test_zeta_agent_turn_rejects_schema_mismatch_before_running(monkeypatch) -> None:
    ran = False

    def fail_invoke(name: str, params: dict[str, Any]) -> dict[str, Any]:
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
    monkeypatch.setattr(zeta_agent, "invoke_capability", fail_invoke)

    result = zeta_agent.run_agent_turn(
        "inspect",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("read",), max_turns=1),
    )

    assert ran is False
    tool_result = next(
        event for event in result.events if event.get("type") == "tool_result"
    )
    assert tool_result["result"]["ok"] is False
    assert tool_result["result"]["error"]["code"] == "schema-mismatch"
    assert tool_result["status"] == "refused"


def test_zeta_agent_turn_rejects_disallowed_tool_before_running(monkeypatch) -> None:
    ran = False

    def fail_invoke(name: str, params: dict[str, Any]) -> dict[str, Any]:
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
    monkeypatch.setattr(zeta_agent, "invoke_capability", fail_invoke)

    result = zeta_agent.run_agent_turn(
        "inspect",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("read",), max_turns=1),
    )

    assert ran is False
    tool_result = next(
        event for event in result.events if event.get("type") == "tool_result"
    )
    assert tool_result["result"]["ok"] is False
    assert tool_result["result"]["error"]["code"] == "disallowed-tool"
    assert tool_result["status"] == "refused"


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
            allowed_capabilities=("edit",),
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
        zeta_agent.AgentConfig(allowed_capabilities=("read",), max_turns=1),
    )

    assert captured["api"] is None
