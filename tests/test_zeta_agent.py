"""Agent loop tests."""

import asyncio
import json
import threading
import time
import tomllib
from collections.abc import Callable, Iterable
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from _zeta_helpers import (
    assert_prompt_trace_replay_graph,
    assert_tool_call_derivation,
    assert_tool_result_derivation,
    assert_tool_result_derivation_graph,
    event_by_type,
    projected_tool_call_object_id,
    projected_tool_result_object_id,
    read_tool_call_response,
    read_tool_payload,
    required_stream_sink,
    timeline_events,
    tool_call_fixture,
)
from click.testing import CliRunner

from sigil import agent_io as sigil_agent_io
from sigil.agent_io import run_zeta_rpc_session
from sigil.cli import cli
from sigil.tools import ensure_builtin_tools_registered
from zeta import cli as zeta_cli
from zeta import dispatch as zeta_dispatch
from zeta import events as zeta_event_model
from zeta import loop as zeta_agent
from zeta import models as zeta_models_api
from zeta import rpc as zeta_rpc
from zeta import session as zeta_session
from zeta.agents.capabilities import CompactionPolicy
from zeta.capabilities.base import (
    InProcessCapabilityExecutor,
)
from zeta.capabilities.registry import CapabilityRegistry, RegisteredCapability
from zeta.context import builder as zeta_context
from zeta.kernel import models as zeta_model_shapes
from zeta.kernel.capabilities import (
    Capability,
    CapabilityId,
)
from zeta.kernel.events import DraftEvent, Event
from zeta.loop import AgentTurnResult
from zeta.models import chat_completions as zeta_model
from zeta.store.events import (
    Filter,
    MemoryEventStore,
    SqliteEventStore,
    event_store_path,
)
from zeta.store.substrate import InMemoryStore

zeta_trace = SimpleNamespace(InMemoryStore=InMemoryStore)

ensure_builtin_tools_registered()

zeta_events = SimpleNamespace(
    DraftEvent=DraftEvent,
    Event=Event,
    Filter=Filter,
    MemoryEventStore=MemoryEventStore,
    SqliteEventStore=SqliteEventStore,
)


def rpc_event(
    content: str,
    *,
    cursor: int,
    session_id: str | None = None,
    turn_id: str | None = None,
) -> Event:
    return Event(
        id=f"evt_{cursor}",
        event_type="zeta.user_message",
        source="test",
        payload={"content": content, "_timeline_type": "user_message"},
        idempotency_key=None,
        caused_by=None,
        session_id=session_id,
        turn_id=turn_id,
        timestamp_ms=cursor,
        cursor=cursor,
    )


def published_event_views(events: list[Event | DraftEvent]) -> list[dict[str, Any]]:
    return [
        zeta_event_model.event_view(event)
        if isinstance(event, Event)
        else zeta_event_model.draft_event_view(event)
        for event in events
    ]


def run_agent_turn(*args: Any, **kwargs: Any) -> AgentTurnResult:
    return asyncio.run(zeta_agent.async_run_agent_turn(*args, **kwargs))


def run_rpc_session(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return asyncio.run(zeta_rpc.run_rpc_session(*args, **kwargs))


def dispatch_event(
    dispatcher: zeta_dispatch.EventDispatcher,
    draft: DraftEvent,
) -> zeta_dispatch.DispatchOutcome:
    return asyncio.run(dispatcher.dispatch(draft))


def _test_capability(
    name: str,
    *,
    provider: str = "test",
    schema: dict[str, Any] | None = None,
    run_result: dict[str, Any] | None = None,
    with_stage_executor: bool = False,
) -> RegisteredCapability:
    return RegisteredCapability(
        Capability(
            CapabilityId(provider, name),
            f"{name} test capability.",
            schema or {"type": "object"},
        ),
        InProcessCapabilityExecutor(
            lambda params: (
                run_result or {"ok": True, "content": [{"type": "text", "text": "ok"}]}
            ),
            (lambda params: {"ok": True, "effect": {"status": "proposed"}})
            if with_stage_executor
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

    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api, "chat_completion_messages", fake_chat_completion_messages
    )

    result = run_agent_turn(
        "answer",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("read",), max_turns=1),
    )

    assert result.events[0].payload["reasoning"] == "weighing the options"
    assert result.events[0].payload["content"] == "done"


def test_zeta_agent_turn_emits_model_draft(monkeypatch) -> None:
    drafts: list[DraftEvent] = []

    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api,
        "chat_completion_messages",
        lambda *args, **kwargs: {"content": "done"},
    )

    result = run_agent_turn(
        "answer",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("read",), max_turns=1),
        event_sink=drafts.append,
        caused_by="prompt-1",
    )

    assert result.events[0].payload["content"] == "done"
    assert len(drafts) == 1
    assert drafts[0].event_type == "zeta.model_call.completed"
    assert drafts[0].payload == {"content": "done", "_timeline_type": "model"}
    assert drafts[0].session_id is None
    assert drafts[0].turn_id is None
    assert drafts[0].caused_by == "prompt-1"


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


def test_zeta_tool_result_event_uses_generic_failed_content_message() -> None:
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
        "message": "$ run exit 1 stderr: Traceback ValueError: bad input",
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


def test_zeta_model_event_has_boundary_dict_shape() -> None:
    assert zeta_agent.model_event(
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
    ) == {
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


def test_zeta_model_called_draft_sets_durable_metadata() -> None:
    draft = zeta_event_model.model_call_draft(
        payload={"content": "done"},
        turn_id="turn-1",
        session_id="session-1",
        caused_by="prompt-1",
        event_id="model-1",
    )

    assert draft.event_type == "zeta.model_call.completed"
    assert draft.source == "zeta"
    assert draft.payload == {"content": "done"}
    assert draft.turn_id == "turn-1"
    assert draft.session_id == "session-1"
    assert draft.caused_by == "prompt-1"
    assert draft.idempotency_key == "zeta.model_call.completed:model-1"


def test_zeta_durable_model_event_payload_keeps_domain_fields() -> None:
    payload = zeta_event_model.durable_model_event_payload(
        {
            "type": "model",
            "id": "model-1",
            "content": "done",
            "prompt_trace": {
                "prompt_object_id": "sha256:prompt",
                "assistant_message_object_id": "sha256:assistant",
            },
            "tool_call_object_ids": ["sha256:call-1"],
            "tool_call_object_id": "sha256:call-2",
        }
    )

    assert payload == {
        "_timeline_type": "model",
        "content": "done",
        "prompt_trace": {
            "prompt_object_id": "sha256:prompt",
            "assistant_message_object_id": "sha256:assistant",
        },
        "tool_call_object_ids": ["sha256:call-1"],
        "tool_call_object_id": "sha256:call-2",
    }


def test_zeta_tool_call_event_has_boundary_dict_shape() -> None:
    model_tool_call = zeta_agent.ModelToolCall(
        call_id="call-1",
        name="read",
        raw_arguments="{}",
        params={},
    )

    assert model_tool_call.event(caused_by="assistant-1") == {
        "type": "tool_call",
        "id": "call-1",
        "tool_call_id": "call-1",
        "status": "pending",
        "name": "read",
        "input": {},
        "arguments": "{}",
        "caused_by": "assistant-1",
    }


def test_zeta_tool_called_draft_sets_durable_metadata() -> None:
    draft = zeta_event_model.tool_call_draft(
        payload={"_timeline_type": "tool_call", "name": "read"},
        turn_id="turn-1",
        session_id="session-1",
        caused_by="model-1",
        event_id="tool-1",
    )

    assert draft.event_type == "zeta.tool_call.started"
    assert draft.source == "zeta"
    assert draft.payload == {"_timeline_type": "tool_call", "name": "read"}
    assert draft.turn_id == "turn-1"
    assert draft.session_id == "session-1"
    assert draft.caused_by == "model-1"
    assert draft.idempotency_key == "zeta.tool_call.started:tool-1"


def test_zeta_durable_tool_result_event_payload_keeps_domain_fields() -> None:
    payload = zeta_event_model.durable_tool_event_payload(
        {
            "type": "tool_result",
            "id": "result-1",
            "result": {"ok": True},
            "tool_call_object_id": "sha256:call",
            "tool_result_object_id": "sha256:result",
        }
    )

    assert payload == {
        "_timeline_type": "tool_result",
        "result": {"ok": True},
        "tool_call_object_id": "sha256:call",
        "tool_result_object_id": "sha256:result",
    }


def test_zeta_durable_tool_call_event_payload_keeps_domain_fields() -> None:
    payload = zeta_event_model.durable_tool_event_payload(
        {
            "type": "tool_call",
            "id": "call-1",
            "name": "read",
            "tool_call_object_id": "sha256:call",
        }
    )

    assert payload == {
        "_timeline_type": "tool_call",
        "name": "read",
        "tool_call_object_id": "sha256:call",
    }


def test_zeta_tool_result_event_has_boundary_dict_shape() -> None:
    event = zeta_agent.tool_result_event(
        "call-1",
        "read",
        {"ok": True, "content": [{"type": "text", "text": "done"}]},
        capability_id="builtin.read",
        model_telemetry={"input_tokens": 1},
    )
    event["id"] = "result-1"
    event["prompt_trace"] = {"session_id": "session-1"}

    assert event == {
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


def test_zeta_record_model_event_sends_same_draft_to_sink() -> None:
    events: list[DraftEvent] = []
    sink_events: list[DraftEvent] = []
    ctx = zeta_agent.TurnContext(
        event_sink=sink_events.append,
        trace_store=None,
        tool_registry=CapabilityRegistry(),
        builder=cast(Any, None),
        cancellation_event=None,
        deadline=None,
    )

    event_id, tool_calls = zeta_agent.record_model_event(
        {"content": "done"},
        events,
        prompt_trace=None,
        caused_by="parent-1",
        ctx=ctx,
    )

    assert isinstance(event_id, str)
    assert tool_calls == []
    assert sink_events == events
    assert sink_events[0] is events[0]
    assert events[0].payload["content"] == "done"
    assert events[0].caused_by == "parent-1"


def test_zeta_record_model_event_records_draft() -> None:
    events: list[DraftEvent] = []
    drafts: list[DraftEvent] = []

    ctx = zeta_agent.TurnContext(
        event_sink=drafts.append,
        trace_store=None,
        tool_registry=CapabilityRegistry(),
        builder=cast(Any, None),
        cancellation_event=None,
        deadline=None,
    )

    event_id, tool_calls = zeta_agent.record_model_event(
        {"content": "done"},
        events,
        prompt_trace=None,
        caused_by="parent-1",
        ctx=ctx,
    )

    assert isinstance(event_id, str)
    assert tool_calls == []
    assert len(events) == 1
    assert len(drafts) == 1
    assert drafts[0].event_type == "zeta.model_call.completed"
    assert drafts[0].payload == {
        "_timeline_type": "model",
        "content": "done",
    }
    assert drafts[0].session_id is None
    assert drafts[0].turn_id is None
    assert drafts[0].caused_by == "parent-1"
    assert drafts[0].idempotency_key == f"zeta.model_call.completed:{event_id}"


def test_zeta_handle_tool_call_emits_drafts() -> None:
    drafts: list[DraftEvent] = []

    registry = CapabilityRegistry()
    registry.register(
        _test_capability(
            "read",
            run_result={"ok": True, "content": [{"type": "text", "text": "done"}]},
        )
    )
    allowed_capabilities = ("test.read",)
    ctx = zeta_agent.TurnContext(
        event_sink=drafts.append,
        trace_store=None,
        tool_registry=registry,
        builder=zeta_context.PromptBuilder(),
        cancellation_event=None,
        deadline=None,
    )

    result = asyncio.run(
        zeta_agent.handle_tool_call(
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "read", "arguments": '{"path": "README.md"}'},
            },
            allowed_capabilities=allowed_capabilities,
            projection=registry.project(allowed_capabilities),
            index=0,
            execution_mode="direct",
            caused_by="model-1",
            ctx=ctx,
        )
    )

    assert [event["type"] for event in timeline_events(result.events)] == [
        "tool_call",
        "tool_result",
    ]
    assert [draft.event_type for draft in drafts] == [
        "zeta.tool_call.started",
        "zeta.tool_call.completed",
    ]
    assert drafts[0].payload == {
        "_timeline_type": "tool_call",
        "arguments": '{"path": "README.md"}',
        "capability_id": "test.read",
        "input": {"path": "README.md"},
        "name": "read",
        "status": "pending",
        "tool_call_id": "call-1",
    }
    assert drafts[1].payload["_timeline_type"] == "tool_result"
    assert drafts[1].payload["result"] == {
        "ok": True,
        "content": [{"type": "text", "text": "done"}],
    }
    assert [draft.session_id for draft in drafts] == [None, None]
    assert [draft.turn_id for draft in drafts] == [None, None]
    assert [draft.caused_by for draft in drafts] == ["model-1", "model-1"]


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
        zeta_models_api, "chat_completion_messages", fake_chat_completion_messages
    )

    output, streamed_content, telemetry = asyncio.run(
        zeta_agent.request_assistant_message(
            zeta_model_shapes.ModelInput(
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
                tool_choice="auto",
            ),
            config=zeta_agent.AgentConfig(),
        )
    )

    assert output == zeta_model_shapes.ModelOutput(
        message={"role": "assistant", "content": "done"}
    )
    assert streamed_content is False
    assert telemetry == {"usage": {"prompt_tokens": 1}}


def test_zeta_request_model_turn_builds_assistant_from_model_output(
    monkeypatch,
) -> None:
    class PlanOnlyPromptBuilder(zeta_context.PromptBuilder):
        planned = False
        committed = False

        def build(self, *args: object, **kwargs: object) -> zeta_context.PreparedPrompt:
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
        ) -> zeta_context.PromptPlan:
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
            plan: zeta_context.PromptPlan,
        ) -> zeta_context.StoredPrompt:
            self.committed = True
            return super().commit_prompt_plan(plan)

    def fake_request_assistant_message(
        model_input: zeta_model_shapes.ModelInput,
        **kwargs: object,
    ) -> tuple[zeta_model_shapes.ModelOutput, bool, dict[str, Any]]:
        assert model_input.messages
        del kwargs
        return (
            zeta_model_shapes.ModelOutput(
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
    ctx = zeta_agent.TurnContext(
        event_sink=None,
        trace_store=None,
        tool_registry=CapabilityRegistry(),
        builder=builder,
        cancellation_event=None,
        deadline=None,
    )

    turn = asyncio.run(
        zeta_agent.request_model_turn(
            "answer",
            [],
            config=zeta_agent.AgentConfig(),
            allowed_capabilities=(),
            context="",
            tools=[],
            state=state,
            ctx=ctx,
        )
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

    prepared_prompt, model_input = zeta_agent.build_prompt_step(
        "answer",
        [{"role": "user", "content": "prior"}],
        config=zeta_agent.AgentConfig(model_name="unit-model"),
        allowed_capabilities=(),
        context="Project context",
        current_events=[],
        tools=[],
        state=state,
        builder=zeta_context.PromptBuilder(store=store),
    )

    assert [step.step for step in state.steps] == ["build_prompt"]
    assert prepared_prompt.prompt_object_id is not None
    assert model_input == zeta_model_shapes.ModelInput(
        messages=prepared_prompt.messages,
        tools=[],
        tool_choice="auto",
        max_tokens=zeta_model.DEFAULT_MAX_COMPLETION_TOKENS,
        selected_model="unit-model",
    )


def test_zeta_call_model_step_returns_output_and_telemetry() -> None:
    class FakeGateway:
        def available(self, config: zeta_agent.AgentConfig) -> bool:
            return True

        async def generate(
            self,
            model_input: zeta_model_shapes.ModelInput,
            config: zeta_agent.AgentConfig,
            *,
            stream: zeta_agent.ModelStream | None = None,
            telemetry_sink: Callable[[dict[str, Any]], None] | None = None,
        ) -> zeta_model_shapes.ModelOutput:
            del config, stream
            assert model_input.messages == [{"role": "user", "content": "answer"}]
            assert model_input.tools == []
            if telemetry_sink is not None:
                telemetry_sink({"usage": {"prompt_tokens": 1}})
            return zeta_model_shapes.ModelOutput(message={"content": "done"})

    state = zeta_agent.RunState()

    model_output, streamed_content, model_telemetry = asyncio.run(
        zeta_agent.call_model_step(
            zeta_model_shapes.ModelInput(
                messages=[{"role": "user", "content": "answer"}],
                tools=[],
                tool_choice="auto",
            ),
            config=zeta_agent.AgentConfig(),
            state=state,
            model_gateway=FakeGateway(),
            event_sink=None,
        )
    )

    assert [step.step for step in state.steps] == ["call_model"]
    assert model_output == zeta_model_shapes.ModelOutput(message={"content": "done"})
    assert streamed_content is False
    assert model_telemetry == {"usage": {"prompt_tokens": 1}}


def test_zeta_agent_compaction_policy_bounds_model_input() -> None:
    captured: dict[str, zeta_model_shapes.ModelInput] = {}

    class FakeGateway:
        def available(self, config: zeta_agent.AgentConfig) -> bool:
            return True

        def generate(
            self,
            model_input: zeta_model_shapes.ModelInput,
            config: zeta_agent.AgentConfig,
            *,
            stream: zeta_agent.ModelStream | None = None,
            telemetry_sink: Callable[[dict[str, Any]], None] | None = None,
        ) -> zeta_model_shapes.ModelOutput:
            del config, stream, telemetry_sink
            captured["model_input"] = model_input
            return zeta_model_shapes.ModelOutput(message={"content": "done"})

    prior_timeline = [
        {
            "type": "user_message",
            "content": "old context " * 200,
        },
        {
            "type": "model",
            "content": "old answer " * 200,
        },
    ]

    result = run_agent_turn(
        "answer now",
        prior_timeline,
        zeta_agent.AgentConfig(
            max_turns=1,
            compaction_policy=CompactionPolicy(
                strategy="drop_oldest",
                max_context_tokens=80,
            ),
        ),
        model_gateway=FakeGateway(),
    )

    assert result.final_answer == "done"
    rendered_messages = json.dumps(captured["model_input"].messages)
    assert "old context" not in rendered_messages
    assert "old answer" not in rendered_messages
    assert "answer now" in rendered_messages


def test_zeta_async_agent_turn_runs_turns_concurrently() -> None:
    barrier = asyncio.Event()
    seen: list[str] = []

    class BlockingGateway:
        def available(self, config: zeta_agent.AgentConfig) -> bool:
            return True

        async def generate(
            self,
            model_input: zeta_model_shapes.ModelInput,
            config: zeta_agent.AgentConfig,
            *,
            stream: zeta_agent.ModelStream | None = None,
            telemetry_sink: Callable[[dict[str, Any]], None] | None = None,
        ) -> zeta_model_shapes.ModelOutput:
            del config, stream, telemetry_sink
            objective = str(model_input.messages[-1]["content"]).splitlines()[0]
            seen.append(objective)
            if len(seen) == 2:
                barrier.set()
            await barrier.wait()
            return zeta_model_shapes.ModelOutput(message={"content": objective})

    async def run() -> None:
        gateway = BlockingGateway()
        first, second = await asyncio.wait_for(
            asyncio.gather(
                zeta_agent.async_run_agent_turn(
                    "first",
                    [],
                    zeta_agent.AgentConfig(max_turns=1),
                    model_gateway=gateway,
                ),
                zeta_agent.async_run_agent_turn(
                    "second",
                    [],
                    zeta_agent.AgentConfig(max_turns=1),
                    model_gateway=gateway,
                ),
            ),
            timeout=3,
        )

        assert {first.final_answer, second.final_answer} == {"first", "second"}

    asyncio.run(run())
    assert set(seen) == {"first", "second"}


def test_zeta_record_assistant_step_links_output_to_prompt() -> None:
    store = zeta_trace.InMemoryStore()
    state = zeta_agent.RunState()
    builder = zeta_context.PromptBuilder(store=store)
    prepared_prompt, _ = zeta_agent.build_prompt_step(
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

    assistant, prompt_trace = zeta_agent.record_assistant_step(
        prepared_prompt,
        zeta_model_shapes.ModelOutput(message={"content": "done"}),
        {"usage": {"prompt_tokens": 1}},
        state=state,
        builder=builder,
    )

    assert [step.step for step in state.steps] == [
        "build_prompt",
        "record_assistant",
    ]
    assert assistant.content == "done"
    assert prompt_trace is not None
    assert state.prompt_traces == [prompt_trace]
    assert state.latest_model_telemetry == {"usage": {"prompt_tokens": 1}}


def test_zeta_run_capability_step_records_call_execution_and_result(
    monkeypatch,
) -> None:
    state = zeta_agent.RunState()
    registry = CapabilityRegistry()
    projection = registry.project(())
    tool_call = {"id": "call-1", "function": {"name": "read", "arguments": "{}"}}
    ctx = zeta_agent.TurnContext(
        event_sink=None,
        trace_store=None,
        tool_registry=registry,
        builder=zeta_context.PromptBuilder(),
        cancellation_event=None,
        deadline=None,
    )

    def fake_handle_tool_call(
        received: dict[str, Any],
        **kwargs: object,
    ) -> zeta_agent.CapabilityCallResult:
        assert received == tool_call
        assert kwargs["index"] == 0
        return zeta_agent.CapabilityCallResult(
            events=[
                zeta_event_model.runtime_event_draft(
                    {"type": "tool_call", "id": "call-1", "tool_call_id": "call-1"},
                    session_id=None,
                    turn_id=None,
                ),
                zeta_event_model.runtime_event_draft(
                    {
                        "type": "tool_result",
                        "id": "result-1",
                        "tool_call_id": "call-1",
                        "result": {"ok": True},
                    },
                    session_id=None,
                    turn_id=None,
                ),
            ]
        )

    monkeypatch.setattr(zeta_agent, "handle_tool_call", fake_handle_tool_call)

    result = asyncio.run(
        zeta_agent.run_capability_step(
            tool_call,
            index=0,
            config=zeta_agent.AgentConfig(),
            allowed_capabilities=(),
            projection=projection,
            model_telemetry={},
            assistant_event_id="assistant-1",
            state=state,
            ctx=ctx,
        )
    )

    assert [step.step for step in state.steps] == [
        "check_budget",
        "record_capability_call",
        "execute_capability",
        "record_capability_result",
    ]
    projected = timeline_events(result.events)
    assert projected == [
        {
            "type": "tool_call",
            "id": "call-1",
            "tool_call_id": "call-1",
            "time": projected[0]["time"],
        },
        {
            "type": "tool_result",
            "id": "result-1",
            "tool_call_id": "call-1",
            "result": {"ok": True},
            "time": projected[1]["time"],
        },
    ]


def test_zeta_run_capability_step_reconciles_existing_terminal_result(
    monkeypatch,
) -> None:
    state = zeta_agent.RunState(
        events=[
            zeta_event_model.runtime_event_draft(
                {
                    "type": "tool_result",
                    "id": "result-1",
                    "tool_call_id": "call-1",
                    "status": "completed",
                    "result": {"ok": True},
                },
                session_id=None,
                turn_id=None,
            )
        ]
    )
    registry = CapabilityRegistry()
    projection = registry.project(())
    invoked = False
    ctx = zeta_agent.TurnContext(
        event_sink=None,
        trace_store=None,
        tool_registry=registry,
        builder=zeta_context.PromptBuilder(),
        cancellation_event=None,
        deadline=None,
    )

    def fail_handle_tool_call(
        *args: object, **kwargs: object
    ) -> zeta_agent.CapabilityCallResult:
        nonlocal invoked
        invoked = True
        return zeta_agent.CapabilityCallResult(events=[])

    monkeypatch.setattr(zeta_agent, "handle_tool_call", fail_handle_tool_call)

    result = asyncio.run(
        zeta_agent.run_capability_step(
            {"id": "call-1", "function": {"name": "read", "arguments": "{}"}},
            index=0,
            config=zeta_agent.AgentConfig(),
            allowed_capabilities=(),
            projection=projection,
            model_telemetry={},
            assistant_event_id="assistant-1",
            state=state,
            ctx=ctx,
        )
    )

    assert invoked is False
    assert result.events == []
    assert [step.step for step in state.steps] == [
        "check_budget",
        "record_capability_result",
    ]


def rpc_messages(output: StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in output.getvalue().splitlines()]


def test_zeta_rpc_initialize_returns_server_metadata() -> None:
    input_stream = StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n"
    )
    output = StringIO()
    server = zeta_rpc.JsonRpcServer(input_stream, output)

    asyncio.run(server.serve())

    assert rpc_messages(output) == [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"server": "zeta", "protocol": "0.1"},
        }
    ]


def test_zeta_async_rpc_initialize_returns_server_metadata() -> None:
    async def run() -> None:
        input_stream = StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n"
        )
        output = StringIO()
        server = zeta_rpc.JsonRpcServer(input_stream, output)

        await server.serve()

        assert rpc_messages(output) == [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"server": "zeta", "protocol": "0.1"},
            }
        ]

    asyncio.run(run())


def test_zeta_async_rpc_session_runs_in_task_group() -> None:
    async def run() -> None:
        input_stream = StringIO(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "session.run",
                    "params": {"objective": "one"},
                }
            )
            + "\n"
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session.run",
                    "params": {"objective": "two"},
                }
            )
            + "\n"
        )
        output = StringIO()
        started: list[str] = []
        release = asyncio.Event()

        async def session_runner(params: dict[str, Any]) -> dict[str, Any]:
            objective = cast(str, params["objective"])
            started.append(objective)
            if len(started) == 2:
                release.set()
            await release.wait()
            return {"run_id": params["_zeta_run_id"], "outcome": objective}

        server = zeta_rpc.JsonRpcServer(
            input_stream,
            output,
            session_runner=session_runner,
        )

        await server.serve()

        messages = rpc_messages(output)
        assert started == ["one", "two"]
        assert {message["id"] for message in messages} == {1, 2}
        assert {message["result"]["outcome"] for message in messages} == {"one", "two"}

    asyncio.run(run())


def test_zeta_rpc_unknown_method_returns_structured_error() -> None:
    input_stream = StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "missing.method"}) + "\n"
    )
    output = StringIO()
    server = zeta_rpc.JsonRpcServer(input_stream, output)

    asyncio.run(server.serve())

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


def test_zeta_rpc_sync_dispatch_uses_registered_method_handlers() -> None:
    server = zeta_rpc.JsonRpcServer(StringIO(), StringIO())

    assert server.dispatch_sync("initialize", {}) == {
        "server": "zeta",
        "protocol": "0.1",
    }
    assert server.dispatch_sync("tools.register", {"tools": []}) == {"registered": []}


def test_zeta_async_rpc_dispatch_uses_registered_method_handlers() -> None:
    async def run() -> None:
        server = zeta_rpc.JsonRpcServer(StringIO(), StringIO())
        server.runs["run_active"] = zeta_rpc.RpcRunState(
            run_id="run_active",
            request_id=1,
            cancellation_event=asyncio.Event(),
        )

        result = await server.dispatch(
            "session.cancel",
            {"run_id": "run_active"},
        )

        assert result == {"cancelled": True, "run_id": "run_active"}
        assert server.runs["run_active"].status == "cancelling"

    asyncio.run(run())


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

    asyncio.run(server.serve())

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


def test_zeta_rpc_session_uses_shared_runner_boundary(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_run_rpc_session(
        params: dict[str, Any],
        *,
        publish_event: Callable[[Event | DraftEvent], None],
        runtime_context: zeta_session.Session | None = None,
    ) -> dict[str, Any]:
        captured["params"] = params
        captured["runtime_context"] = runtime_context
        publish_event(DraftEvent("seen", "test", {"_timeline_type": "seen"}))
        return {"run_id": "run-1", "outcome": "completed"}

    published: list[Event | DraftEvent] = []
    monkeypatch.setattr(zeta_rpc, "run_rpc_session", fake_run_rpc_session)

    result = asyncio.run(
        zeta_rpc.run_rpc_session(
            {"objective": "answer"},
            publish_event=published.append,
            runtime_context=None,
        )
    )

    assert result == {"run_id": "run-1", "outcome": "completed"}
    assert captured["params"] == {"objective": "answer"}
    assert captured["runtime_context"] is None
    assert [event["type"] for event in published_event_views(published)] == ["seen"]


def test_sigil_zeta_rpc_session_uses_shared_runner_boundary(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_run_zeta_rpc_session(
        params: dict[str, Any],
        *,
        publish_event: Callable[[Event | DraftEvent], None],
    ) -> dict[str, Any]:
        captured["params"] = params
        publish_event(DraftEvent("seen", "test", {"_timeline_type": "seen"}))
        return {"turn_id": "turn-1", "outcome": "completed", "final_answer": "done"}

    published: list[Event | DraftEvent] = []
    monkeypatch.setattr(
        sigil_agent_io,
        "run_zeta_rpc_session",
        fake_run_zeta_rpc_session,
    )

    result = asyncio.run(
        sigil_agent_io.run_zeta_rpc_session(
            {"objective": "answer"},
            publish_event=published.append,
        )
    )

    assert result == {
        "turn_id": "turn-1",
        "outcome": "completed",
        "final_answer": "done",
    }
    assert captured["params"] == {"objective": "answer"}
    assert [
        zeta_event_model.draft_event_view(cast(DraftEvent, event))["type"]
        for event in published
    ] == ["seen"]


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

    asyncio.run(server.serve())

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


def test_zeta_session_run_params_capture_defaults_and_options() -> None:
    params = zeta_session.SessionRunParams.from_mapping(
        {
            "objective": "answer",
            "tools": ["read", "", 12, "bash"],
            "context": "existing notes",
            "model": "gpt-test",
            "max_steps": 3,
            "max_wall_seconds": 1,
        }
    )

    assert params.objective == "answer"
    assert params.workflow == "ask"
    assert params.tools == ("read", "", "bash")
    assert params.context == "existing notes"
    assert params.model == "gpt-test"
    assert params.max_steps == 3
    assert params.max_wall_seconds == 1.0


def test_zeta_event_trigger_rule_matches_exact_and_prefix() -> None:
    exact = zeta_dispatch.EventPattern("session.turn.requested")
    prefix = zeta_dispatch.EventPattern("github.issue.*")
    event = zeta_events.Event.from_draft(
        zeta_events.DraftEvent(
            "session.turn.requested",
            "test",
            {},
            session_id="session-1",
        )
    )

    assert exact.matches(event)
    assert not prefix.matches(event)
    assert prefix.matches(
        zeta_events.Event.from_draft(
            zeta_events.DraftEvent(
                "github.issue.opened",
                "test",
                {},
                session_id="session-1",
            )
        )
    )


def test_zeta_event_dispatcher_persists_unmatched_event(tmp_path: Path) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    published: list[zeta_events.Event] = []
    dispatcher = zeta_dispatch.EventDispatcher(
        event_store,
        publish_event=published.append,
    )

    outcome = dispatch_event(
        dispatcher,
        zeta_events.DraftEvent(
            "github.issue.opened",
            "github",
            {"title": "Bug"},
            session_id="repo",
        ),
    )

    assert outcome.inserted is True
    assert outcome.work_events == []
    assert [event.event_type for event in published] == ["github.issue.opened"]
    assert [
        event.event_type for event in event_store.list_events(zeta_events.Filter())
    ] == ["github.issue.opened"]


def test_zeta_event_dispatcher_creates_work_for_matching_agent(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    published: list[zeta_events.Event] = []
    seen: list[zeta_dispatch.AgentInvocation] = []

    def run_agent(run: zeta_dispatch.AgentInvocation) -> dict[str, object]:
        seen.append(run)
        return {"outcome": "handled"}

    dispatcher = zeta_dispatch.EventDispatcher(
        event_store,
        agents=[
            zeta_dispatch.RegisteredAgent(
                zeta_dispatch.AgentDefinition(
                    "issue-triage",
                    (zeta_dispatch.EventPattern("github.issue.*"),),
                ),
                run=run_agent,
            )
        ],
        publish_event=published.append,
    )

    outcome = dispatch_event(
        dispatcher,
        zeta_events.DraftEvent(
            "github.issue.opened",
            "github",
            {"title": "Bug"},
            session_id="repo",
            idempotency_key="github:event:1",
        ),
    )

    assert outcome.inserted is True
    assert len(seen) == 1
    assert seen[0].agent.agent_id == "issue-triage"
    assert seen[0].triggering_event.event_type == "github.issue.opened"
    assert [event.event_type for event in outcome.work_events] == [
        "runtime.work.pending",
        "runtime.work.claimed",
        "runtime.work.completed",
    ]
    assert {event.caused_by for event in outcome.work_events} == {outcome.event.id}
    assert [event.payload["agent_id"] for event in outcome.work_events] == [
        "issue-triage",
        "issue-triage",
        "issue-triage",
    ]
    assert outcome.agent_results == [{"outcome": "handled", "final_event_cursor": "4"}]
    assert [event.event_type for event in published] == [
        "github.issue.opened",
        "runtime.work.pending",
        "runtime.work.claimed",
        "runtime.work.completed",
    ]


def test_zeta_event_dispatcher_runs_matching_agents_in_task_group() -> None:
    async def run() -> None:
        event_store = zeta_events.MemoryEventStore()
        started: list[str] = []
        release = asyncio.Event()

        async def run_agent(run: zeta_dispatch.AgentInvocation) -> dict[str, Any]:
            started.append(run.agent.agent_id)
            if len(started) == 2:
                release.set()
            await release.wait()
            return {"outcome": "handled", "agent": run.agent.agent_id}

        dispatcher = zeta_dispatch.EventDispatcher(
            event_store,
            agents=[
                zeta_dispatch.RegisteredAgent(
                    zeta_dispatch.AgentDefinition(
                        "agent.one",
                        (zeta_dispatch.EventPattern("github.issue.opened"),),
                    ),
                    run=run_agent,
                ),
                zeta_dispatch.RegisteredAgent(
                    zeta_dispatch.AgentDefinition(
                        "agent.two",
                        (zeta_dispatch.EventPattern("github.issue.opened"),),
                    ),
                    run=run_agent,
                ),
            ],
        )

        outcome = await dispatcher.dispatch(
            zeta_events.DraftEvent("github.issue.opened", "github", {})
        )

        assert started == ["agent.one", "agent.two"]
        assert [event.event_type for event in outcome.work_events] == [
            "runtime.work.pending",
            "runtime.work.claimed",
            "runtime.work.completed",
            "runtime.work.pending",
            "runtime.work.claimed",
            "runtime.work.completed",
        ]
        assert [result["agent"] for result in outcome.agent_results] == [
            "agent.one",
            "agent.two",
        ]

    asyncio.run(run())


def test_zeta_event_dispatcher_matches_exact_event_type(tmp_path: Path) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    calls: list[str] = []

    dispatcher = zeta_dispatch.EventDispatcher(
        event_store,
        agents=[
            zeta_dispatch.RegisteredAgent(
                zeta_dispatch.AgentDefinition(
                    "exact-agent",
                    (zeta_dispatch.EventPattern("github.issue.opened"),),
                ),
                run=lambda run: {"outcome": calls.append(run.triggering_event.id)},
            ),
            zeta_dispatch.RegisteredAgent(
                zeta_dispatch.AgentDefinition(
                    "other-agent",
                    (zeta_dispatch.EventPattern("github.issue.closed"),),
                ),
                run=lambda run: {"outcome": calls.append(run.triggering_event.id)},
            ),
        ],
    )

    outcome = dispatch_event(
        dispatcher,
        zeta_events.DraftEvent(
            "github.issue.opened",
            "github",
            {"title": "Bug"},
            session_id="repo",
        ),
    )

    assert len(calls) == 1
    assert calls == [outcome.event.id]
    assert [event.payload["agent_id"] for event in outcome.work_events] == [
        "exact-agent",
        "exact-agent",
        "exact-agent",
    ]


def test_zeta_event_dispatcher_records_pending_work_without_runner(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    published: list[zeta_events.Event] = []
    dispatcher = zeta_dispatch.EventDispatcher(
        event_store,
        agents=[
            zeta_dispatch.RegisteredAgent(
                zeta_dispatch.AgentDefinition(
                    "issue-triage",
                    (zeta_dispatch.EventPattern("github.issue.opened"),),
                )
            )
        ],
        publish_event=published.append,
    )

    outcome = dispatch_event(
        dispatcher,
        zeta_events.DraftEvent(
            "github.issue.opened",
            "github",
            {"title": "Bug"},
            session_id="repo",
        ),
    )

    assert [event.event_type for event in outcome.work_events] == [
        "runtime.work.pending"
    ]
    assert outcome.agent_results == []
    assert [event.event_type for event in published] == [
        "github.issue.opened",
        "runtime.work.pending",
    ]


def test_zeta_event_dispatcher_does_not_route_duplicate_events(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    calls = 0

    def run_agent(run: zeta_dispatch.AgentInvocation) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"outcome": "handled"}

    dispatcher = zeta_dispatch.EventDispatcher(
        event_store,
        agents=[
            zeta_dispatch.RegisteredAgent(
                zeta_dispatch.AgentDefinition(
                    "issue-triage",
                    (zeta_dispatch.EventPattern("github.issue.opened"),),
                ),
                run=run_agent,
            )
        ],
    )
    draft = zeta_events.DraftEvent(
        "github.issue.opened",
        "github",
        {"title": "Bug"},
        session_id="repo",
        idempotency_key="github:event:1",
    )

    first = dispatch_event(dispatcher, draft)
    second = dispatch_event(dispatcher, draft)

    assert first.inserted is True
    assert second.inserted is False
    assert calls == 1
    assert second.work_events == []
    assert [
        event.event_type for event in event_store.list_events(zeta_events.Filter())
    ] == [
        "github.issue.opened",
        "runtime.work.pending",
        "runtime.work.claimed",
        "runtime.work.completed",
    ]


def test_zeta_event_dispatcher_records_failed_work(tmp_path: Path) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")

    def fail_agent(run: zeta_dispatch.AgentInvocation) -> dict[str, object]:
        del run
        raise RuntimeError("boom")

    dispatcher = zeta_dispatch.EventDispatcher(
        event_store,
        agents=[
            zeta_dispatch.RegisteredAgent(
                zeta_dispatch.AgentDefinition(
                    "issue-triage",
                    (zeta_dispatch.EventPattern("github.issue.opened"),),
                ),
                run=fail_agent,
            )
        ],
    )

    outcome = dispatch_event(
        dispatcher,
        zeta_events.DraftEvent(
            "github.issue.opened",
            "github",
            {"title": "Bug"},
            session_id="repo",
        ),
    )

    assert [event.event_type for event in outcome.work_events] == [
        "runtime.work.pending",
        "runtime.work.claimed",
        "runtime.work.failed",
    ]
    assert outcome.work_events[-1].payload["status"] == "failed"
    assert outcome.agent_results == [
        {"outcome": "failed", "error": "boom", "final_event_cursor": "4"}
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
    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda *args: True)
    monkeypatch.setattr(
        zeta_models_api,
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
    assert [event["type"] for event in published] == [
        "session.turn.requested",
        "runtime.work.pending",
        "runtime.work.claimed",
        "user_message",
        "model",
        "runtime.work.completed",
    ]
    assert messages[-1]["result"]["outcome"] == "completed"
    assert messages[-1]["result"]["final_answer"] == "done"
    assert messages[-1]["result"]["run_id"].startswith("run_")
    assert {event["run_id"] for event in published if "run_id" in event} == {
        messages[-1]["result"]["run_id"]
    }


def test_zeta_rpc_events_publish_triggers_session_turn(
    monkeypatch,
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    context = zeta_session.Session(
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
                "method": "events.publish",
                "params": {
                    "type": "session.turn.requested",
                    "payload": {
                        "objective": "answer",
                        "workflow": "ask",
                        "tools": [],
                        "context": "",
                    },
                    "session_id": "ctx-session",
                    "turn_id": "run_event",
                    "idempotency_key": "session.turn.requested:run_event",
                },
            }
        )
        + "\n"
    )
    output = StringIO()
    server = zeta_rpc.JsonRpcServer(
        input_stream,
        output,
        event_reader=event_store,
        event_sink=event_store,
    )
    server.event_dispatcher = zeta_rpc.session_event_dispatcher(
        context,
        publish_event=server.publish_event,
    )

    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda *args: True)
    monkeypatch.setattr(
        zeta_models_api,
        "chat_completion_messages",
        lambda *args, **kwargs: {"content": "done"},
    )

    asyncio.run(server.serve())

    messages = rpc_messages(output)
    response = messages[-1]
    published = [
        message["params"]["event"]
        for message in messages
        if message.get("method") == "events.publish"
    ]

    assert response["id"] == 1
    assert response["result"]["inserted"] is True
    assert response["result"]["event"]["type"] == "session.turn.requested"
    assert response["result"]["agent_results"][0]["outcome"] == "completed"
    assert [event["type"] for event in published] == [
        "session.turn.requested",
        "runtime.work.pending",
        "runtime.work.claimed",
        "user_message",
        "model",
        "runtime.work.completed",
    ]
    assert [
        event.event_type for event in event_store.list_events(zeta_events.Filter())
    ] == [
        "session.turn.requested",
        "runtime.work.pending",
        "runtime.work.claimed",
        "zeta.user_message",
        "zeta.model_call.completed",
        "runtime.work.completed",
    ]
    assert [
        event["type"]
        for event in server.list_events(
            {"session_id": "ctx-session", "run_id": "run_event"}
        )["events"]
    ] == [
        "session.turn.requested",
        "runtime.work.pending",
        "runtime.work.claimed",
        "user_message",
        "model",
        "runtime.work.completed",
    ]


def test_zeta_rpc_session_uses_explicit_context(monkeypatch, tmp_path: Path) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    trace_store = zeta_trace.InMemoryStore()
    context = zeta_session.Session(
        session_id="ctx-session",
        event_sink=event_store,
        trace_store=trace_store,
        tool_registry=CapabilityRegistry(),
        state_dir=tmp_path,
        session_dir=tmp_path / "sessions" / "ctx-session",
    )
    published: list[Event | DraftEvent] = []

    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda *args: True)
    monkeypatch.setattr(
        zeta_models_api,
        "chat_completion_messages",
        lambda *args, **kwargs: {"content": "done"},
    )

    result = run_rpc_session(
        {"objective": "answer", "tools": [], "context": ""},
        publish_event=published.append,
        runtime_context=context,
    )

    assert result["outcome"] == "completed"
    assert result["final_answer"] == "done"
    assert result["run_id"].startswith("run_")
    assert result["final_event_cursor"] == "6"
    published_views = published_event_views(published)
    assert [event["type"] for event in published_views] == [
        "session.turn.requested",
        "runtime.work.pending",
        "runtime.work.claimed",
        "user_message",
        "model",
        "runtime.work.completed",
    ]
    assert {event["session"] for event in published_views} == {"ctx-session"}
    assert {event["run_id"] for event in published_views if "run_id" in event} == {
        result["run_id"]
    }
    assert [event["cursor"] for event in published_views] == [
        "1",
        "2",
        "3",
        "4",
        "5",
        "6",
    ]
    assert {event["turn_id"] for event in published_views} == {result["run_id"]}
    assert [
        event.event_type for event in event_store.list_events(zeta_events.Filter())
    ] == [
        "session.turn.requested",
        "runtime.work.pending",
        "runtime.work.claimed",
        "zeta.user_message",
        "zeta.model_call.completed",
        "runtime.work.completed",
    ]
    assert [
        event.turn_id
        for event in event_store.list_events(
            zeta_events.Filter(turn_id=result["run_id"])
        )
    ] == [
        result["run_id"],
        result["run_id"],
        result["run_id"],
        result["run_id"],
        result["run_id"],
        result["run_id"],
    ]
    assert trace_store.objects(kind="run_event") == []
    ref_names = {ref.name for ref in trace_store.refs()}
    assert "run/ctx-session/head" not in ref_names
    assert "run/ctx-session/event_head" not in ref_names


def test_zeta_rpc_session_result_returns_prompt_trace_refs(
    monkeypatch,
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    trace_store = zeta_trace.InMemoryStore()
    context = zeta_session.Session(
        session_id="ctx-session",
        event_sink=event_store,
        trace_store=trace_store,
        tool_registry=CapabilityRegistry(),
        state_dir=tmp_path,
        session_dir=tmp_path / "sessions" / "ctx-session",
    )

    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda *args: True)
    monkeypatch.setattr(
        zeta_models_api,
        "chat_completion_messages",
        lambda *args, **kwargs: {"content": "done"},
    )

    result = run_rpc_session(
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
    context = zeta_session.Session(
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

    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda *args: True)
    monkeypatch.setattr(
        zeta_models_api,
        "chat_completion_messages",
        lambda *args, **kwargs: next(responses),
    )

    result = run_rpc_session(
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
    context = zeta_session.Session(
        session_id="ctx-session",
        event_sink=event_store,
        trace_store=zeta_trace.InMemoryStore(),
        tool_registry=CapabilityRegistry(),
        state_dir=tmp_path,
        session_dir=tmp_path / "sessions" / "ctx-session",
    )

    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda *args: True)
    monkeypatch.setattr(
        zeta_models_api,
        "chat_completion_messages",
        lambda *args, **kwargs: {"content": "done"},
    )
    result = run_rpc_session(
        {"objective": "answer", "tools": [], "context": ""},
        publish_event=lambda event: None,
        runtime_context=context,
    )

    trace = result["trace"]
    assert len(trace["prompt_ids"]) == 1
    assert len(trace["assistant_message_ids"]) == 1
    assert len(trace["model_event_ids"]) == 1
    assert trace["tool_event_ids"] == []
    assert trace["tool_call_ids"] == []
    assert trace["tool_result_ids"] == []


def test_zeta_rpc_sequential_runs_get_distinct_run_ids(
    monkeypatch,
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    context = zeta_session.Session(
        session_id="ctx-session",
        event_sink=event_store,
        trace_store=zeta_trace.InMemoryStore(),
        tool_registry=CapabilityRegistry(),
        state_dir=tmp_path,
        session_dir=tmp_path / "sessions" / "ctx-session",
    )

    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda *args: True)
    monkeypatch.setattr(
        zeta_models_api,
        "chat_completion_messages",
        lambda *args, **kwargs: {"content": "done"},
    )

    first = run_rpc_session(
        {"objective": "first", "tools": [], "context": ""},
        publish_event=lambda event: None,
        runtime_context=context,
    )
    second = run_rpc_session(
        {"objective": "second", "tools": [], "context": ""},
        publish_event=lambda event: None,
        runtime_context=context,
    )

    assert first["run_id"].startswith("run_")
    assert second["run_id"].startswith("run_")
    assert first["run_id"] != second["run_id"]
    assert first["final_event_cursor"] == "6"
    assert second["final_event_cursor"] == "12"


def test_zeta_rpc_session_returns_aborted_on_wall_clock_budget(
    monkeypatch,
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    context = zeta_session.Session(
        session_id="ctx-session",
        event_sink=event_store,
        trace_store=zeta_trace.InMemoryStore(),
        tool_registry=CapabilityRegistry(),
        state_dir=tmp_path,
        session_dir=tmp_path / "sessions" / "ctx-session",
    )
    published: list[Event | DraftEvent] = []

    def fail_chat_completion_messages(*args: object, **kwargs: object) -> dict:
        raise AssertionError("expired turn must not request the model")

    monkeypatch.setattr(zeta_agent, "time_monotonic", lambda: 10.0)
    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda *args: True)
    monkeypatch.setattr(
        zeta_models_api, "chat_completion_messages", fail_chat_completion_messages
    )

    result = run_rpc_session(
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
    assert result["final_answer"] == ""
    assert result["run_id"].startswith("run_")
    assert result["final_event_cursor"] == "6"
    published_views = published_event_views(published)
    assert [event["type"] for event in published_views] == [
        "session.turn.requested",
        "runtime.work.pending",
        "runtime.work.claimed",
        "user_message",
        "turn_aborted",
        "runtime.work.cancelled",
    ]
    assert {event["run_id"] for event in published_views if "run_id" in event} == {
        result["run_id"]
    }
    assert published_views[-2]["reason"] == "deadline_exceeded"
    assert [
        event.event_type for event in event_store.list_events(zeta_events.Filter())
    ] == [
        "session.turn.requested",
        "runtime.work.pending",
        "runtime.work.claimed",
        "zeta.user_message",
        "zeta.turn.failed",
        "runtime.work.cancelled",
    ]


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
        cancellation_event=asyncio.Event(),
        status="completed",
    )

    assert server.cancel_session({"run_id": "run_done"}) == {
        "cancelled": False,
        "run_id": "run_done",
        "status": "completed",
    }


def test_zeta_async_rpc_session_cancel_sets_async_cancellation_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        input_stream = StringIO(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "session.run",
                    "params": {"objective": "wait"},
                }
            )
            + "\n"
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session.cancel",
                    "params": {"run_id": "run_async_cancel"},
                }
            )
            + "\n"
        )
        output = StringIO()

        async def session_runner(params: dict[str, Any]) -> dict[str, Any]:
            cancellation_event = params["_zeta_cancellation_event"]
            await cancellation_event.wait()
            return {
                "run_id": params["_zeta_run_id"],
                "outcome": "aborted",
                "final_answer": "",
            }

        server = zeta_rpc.JsonRpcServer(
            input_stream,
            output,
            session_runner=session_runner,
        )

        monkeypatch.setattr(zeta_rpc, "rpc_run_id", lambda: "run_async_cancel")
        await server.serve()

        messages = rpc_messages(output)
        cancel_response = next(
            message for message in messages if message.get("id") == 2
        )
        run_response = next(message for message in messages if message.get("id") == 1)

        assert cancel_response["result"] == {
            "cancelled": True,
            "run_id": "run_async_cancel",
        }
        assert run_response["result"]["outcome"] == "aborted"
        assert run_response["result"]["run_id"] == "run_async_cancel"

    asyncio.run(run())


def test_zeta_async_rpc_session_cancel_cancels_native_async_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        input_stream = StringIO(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "session.run",
                    "params": {"objective": "wait"},
                }
            )
            + "\n"
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session.cancel",
                    "params": {"run_id": "run_native_cancel"},
                }
            )
            + "\n"
        )
        output = StringIO()
        cancelled = asyncio.Event()

        async def session_runner(params: dict[str, Any]) -> dict[str, Any]:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return {
                "run_id": params["_zeta_run_id"],
                "outcome": "completed",
            }

        server = zeta_rpc.JsonRpcServer(
            input_stream,
            output,
            session_runner=session_runner,
        )

        monkeypatch.setattr(zeta_rpc, "rpc_run_id", lambda: "run_native_cancel")
        await server.serve()

        messages = rpc_messages(output)
        cancel_response = next(
            message for message in messages if message.get("id") == 2
        )
        run_response = next(message for message in messages if message.get("id") == 1)

        assert cancelled.is_set()
        assert cancel_response["result"] == {
            "cancelled": True,
            "run_id": "run_native_cancel",
        }
        assert run_response["result"] == {
            "run_id": "run_native_cancel",
            "outcome": "aborted",
            "final_answer": "",
        }

    asyncio.run(run())


def test_zeta_async_rpc_session_cancel_keeps_cooperative_task_runner_alive() -> None:
    async def run() -> None:
        output = StringIO()
        server = zeta_rpc.JsonRpcServer(StringIO(), output)
        released = asyncio.Event()
        completed = asyncio.Event()

        async def cooperative_runner(params: dict[str, Any]) -> dict[str, Any]:
            await released.wait()
            completed.set()
            return {
                "run_id": params["_zeta_run_id"],
                "outcome": "aborted",
                "final_answer": "",
            }

        cast(Any, cooperative_runner).__rpc_cancel_mode__ = "cooperative"
        state = zeta_rpc.RpcRunState(
            run_id="run_cooperative_cancel",
            request_id=1,
            cancellation_event=asyncio.Event(),
            cancel_mode="cooperative",
        )
        server.runs[state.run_id] = state
        server.session_runner = cooperative_runner

        state.task = asyncio.create_task(
            server.complete_session_run(
                state,
                {
                    "_zeta_run_id": state.run_id,
                    "_zeta_cancellation_event": state.cancellation_event,
                },
            )
        )
        cancel_result = server.cancel_session({"run_id": state.run_id})

        assert cancel_result == {"cancelled": True, "run_id": state.run_id}
        assert state.cancellation_event.is_set()
        assert state.task is not None
        assert not state.task.cancelled()

        released.set()
        await state.task

        assert completed.is_set()
        assert state.status == "cancelled"

    asyncio.run(run())


def test_zeta_rpc_registers_client_tool_on_server_registry() -> None:
    registry = CapabilityRegistry()
    server = zeta_rpc.JsonRpcServer(StringIO(), StringIO(), tool_registry=registry)

    registered = server.register_client_tools(
        [
            {
                "name": "ctx_read",
                "description": "Read through the client.",
                "schema": {"type": "object"},
            }
        ]
    )

    assert registered == [
        {
            "id": "rpc.ctx_read",
            "provider": "rpc",
            "name": "ctx_read",
            "description": "Read through the client.",
            "input_schema": {"type": "object"},
        }
    ]
    assert registry.get("rpc.ctx_read") is not None
    assert registry.get_by_name("ctx_read") is not None
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
            "description": "Write through the client.",
            "input_schema": {"type": "object"},
            "timeout_sec": 2.5,
        }
    ]
    assert capability is not None
    assert {
        "id": capability.declaration.id.canonical(),
        "provider": capability.declaration.id.provider,
        "name": capability.declaration.id.name,
        "description": capability.declaration.description,
        "input_schema": capability.declaration.input_schema,
    } == {
        "id": "rpc.client.write",
        "provider": "rpc",
        "name": "client.write",
        "description": "Write through the client.",
        "input_schema": {"type": "object"},
    }
    assert capability.executor.timeout_seconds == 2.5


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

    asyncio.run(server.serve())

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

    asyncio.run(server.serve())

    message = rpc_messages(output)[0]
    assert message["error"]["code"] == -32602
    assert message["error"]["message"] == "Invalid params"
    assert message["error"]["data"]["code"] == "invalid_tool_schema"
    assert message["error"]["data"]["tool"] == "client.bad"


def test_zeta_rpc_direct_only_client_tool_runs_in_stage_mode() -> None:
    registry = CapabilityRegistry()
    server = zeta_rpc.JsonRpcServer(StringIO(), StringIO(), tool_registry=registry)
    server.register_client_tools(
        [
            {
                "name": "client.write",
                "description": "Write through the client.",
                "schema": {"type": "object"},
            }
        ]
    )

    result = registry.invoke("client.write", {}, execution_mode="stage")

    assert result["ok"] is False
    assert result["error"]["code"] == "client-disconnected"


def test_zeta_rpc_client_tool_registers_without_execution_policy() -> None:
    registry = CapabilityRegistry()
    server = zeta_rpc.JsonRpcServer(StringIO(), StringIO(), tool_registry=registry)

    registered = server.register_client_tools(
        [
            {
                "name": "client.write",
                "description": "Write through the client.",
                "schema": {"type": "object"},
            }
        ]
    )

    assert registered[0]["id"] == "rpc.client.write"


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

    asyncio.run(server.serve())

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
                        "description": "Read through the client.",
                        "input_schema": {"type": "object"},
                    }
                ]
            },
        }
    ]
    assert registry.get("test.ctx_read") is not None
    assert registry.get("rpc.ctx_read") is not None


def test_zeta_rpc_client_alias_collision_is_rejected_at_projection_time() -> None:
    registry = CapabilityRegistry()
    registry.register(_test_capability("read", provider="sigil"))
    server = zeta_rpc.JsonRpcServer(StringIO(), StringIO(), tool_registry=registry)

    server.register_client_tools(
        [
            {
                "name": "read",
                "description": "Read through the client.",
                "schema": {"type": "object"},
            }
        ]
    )

    assert registry.get("sigil.read") is not None
    assert registry.get("rpc.read") is not None
    with pytest.raises(ValueError, match="ambiguous capability name 'read'"):
        registry.project(("sigil.read", "rpc.read"))


def test_zeta_agent_auto_enabled_capabilities_include_registered_tools() -> None:
    registry = CapabilityRegistry()
    registry.register(_test_capability("read", provider="host"))
    registry.register(
        _test_capability(
            "write",
            provider="rpc",
            with_stage_executor=True,
        )
    )

    assert zeta_agent.registered_capabilities(None, tool_registry=registry) == (
        "host.read",
        "rpc.write",
    )
    assert zeta_agent.registered_capabilities(("write",), tool_registry=registry) == (
        "rpc.write",
    )


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

    asyncio.run(server.serve())

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
                        },
                        {
                            "name": "client.echo",
                            "description": "Echo from the client.",
                            "schema": {"type": "object"},
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

    asyncio.run(server.serve())

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

    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda *args: True)

    def fake_chat_completion_messages(*args: Any, **kwargs: Any) -> dict[str, Any]:
        stream = kwargs.get("stream_sink")
        assert stream is not None
        stream.content_delta("do")
        stream.content_delta("ne")
        return {"content": "done"}

    monkeypatch.setattr(
        zeta_models_api,
        "chat_completion_messages",
        fake_chat_completion_messages,
    )

    server = zeta_rpc.JsonRpcServer(input_stream, output)
    server.session_runner = lambda params: run_zeta_rpc_session(
        params,
        publish_event=server.publish_event,
    )

    asyncio.run(server.serve())

    messages = rpc_messages(output)
    published = [
        message["params"]["event"]
        for message in messages
        if message.get("method") == "events.publish"
    ]
    response = messages[-1]

    assert [event["type"] for event in published] == [
        "user_message",
        "runtime.stream.chunk",
        "runtime.stream.chunk",
        "model",
        "turn.completed",
    ]
    assert response["id"] == 1
    assert response["result"]["outcome"] == "completed"
    assert response["result"]["turn_id"]
    event_store = SqliteEventStore(event_store_path())
    try:
        model_events = event_store.list_events(
            Filter(event_type="zeta.model_call.completed")
        )
        stream_events = event_store.list_events(
            Filter(event_type="runtime.stream.chunk")
        )
    finally:
        event_store.close()
    assert len(model_events) == 1
    assert stream_events == []
    assert model_events[0].id == published[3]["id"]
    assert model_events[0].caused_by != published[0]["id"]


def test_zeta_rpc_events_list_pages_in_append_order(tmp_path: Path) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    for content in ("one", "two", "three"):
        event_store.accept(
            DraftEvent(
                event_type="zeta.user_message",
                source="zeta",
                payload={
                    "_timeline_type": "user_message",
                    "content": content,
                    "run_id": "run_1",
                },
                session_id="session-1",
                turn_id="run_1",
                caused_by=None,
                idempotency_key=None,
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

    asyncio.run(server.serve())

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
            DraftEvent(
                event_type="zeta.user_message",
                source="zeta",
                payload={
                    "_timeline_type": "user_message",
                    "content": content,
                    "run_id": run_id,
                },
                session_id=session_id,
                turn_id=run_id,
                caused_by=None,
                idempotency_key=None,
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

    asyncio.run(server.serve())

    assert [
        event["content"] for event in rpc_messages(output)[0]["result"]["events"]
    ] == [
        "one",
        "three",
    ]


def test_zeta_rpc_event_list_params_reject_invalid_cursor() -> None:
    with pytest.raises(zeta_rpc.RpcError) as raised:
        zeta_rpc.EventListParams.from_mapping({"after": 1})

    assert raised.value.jsonrpc_code == -32602
    assert raised.value.error_data() == {
        "code": "invalid_cursor",
        "message": "after must be an event cursor string",
    }


def test_zeta_rpc_event_list_params_reject_invalid_limit() -> None:
    with pytest.raises(zeta_rpc.RpcError) as raised:
        zeta_rpc.EventListParams.from_mapping({"limit": 0})

    assert raised.value.jsonrpc_code == -32602
    assert raised.value.error_data() == {
        "code": "invalid_limit",
        "message": "limit must be a positive integer",
    }


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

    asyncio.run(server.serve())
    server.publish_event(rpc_event("old", cursor=1))
    server.publish_event(rpc_event("new", cursor=2))

    messages = rpc_messages(output)
    assert messages[0]["result"]["subscription_id"].startswith("sub_")
    assert [
        message["params"]["event"]["content"]
        for message in messages
        if message.get("method") == "events.publish"
    ] == ["new"]


def test_zeta_rpc_session_cancel_params_require_run_id() -> None:
    with pytest.raises(zeta_rpc.RpcError) as raised:
        zeta_rpc.SessionCancelParams.from_mapping({"run_id": ""})

    assert raised.value.jsonrpc_code == -32602
    assert raised.value.error_data() == {
        "code": "invalid_run_id",
        "message": "run_id must be a non-empty string",
    }


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

    asyncio.run(server.serve())
    server.publish_event(
        rpc_event("wrong-session", cursor=1, session_id="session-2", turn_id="run_1")
    )
    server.publish_event(
        rpc_event("wrong-run", cursor=2, session_id="session-1", turn_id="run_2")
    )
    server.publish_event(
        rpc_event("match", cursor=3, session_id="session-1", turn_id="run_1")
    )

    assert [
        message["params"]["event"]["content"]
        for message in rpc_messages(output)
        if message.get("method") == "events.publish"
    ] == ["match"]


def test_zeta_rpc_publish_event_keeps_default_stream_without_subscription() -> None:
    output = StringIO()
    server = zeta_rpc.JsonRpcServer(StringIO(), output)

    server.publish_event(rpc_event("live", cursor=1))

    [message] = rpc_messages(output)
    assert message["jsonrpc"] == "2.0"
    assert message["method"] == "events.publish"
    assert message["params"]["event"]["type"] == "user_message"
    assert message["params"]["event"]["content"] == "live"
    assert message["params"]["event"]["cursor"] == "1"


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

    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api,
        "chat_completion_messages",
        lambda *args, **kwargs: next(responses),
    )

    result = run_agent_turn(
        "echo",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("ctx_echo",), max_turns=2),
        tool_registry=registry,
    )

    assert zeta_agent.tool_registry.get("ctx_echo") is None
    assert result.final_answer == "done"
    assert [
        event.get("name") for event in timeline_events(result.events) if "name" in event
    ] == [
        "ctx_echo",
        "ctx_echo",
    ]


def test_zeta_agent_turn_resolves_model_name_through_projection(monkeypatch) -> None:
    registry = CapabilityRegistry()
    registry.register(_test_capability("read", provider="host"))
    registry.register(_test_capability("read", provider="rpc"))
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

    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api,
        "chat_completion_messages",
        lambda *args, **kwargs: next(responses),
    )
    monkeypatch.setattr(zeta_agent, "invoke_capability", fake_invoke)

    result = run_agent_turn(
        "read",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("host.read",), max_turns=2),
        tool_registry=registry,
    )

    assert result.final_answer == "done"
    assert invoked == [("host.read", {"path": "README.md"})]
    tool_call = next(
        event
        for event in timeline_events(result.events)
        if event["type"] == "tool_call"
    )
    tool_result = next(
        event
        for event in timeline_events(result.events)
        if event["type"] == "tool_result"
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

    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api, "chat_completion_messages", fake_chat_completion_messages
    )

    run_agent_turn(
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

    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api, "chat_completion_messages", fake_chat_completion_messages
    )

    result = run_agent_turn(
        "read",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("read",), max_turns=1),
        prompt_builder=zeta_context.PromptBuilder(store=store),
        caused_by="prompt-event",
    )

    assistant = event_by_type(result.events, "model")
    tool_call = event_by_type(result.events, "tool_call")
    tool_result = event_by_type(result.events, "tool_result")
    assert assistant["id"]
    assert assistant["caused_by"] == "prompt-event"
    assert tool_call["caused_by"] == assistant["id"]
    assert tool_result["caused_by"] == assistant["id"]
    assert projected_tool_call_object_id(store, tool_call)


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

    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api, "chat_completion_messages", fake_chat_completion_messages
    )

    result = run_agent_turn(
        "answer",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("read",), max_turns=1),
        trace_store=store,
    )

    assert result.final_answer == "done"
    assert timeline_events(result.events)[0]["type"] == "model"
    assert timeline_events(result.events)[0]["content"] == "done"
    assert timeline_events(result.events)[0]["prompt_object_id"]
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

    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api, "chat_completion_messages", fake_chat_completion_messages
    )

    result = run_agent_turn(
        "answer",
        [{"role": "user", "content": "prior"}],
        zeta_agent.AgentConfig(
            allowed_capabilities=("read",),
            max_turns=1,
            model_name="unit-model",
        ),
        context="Project context",
        prompt_builder=zeta_context.PromptBuilder(store=store),
    )

    assert len(result.prompt_traces) == 1
    trace = result.prompt_traces[0]
    prompt = store.get_object(trace.prompt_object_id)
    assert prompt is not None
    kwargs = cast(dict[str, Any], captured["kwargs"])
    assert prompt.data["payload_sha256"] == zeta_context.payload_sha256(
        zeta_model.chat_completion_request_body(
            cast(list[dict[str, Any]], captured["messages"]),
            tools=cast(list[dict[str, Any]], kwargs["tools"]),
            tool_choice=cast(str, kwargs["tool_choice"]),
            selected_model="unit-model",
        )
    )
    reconstructed = assert_prompt_trace_replay_graph(store, trace)
    assert reconstructed.messages == captured["messages"]
    assistant = store.get_object(cast(str, trace.assistant_message_object_id))
    assert assistant is not None
    assert assistant.kind == "assistant_message"
    assert assistant.links == (trace.prompt_object_id,)
    assert assistant.data["message"] == {"content": "done"}
    assert timeline_events(result.events)[0]["prompt_object_id"] == (
        trace.prompt_object_id
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

    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api, "chat_completion_messages", fake_chat_completion_messages
    )

    result = run_agent_turn(
        "answer",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("read",), max_turns=1),
    )

    assert result.final_answer == "done"
    assert result.telemetry == {
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

    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api, "chat_completion_messages", fake_chat_completion_messages
    )

    result = run_agent_turn(
        "inspect",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("read",), max_turns=2),
    )

    tool_results = [
        event
        for event in timeline_events(result.events)
        if event.get("type") == "tool_result"
    ]
    assert tool_results[0]["model_telemetry"] == tool_telemetry
    assert "model_telemetry" not in tool_results[1]
    assert result.telemetry == final_telemetry


def test_zeta_agent_turn_records_one_prompt_trace_per_model_request(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "README.md"
    target.write_text("README\n", encoding="utf-8")
    store = zeta_trace.InMemoryStore()
    responses = iter([read_tool_call_response(target), {"content": "done"}])

    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api,
        "chat_completion_messages",
        lambda messages, **kwargs: next(responses),
    )
    monkeypatch.setattr(
        zeta_agent,
        "invoke_capability",
        lambda name, params: read_tool_payload(target),
    )

    result = run_agent_turn(
        "inspect",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("read",), max_turns=2),
        prompt_builder=zeta_context.PromptBuilder(store=store),
    )

    assert result.final_answer == "done"
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

    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api,
        "chat_completion_messages",
        lambda messages, **kwargs: next(responses),
    )
    monkeypatch.setattr(
        zeta_agent,
        "invoke_capability",
        lambda name, params: read_tool_payload(target),
    )

    result = run_agent_turn(
        "inspect",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("read",), max_turns=2),
        prompt_builder=zeta_context.PromptBuilder(store=store),
    )

    assert_tool_result_derivation_graph(
        store,
        result,
        event_by_type(result.events, "tool_call"),
        event_by_type(result.events, "tool_result"),
    )
    for trace in result.prompt_traces:
        assert_prompt_trace_replay_graph(store, trace)


def test_zeta_agent_turn_emits_stream_chunks_and_marks_final(monkeypatch) -> None:
    emitted: list[DraftEvent] = []

    def fake_chat_completion_messages(
        *args: object,
        **kwargs: object,
    ) -> dict[str, Any]:
        del args
        stream_sink = required_stream_sink(kwargs)
        stream_sink.content_delta("hel")
        stream_sink.content_delta("lo")
        return {"content": "hello"}

    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api,
        "chat_completion_messages",
        fake_chat_completion_messages,
    )

    result = run_agent_turn(
        "answer",
        [],
        zeta_agent.AgentConfig(max_turns=1),
        event_sink=emitted.append,
    )

    stream_chunks = [
        draft for draft in emitted if draft.event_type == "runtime.stream.chunk"
    ]
    assert [draft.payload["text"] for draft in stream_chunks] == ["hel", "lo"]
    assert result.final_answer == "hello"
    assert result.answer_streamed is True


def test_zeta_agent_reasoning_deltas_emit_status_updates(monkeypatch) -> None:
    emitted: list[DraftEvent] = []

    def fake_chat_completion_messages(
        *args: object,
        **kwargs: object,
    ) -> dict[str, Any]:
        del args
        stream_sink = required_stream_sink(kwargs)
        stream_sink.reasoning_delta("mull")
        stream_sink.content_delta("done")
        return {"content": "done"}

    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api,
        "chat_completion_messages",
        fake_chat_completion_messages,
    )

    result = run_agent_turn(
        "answer",
        [],
        zeta_agent.AgentConfig(max_turns=1),
        event_sink=emitted.append,
    )

    assert result.final_answer == "done"
    status_updates = [
        draft for draft in emitted if draft.event_type == "runtime.status.update"
    ]
    assert [draft.payload["text"] for draft in status_updates] == ["mull"]


def test_zeta_agent_runtime_ui_events_do_not_feed_next_prompt(monkeypatch) -> None:
    captured: list[list[dict[str, Any]]] = []
    responses = iter(
        [
            {"content": "streaming answer", "tool_calls": tool_call_fixture()},
            {"content": "done"},
        ]
    )

    def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        **kwargs: object,
    ) -> dict[str, Any]:
        captured.append(messages)
        stream_sink = required_stream_sink(kwargs)
        stream_sink.content_delta("streaming answer")
        return next(responses)

    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api,
        "chat_completion_messages",
        fake_chat_completion_messages,
    )
    monkeypatch.setattr(
        zeta_agent,
        "invoke_capability",
        lambda name, params: {"ok": True, "content": [{"type": "text", "text": name}]},
    )

    result = run_agent_turn(
        "answer",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("read",), max_turns=2),
    )

    assert result.final_answer == "done"
    assert all("runtime.stream.chunk" not in str(message) for message in captured[1])


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

    monkeypatch.setattr(zeta_model, "model_endpoint_open", fake_model_endpoint_open)
    monkeypatch.setattr(
        zeta_models_api, "chat_completion_messages", fake_chat_completion_messages
    )

    result = run_agent_turn(
        "answer",
        [],
        zeta_agent.AgentConfig(
            allowed_capabilities=("read",),
            max_turns=1,
            model_name="fast-model",
            model_url="http://127.0.0.1:8081/v1/chat/completions",
        ),
    )

    assert result.final_answer == "done"
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

    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api,
        "chat_completion_messages",
        lambda *args, **kwargs: next(responses),
    )

    def fake_invoke(
        name: str, params: dict[str, Any], **kwargs: object
    ) -> dict[str, Any]:
        ran.append((name, params))
        return {"ok": True, "content": [{"type": "text", "text": name}]}

    monkeypatch.setattr(zeta_agent, "invoke_capability", fake_invoke)

    result = run_agent_turn(
        "inspect",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("read", "ls"), max_turns=2),
        caused_by="prompt-event",
    )

    assert ran == [
        ("sigil.read", {"path": "README.md"}),
        ("sigil.ls", {"path": "src"}),
    ]
    assert result.final_answer == "done"
    assert [
        event["name"]
        for event in timeline_events(result.events)
        if event.get("type") == "tool_call"
    ] == ["read", "ls"]
    model_events = [
        event
        for event in timeline_events(result.events)
        if event.get("type") == "model"
    ]
    tool_results = [
        event
        for event in timeline_events(result.events)
        if event.get("type") == "tool_result"
    ]
    assert model_events[0]["caused_by"] == "prompt-event"
    assert tool_results[0]["caused_by"] == model_events[0]["id"]
    assert tool_results[1]["caused_by"] == model_events[0]["id"]
    assert model_events[1]["caused_by"] == tool_results[1]["id"]


def test_zeta_agent_turn_streams_text_between_tool_turns(monkeypatch) -> None:
    emitted: list[DraftEvent] = []
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

    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api,
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

    result = run_agent_turn(
        "inspect",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("read",), max_turns=2),
        event_sink=emitted.append,
    )

    stream_chunks = [
        draft for draft in emitted if draft.event_type == "runtime.stream.chunk"
    ]
    assert [draft.payload["text"] for draft in stream_chunks] == [
        "I'll inspect README.",
        "It is a README.",
    ]
    assert result.final_answer == "It is a README."
    assert result.answer_streamed is True
    model_events = [
        event
        for event in timeline_events(result.events)
        if event.get("type") == "model"
    ]
    assert model_events[0]["content"] == "I'll inspect README."


def test_zeta_agent_turn_does_not_duplicate_current_objective(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        **kwargs: object,
    ) -> dict[str, Any]:
        del kwargs
        captured["messages"] = messages
        return {"content": "done"}

    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api,
        "chat_completion_messages",
        fake_chat_completion_messages,
    )

    result = run_agent_turn(
        "inspect the repo",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("read",), max_turns=1),
    )

    assert result.final_answer == "done"
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

    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api,
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

    result = run_agent_turn(
        "How would you improve it?",
        [
            {"role": "user", "content": "What is this vault about?"},
            {"role": "assistant", "content": "It is a CEO vault."},
        ],
        zeta_agent.AgentConfig(allowed_capabilities=("read",), max_turns=2),
    )

    assert result.final_answer == "Improve the decision log."
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
    streamed: list[DraftEvent] = []

    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api,
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
        assert [event.get("type") for event in timeline_events(streamed)] == [
            "model",
            "tool_call",
        ]
        return {"ok": True, "content": [{"type": "text", "text": "README"}]}

    monkeypatch.setattr(zeta_agent, "invoke_capability", fake_invoke)

    result = run_agent_turn(
        "inspect",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("read",), max_turns=1),
        event_sink=streamed.append,
    )

    assert result.events == streamed
    assert [event.get("type") for event in timeline_events(streamed)] == [
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
    store = zeta_trace.InMemoryStore()

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

    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api, "chat_completion_messages", fake_chat_completion_messages
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

    result = run_agent_turn(
        "test",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("bash",), max_turns=3),
        prompt_builder=zeta_context.PromptBuilder(store=store),
    )

    assert requests == 1
    assert result.staged_effect == {
        "kind": "command",
        "status": "proposed",
        "command": "uv run pytest",
        "reason": "Run tests.",
    }
    assert len(result.prompt_traces) == 1
    assert_prompt_trace_replay_graph(store, result.prompt_traces[0])
    tool_call = event_by_type(result.events, "tool_call")
    tool_result = event_by_type(result.events, "tool_result")
    call_object_id = projected_tool_call_object_id(store, tool_call)
    result_object_id = projected_tool_result_object_id(store, tool_result)
    assert_tool_call_derivation(store, result, call_object_id)
    assert_tool_result_derivation(
        store,
        call_object_id,
        result_object_id,
    )


def test_zeta_agent_turn_stops_after_staged_effect(
    monkeypatch,
) -> None:
    requests = 0
    registry = CapabilityRegistry()
    registry.register(
        _test_capability(
            "mutate",
            with_stage_executor=True,
        )
    )

    def fake_chat_completion_messages(
        *args: object,
        **kwargs: object,
    ) -> dict[str, Any]:
        nonlocal requests
        requests += 1
        return {
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "mutate", "arguments": "{}"},
                }
            ]
        }

    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api,
        "chat_completion_messages",
        fake_chat_completion_messages,
    )

    result = run_agent_turn(
        "mutate",
        [],
        zeta_agent.AgentConfig(
            allowed_capabilities=("mutate",),
            max_turns=3,
            stop_on_staged_effect=False,
        ),
        tool_registry=registry,
    )

    assert requests == 1
    assert result.final_answer == ""
    assert result.staged_effect is None
    assert [event["type"] for event in timeline_events(result.events)] == [
        "model",
        "tool_call",
        "tool_result",
    ]


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

    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api, "chat_completion_messages", fake_chat_completion_messages
    )

    result = run_agent_turn(
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
    assert result.final_answer == "done"
    tool_result = next(
        event
        for event in timeline_events(result.events)
        if event.get("type") == "tool_result"
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

    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api, "chat_completion_messages", fake_chat_completion_messages
    )
    monkeypatch.setattr(
        zeta_agent, "invoke_capability", lambda name, params, **kwargs: {"ok": True}
    )

    result = run_agent_turn(
        "test",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("ls",)),
    )

    assert requests == zeta_agent.DEFAULT_MAX_TURNS
    assert result.final_answer == ""


def test_zeta_agent_turn_aborts_before_model_when_cancelled(monkeypatch) -> None:
    cancellation = threading.Event()
    cancellation.set()
    events: list[DraftEvent] = []

    def fail_chat_completion_messages(*args: object, **kwargs: object) -> dict:
        raise AssertionError("cancelled turn must not request the model")

    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api, "chat_completion_messages", fail_chat_completion_messages
    )

    with pytest.raises(zeta_agent.AgentTurnAborted) as raised:
        run_agent_turn(
            "test",
            [],
            zeta_agent.AgentConfig(allowed_capabilities=("ls",), max_turns=1),
            event_sink=events.append,
            cancellation_event=cancellation,
            caused_by="prompt-event",
        )

    assert raised.value.reason == "cancelled"
    assert raised.value.result.events == events
    assert [step.step for step in raised.value.result.steps] == [
        "check_budget",
        "abort_run",
    ]
    projected = timeline_events(events)
    assert projected == [
        {
            "type": "turn_aborted",
            "id": projected[0]["id"],
            "reason": "cancelled",
            "content": "(turn aborted: cancelled)",
            "caused_by": "prompt-event",
            "time": projected[0]["time"],
        }
    ]


def test_zeta_agent_turn_aborts_on_deadline_between_model_turns(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "README.md"
    target.write_text("README\n", encoding="utf-8")
    store = zeta_trace.InMemoryStore()
    responses = iter([read_tool_call_response(target), {"content": "too late"}])
    events: list[DraftEvent] = []
    monotonic = iter([0.0, 0.0, 0.0, 2.0])

    monkeypatch.setattr(zeta_agent, "time_monotonic", lambda: next(monotonic))
    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api,
        "chat_completion_messages",
        lambda *args, **kwargs: next(responses),
    )
    monkeypatch.setattr(
        zeta_agent,
        "invoke_capability",
        lambda name, params, **kwargs: read_tool_payload(target),
    )

    with pytest.raises(zeta_agent.AgentTurnAborted) as raised:
        run_agent_turn(
            "test",
            [],
            zeta_agent.AgentConfig(
                allowed_capabilities=("read",),
                max_turns=2,
                max_wall_seconds=1.0,
            ),
            event_sink=events.append,
            prompt_builder=zeta_context.PromptBuilder(store=store),
        )

    assert raised.value.reason == "deadline_exceeded"
    result = raised.value.result
    assert len(result.prompt_traces) == 1
    trace = result.prompt_traces[0]
    assert_prompt_trace_replay_graph(store, trace)
    assert trace.assistant_message_object_id is not None
    tool_call = event_by_type(result.events, "tool_call")
    tool_result = event_by_type(result.events, "tool_result")
    call_object_id = projected_tool_call_object_id(store, tool_call)
    result_object_id = projected_tool_result_object_id(store, tool_result)
    assert_tool_call_derivation(
        store,
        result,
        call_object_id,
    )
    assert_tool_result_derivation(
        store,
        call_object_id,
        result_object_id,
    )
    assert raised.value.result.steps[-1].step == "abort_run"
    projected = timeline_events(events)
    assert [event["type"] for event in projected] == [
        "model",
        "tool_call",
        "tool_result",
        "turn_aborted",
    ]
    assert projected[-1]["reason"] == "deadline_exceeded"
    assert projected[-1]["caused_by"] == projected[-2]["id"]


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

    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api, "chat_completion_messages", fake_chat_completion_messages
    )
    monkeypatch.setattr(zeta_agent, "invoke_capability", crash_invoke)

    result = run_agent_turn(
        "test",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("read",), max_turns=3),
    )

    assert result.final_answer == "recovered"
    tool_result = next(
        event
        for event in timeline_events(result.events)
        if event.get("type") == "tool_result"
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

    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api,
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

    result = run_agent_turn(
        "inspect",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("read",), max_turns=1),
    )

    assert ran is False
    tool_result = next(
        event
        for event in timeline_events(result.events)
        if event.get("type") == "tool_result"
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

    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api,
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

    result = run_agent_turn(
        "inspect",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("read",), max_turns=1),
    )

    assert ran is False
    tool_result = next(
        event
        for event in timeline_events(result.events)
        if event.get("type") == "tool_result"
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

    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api,
        "chat_completion_messages",
        fake_chat_completion_messages,
    )

    result = run_agent_turn(
        "edit",
        [],
        zeta_agent.AgentConfig(
            allowed_capabilities=("edit",),
            execution_mode="direct",
            max_turns=3,
        ),
    )

    assert requests == 2
    assert result.final_answer == "done"
    assert target.read_text(encoding="utf-8") == "new\n"


def test_zeta_agent_codex_api_skips_endpoint_probe(monkeypatch) -> None:
    def fail_probe(url: str | None = None) -> bool:
        raise AssertionError("codex profiles must not probe a local endpoint")

    monkeypatch.setattr(zeta_model, "model_endpoint_open", fail_probe)

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

    monkeypatch.setattr(zeta_model, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api, "chat_completion_messages", fake_chat_completion_messages
    )

    run_agent_turn(
        "answer",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("read",), max_turns=1),
    )

    assert captured["api"] is None
