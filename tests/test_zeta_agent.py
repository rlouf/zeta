"""Agent loop tests."""

import asyncio
import json
import threading
import tomllib
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import asdict, fields
from datetime import UTC, datetime
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

from sigil.tools import ensure_builtin_tools_registered
from zeta import cli as zeta_cli
from zeta import models as zeta_models_api
from zeta import process as zeta_process
from zeta import rpc as zeta_rpc
from zeta.capabilities import execution as zeta_capability_execution
from zeta.capabilities.execution import (
    InProcessCapabilityExecutor,
)
from zeta.capabilities.registry import CapabilityRegistry, RegisteredCapability
from zeta.capabilities.types import (
    Capability,
    CapabilityId,
)
from zeta.context import builder as zeta_context
from zeta.models import chat_completions as zeta_model
from zeta.models import types as zeta_model_shapes
from zeta.orchestration import dispatch as zeta_dispatch
from zeta.orchestration import scheduling as zeta_scheduling
from zeta.orchestration import session_turn_agent as zeta_session_turn_agent
from zeta.orchestration import worker as zeta_worker
from zeta.orchestration.attempts import Attempt
from zeta.orchestration.queue import QueueItem
from zeta.records import events as zeta_event_model
from zeta.records.events import DraftEvent, Event
from zeta.records.stores import (
    Filter,
    InMemoryStore,
    MemoryEventStore,
    SqliteEventStore,
    event_store_path,
)
from zeta.run import runtime as zeta_agent
from zeta.run import thread_run as zeta_requests
from zeta.run import threads as zeta_scope
from zeta.run.config import CompactionPolicy
from zeta.run.runtime import AgentRunResult

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
    run_id: str | None = None,
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
        run_id=run_id,
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


def run_agent_turn(*args: Any, **kwargs: Any) -> AgentRunResult:
    return asyncio.run(zeta_agent.run_agent(*args, **kwargs))


def never_abort(*, check_deadline: bool = True) -> str | None:
    del check_deadline
    return None


def test_zeta_run_dependencies_keep_abort_signal_as_boundary() -> None:
    dependency_fields = {field.name for field in fields(zeta_agent.RunDependencies)}

    assert "abort_reason" in dependency_fields
    assert "clock" not in dependency_fields
    assert "deadline" not in dependency_fields
    assert "cancellation_event" not in dependency_fields


def run_rpc_session(*args: Any, **kwargs: Any) -> dict[str, Any]:
    params = args[0] if args else kwargs.pop("params")
    runtime_context = kwargs["runtime_context"]
    event_dispatcher = kwargs.get("event_dispatcher")
    if event_dispatcher is None:
        event_dispatcher = zeta_dispatch.EventDispatcher(
            runtime_context.event_sink,
            executors=[
                zeta_session_turn_agent.session_turn_agent(
                    runtime_context,
                    publish_event=kwargs["publish_event"],
                )
            ],
            publish_event=kwargs["publish_event"],
        )
    return asyncio.run(
        zeta_session_turn_agent.submit_session_turn(
            params,
            runtime_context=runtime_context,
            event_dispatcher=event_dispatcher,
        )
    )


def dispatch_event(
    dispatcher: zeta_dispatch.EventDispatcher,
    draft: DraftEvent,
) -> zeta_dispatch.DispatchOutcome:
    return asyncio.run(dispatcher.publish_and_run(draft))


def record_exact_agent_call(
    calls: list[str],
) -> Callable[[zeta_dispatch.AgentInvocation], Coroutine[Any, Any, dict[str, Any]]]:
    async def run(invocation: zeta_dispatch.AgentInvocation) -> dict[str, Any]:
        calls.append(invocation.triggering_event.id)
        return {"outcome": None}

    return run


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
    event = zeta_capability_execution.tool_result_event(
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
    event = zeta_capability_execution.tool_result_event(
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
    event = zeta_capability_execution.tool_result_event(
        "call-1",
        "read",
        {"ok": False, "error": {"code": "read-failed", "message": "missing"}},
    )

    assert event["result"]["error"] == {"code": "read-failed", "message": "missing"}


def test_zeta_model_tool_call_round_trips_provider_payload_to_event() -> None:
    record = zeta_capability_execution.ModelToolCall.from_provider(
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
    assert record == zeta_capability_execution.ModelToolCall(
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
    assert (
        zeta_capability_execution.ModelToolCall.from_provider({"id": "call-1"}, index=0)
        is None
    )
    assert (
        zeta_capability_execution.model_tool_call_event(
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

    record = zeta_capability_execution.ModelToolCall.from_provider(tool_call, index=0)
    invocation = zeta_capability_execution.tool_call_invocation(
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
    model_tool_call = zeta_capability_execution.ModelToolCall(
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
    event = zeta_capability_execution.tool_result_event(
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
    ctx = zeta_agent.RunDependencies(
        event_sink=sink_events.append,
        trace_store=None,
        tool_registry=CapabilityRegistry(),
        builder=cast(Any, None),
        abort_reason=never_abort,
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

    ctx = zeta_agent.RunDependencies(
        event_sink=drafts.append,
        trace_store=None,
        tool_registry=CapabilityRegistry(),
        builder=cast(Any, None),
        abort_reason=never_abort,
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
    ctx = zeta_capability_execution.CapabilityExecutionContext(
        event_sink=drafts.append,
        trace_store=None,
        tool_registry=registry,
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
    state = zeta_agent.RunState()
    builder = PlanOnlyPromptBuilder()
    ctx = zeta_agent.RunDependencies(
        event_sink=None,
        trace_store=None,
        tool_registry=CapabilityRegistry(),
        builder=builder,
        abort_reason=never_abort,
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


def test_zeta_call_model_step_updates_model_status_during_request() -> None:
    status_events: list[str] = []
    emitted: list[DraftEvent] = []

    class FakeStatus:
        def __enter__(self) -> "FakeStatus":
            status_events.append("enter")
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: object,
        ) -> bool:
            del exc_type, exc, traceback
            status_events.append("exit")
            return False

        def reasoning_delta(self, text: str) -> None:
            status_events.append(f"reasoning:{text}")

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
            del model_input, config, telemetry_sink
            assert status_events == ["enter"]
            assert stream is not None
            stream.reasoning_delta("checking")
            return zeta_model_shapes.ModelOutput(message={"content": "done"})

    state = zeta_agent.RunState()

    asyncio.run(
        zeta_agent.call_model_step(
            zeta_model_shapes.ModelInput(
                messages=[{"role": "user", "content": "answer"}],
                tools=[],
                tool_choice="auto",
            ),
            config=zeta_agent.AgentConfig(model_status_factory=FakeStatus),
            state=state,
            model_gateway=FakeGateway(),
            event_sink=emitted.append,
        )
    )

    assert status_events == ["enter", "reasoning:checking", "exit"]
    assert [draft.payload["text"] for draft in emitted] == ["checking"]


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
                zeta_agent.run_agent(
                    "first",
                    [],
                    zeta_agent.AgentConfig(max_turns=1),
                    model_gateway=gateway,
                ),
                zeta_agent.run_agent(
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
    ctx = zeta_agent.RunDependencies(
        event_sink=None,
        trace_store=None,
        tool_registry=registry,
        builder=zeta_context.PromptBuilder(),
        abort_reason=never_abort,
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
    ctx = zeta_agent.RunDependencies(
        event_sink=None,
        trace_store=None,
        tool_registry=registry,
        builder=zeta_context.PromptBuilder(),
        abort_reason=never_abort,
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


def rpc_client(
    input_stream: StringIO | None = None,
    output: StringIO | None = None,
    *,
    session: zeta_scope.SessionScope | None = None,
    dispatcher: zeta_dispatch.EventDispatcher | None = None,
) -> tuple[zeta_rpc.JsonRpcConnection, zeta_rpc.RpcClient, zeta_rpc.JsonRpcRouter]:
    input_stream = input_stream or StringIO()
    output = output or StringIO()
    connection = zeta_rpc.JsonRpcConnection(input_stream, output)
    if session is None:
        event_store = zeta_events.MemoryEventStore()
        session = zeta_scope.SessionScope(
            session_id="ctx-session",
            event_sink=event_store,
            trace_store=zeta_trace.InMemoryStore(),
            tool_registry=CapabilityRegistry(),
            state_dir=Path("/tmp"),
            session_dir=Path("/tmp") / "sessions" / "ctx-session",
        )

    def notify_event(event: Event) -> None:
        connection.notify("events.notify", {"event": zeta_rpc.event_to_wire(event)})

    if dispatcher is None:
        dispatcher = zeta_dispatch.EventDispatcher(
            session.event_sink,
            publish_event=notify_event,
        )
    client = zeta_rpc.RpcClient(
        connection=connection,
        session=session,
        dispatcher=dispatcher,
        pending_runs={},
        pending_tool_calls={},
    )
    router = zeta_rpc.JsonRpcRouter(client)
    router.route("initialize", zeta_rpc.initialize)
    router.route("events.publish", zeta_rpc.events_publish)
    router.route("events.list", zeta_rpc.events_list)
    router.route("session.run", zeta_rpc.session_run)
    router.route("session.cancel", zeta_rpc.session_cancel)
    router.route("tools.register", zeta_rpc.tools_register)
    router.route("tools.respond", zeta_rpc.tools_respond)
    return connection, client, router


def run_rpc_messages(
    input_stream: StringIO,
    output: StringIO,
    *,
    session: zeta_scope.SessionScope | None = None,
    dispatcher: zeta_dispatch.EventDispatcher | None = None,
) -> zeta_rpc.RpcClient:
    connection, client, router = rpc_client(
        input_stream,
        output,
        session=session,
        dispatcher=dispatcher,
    )
    asyncio.run(connection.serve(router))
    return client


def test_zeta_rpc_initialize_returns_server_metadata() -> None:
    input_stream = StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n"
    )
    output = StringIO()

    run_rpc_messages(input_stream, output)

    assert rpc_messages(output) == [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"server": "zeta", "protocol": "0.1"},
        }
    ]


def test_zeta_rpc_unknown_method_returns_structured_error() -> None:
    input_stream = StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "events.subscribe"}) + "\n"
    )
    output = StringIO()

    run_rpc_messages(input_stream, output)

    assert rpc_messages(output) == [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": -32601,
                "message": "Method not found",
                "data": {"code": "method_not_found", "method": "events.subscribe"},
            },
        }
    ]


def test_zeta_rpc_router_response_for_message_does_not_write_to_connection() -> None:
    output = StringIO()
    _, _, router = rpc_client(output=output)

    response = asyncio.run(
        router.response_for_message({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    )

    assert response == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"server": "zeta", "protocol": "0.1"},
    }
    assert output.getvalue() == ""


def test_zeta_rpc_events_publish_uses_constructor_shaped_event(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    session = zeta_scope.SessionScope(
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
                    "event_type": "zeta.user_message",
                    "source": "test",
                    "payload": {"content": "hello"},
                    "session_id": "ctx-session",
                    "run_id": "run_1",
                },
            }
        )
        + "\n"
    )
    output = StringIO()

    run_rpc_messages(input_stream, output, session=session)

    messages = rpc_messages(output)
    message = next(message for message in messages if message.get("id") == 1)
    notification = next(
        message for message in messages if message.get("method") == "events.notify"
    )
    assert message["result"]["inserted"] is True
    assert message["result"]["event"]["event_type"] == "zeta.user_message"
    assert message["result"]["event"]["payload"] == {"content": "hello"}
    assert message["result"]["event"]["cursor"] == 1
    assert message["result"]["lifecycle_events"] == []
    assert notification["params"]["event"] == message["result"]["event"]


def test_zeta_rpc_events_publish_returns_before_routing_finishes(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
        session = zeta_scope.SessionScope(
            session_id="ctx-session",
            event_sink=event_store,
            trace_store=zeta_trace.InMemoryStore(),
            tool_registry=CapabilityRegistry(),
            state_dir=tmp_path,
            session_dir=tmp_path / "sessions" / "ctx-session",
        )
        started = asyncio.Event()
        release = asyncio.Event()

        async def run_agent(
            invocation: zeta_dispatch.AgentInvocation,
        ) -> dict[str, object]:
            started.set()
            await release.wait()
            return {
                "outcome": "handled",
                "event_id": invocation.triggering_event.id,
            }

        dispatcher = zeta_dispatch.EventDispatcher(
            event_store,
            executors=[
                zeta_dispatch.ExecutableAgent(
                    zeta_dispatch.AgentDefinition(
                        "slow-agent",
                        (zeta_dispatch.EventPattern("zeta.user_message"),),
                    ),
                    run=run_agent,
                )
            ],
        )
        output = StringIO()
        _, _, router = rpc_client(output=output, session=session, dispatcher=dispatcher)

        await router.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "events.publish",
                "params": {
                    "event_type": "zeta.user_message",
                    "source": "test",
                    "payload": {"content": "hello"},
                    "session_id": "ctx-session",
                },
            }
        )

        message = next(
            message for message in rpc_messages(output) if message.get("id") == 1
        )
        assert message["result"]["inserted"] is True
        assert message["result"]["lifecycle_events"] == []
        assert not release.is_set()

        await asyncio.wait_for(started.wait(), timeout=1)
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(run())


def test_zeta_rpc_events_publish_rejects_lifecycle_event_ingress(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    session = zeta_scope.SessionScope(
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
                    "event_type": "runtime.attempt.started",
                    "source": "test",
                    "payload": {"attempt_id": "att_1"},
                    "session_id": "ctx-session",
                },
            }
        )
        + "\n"
    )
    output = StringIO()

    run_rpc_messages(input_stream, output, session=session)

    message = next(
        message for message in rpc_messages(output) if message.get("id") == 1
    )
    assert message["error"]["code"] == -32602
    assert message["error"]["data"]["code"] == "reserved_runtime_event"
    assert event_store.list_events(zeta_events.Filter()) == []


def test_zeta_rpc_events_list_uses_event_store_filter_names(tmp_path: Path) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    for content in ("one", "two", "three"):
        event_store.accept(
            DraftEvent(
                event_type="zeta.user_message",
                source="test",
                payload={"content": content},
                session_id="ctx-session",
                run_id="run_1",
            )
        )
    session = zeta_scope.SessionScope(
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
                "method": "events.list",
                "params": {
                    "session_id": "ctx-session",
                    "run_id": "run_1",
                    "after_cursor": 1,
                    "limit": 2,
                },
            }
        )
        + "\n"
    )
    output = StringIO()

    run_rpc_messages(input_stream, output, session=session)

    message = next(
        message for message in rpc_messages(output) if message.get("id") == 1
    )
    assert [event["payload"]["content"] for event in message["result"]["events"]] == [
        "two",
        "three",
    ]
    assert message["result"]["next_cursor"] == 3


def test_zeta_rpc_eventlog_events_list_request_produces_response() -> None:
    event_store = zeta_events.MemoryEventStore()
    stored = event_store.accept(
        DraftEvent(
            event_type="zeta.user_message",
            source="test",
            payload={"content": "hello"},
            session_id="ctx-session",
        )
    ).event
    request = event_store.accept(
        zeta_rpc.rpc_requested_draft(
            "events.list",
            {"event_type": "zeta.user_message"},
            request_id="req_1",
            session_id="ctx-session",
        )
    ).event
    session = zeta_scope.SessionScope(
        session_id="ctx-session",
        event_sink=event_store,
        trace_store=zeta_trace.InMemoryStore(),
        tool_registry=CapabilityRegistry(),
        state_dir=Path("/tmp"),
        session_dir=Path("/tmp") / "sessions" / "ctx-session",
    )
    _, _, router = rpc_client(session=session)

    response = asyncio.run(zeta_rpc.run_eventlog_rpc_once(router))

    assert response is not None
    assert response.event_type == "rpc.responded"
    assert response.caused_by == request.id
    assert response.payload["request_id"] == "req_1"
    assert response.payload["result"]["events"][0]["id"] == stored.id


def test_zeta_rpc_eventlog_invalid_session_run_produces_failed_event() -> None:
    event_store = zeta_events.MemoryEventStore()
    request = event_store.accept(
        zeta_rpc.rpc_requested_draft(
            "session.run",
            {},
            request_id="req_invalid",
            session_id="ctx-session",
        )
    ).event
    session = zeta_scope.SessionScope(
        session_id="ctx-session",
        event_sink=event_store,
        trace_store=zeta_trace.InMemoryStore(),
        tool_registry=CapabilityRegistry(),
        state_dir=Path("/tmp"),
        session_dir=Path("/tmp") / "sessions" / "ctx-session",
    )
    _, _, router = rpc_client(session=session)

    response = asyncio.run(zeta_rpc.run_eventlog_rpc_once(router))

    assert response is not None
    assert response.event_type == "rpc.failed"
    assert response.caused_by == request.id
    assert response.payload["request_id"] == "req_invalid"
    assert response.payload["error"]["code"] == -32602
    assert response.payload["error"]["data"]["code"] == "invalid_params"


def test_zeta_rpc_eventlog_session_run_request_produces_started_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_store = zeta_events.MemoryEventStore()
    request = event_store.accept(
        zeta_rpc.rpc_requested_draft(
            "session.run",
            {"objective": "answer", "tools": []},
            request_id="req_run",
            session_id="ctx-session",
        )
    ).event
    session = zeta_scope.SessionScope(
        session_id="ctx-session",
        event_sink=event_store,
        trace_store=zeta_trace.InMemoryStore(),
        tool_registry=CapabilityRegistry(),
        state_dir=Path("/tmp"),
        session_dir=Path("/tmp") / "sessions" / "ctx-session",
    )
    _, _, router = rpc_client(session=session)
    monkeypatch.setattr(zeta_rpc.routes, "session_run_id", lambda: "run_eventlog")

    response = asyncio.run(zeta_rpc.run_eventlog_rpc_once(router))

    assert response is not None
    assert response.event_type == "rpc.responded"
    assert response.caused_by == request.id
    assert response.payload["request_id"] == "req_run"
    result = response.payload["result"]
    assert result["run_id"] == "run_eventlog"
    assert result["status"] == "started"
    assert result["event"]["event_type"] == "session.turn.requested"


def test_zeta_rpc_session_run_returns_started_event_from_shared_draft(
    monkeypatch: pytest.MonkeyPatch,
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
    monkeypatch.setattr(zeta_rpc.routes, "session_run_id", lambda: "run_test")

    client = run_rpc_messages(input_stream, output)

    message = next(
        message for message in rpc_messages(output) if message.get("id") == 1
    )
    assert message["result"]["run_id"] == "run_test"
    assert message["result"]["status"] == "started"
    event = message["result"]["event"]
    assert event["event_type"] == "session.turn.requested"
    assert event["run_id"] == "run_test"
    assert event["idempotency_key"] == "session.turn.requested:run_test"
    assert (
        event["payload"]
        == zeta_requests.session_turn_requested_draft(
            {"objective": "answer", "tools": []},
            run_id="run_test",
            runtime_context=client.session,
        ).payload
    )
    assert "turn_id" not in message["result"]["event"]
    assert client.pending_runs["run_test"].task is not None


def test_zeta_rpc_session_cancel_updates_run_state() -> None:
    _, client, router = rpc_client()
    cancellation_event = asyncio.Event()
    client.pending_runs["run_active"] = zeta_rpc.RunState(
        run_id="run_active",
        cancellation_event=cancellation_event,
    )

    result = asyncio.run(
        zeta_rpc.session_cancel({"run_id": "run_active"}, router.client)
    )

    assert result == {
        "cancelled": True,
        "run_id": "run_active",
        "status": "cancelling",
    }
    assert cancellation_event.is_set()


def test_zeta_rpc_tools_register_uses_capability_shape() -> None:
    registry = CapabilityRegistry()
    event_store = zeta_events.MemoryEventStore()
    session = zeta_scope.SessionScope(
        session_id="ctx-session",
        event_sink=event_store,
        trace_store=zeta_trace.InMemoryStore(),
        tool_registry=registry,
        state_dir=Path("/tmp"),
        session_dir=Path("/tmp") / "sessions" / "ctx-session",
    )
    _, client, _ = rpc_client(session=session)

    result = asyncio.run(
        zeta_rpc.tools_register(
            {
                "capabilities": [
                    {
                        "name": "pick_file",
                        "description": "Pick a file.",
                        "input_schema": {"type": "object"},
                        "timeout_seconds": 2,
                    },
                    {
                        "name": "open_panel",
                        "description": "Open a panel.",
                        "input_schema": {"type": "object"},
                    },
                ]
            },
            client,
        )
    )

    assert result == {
        "registered": [
            {
                "id": "rpc.pick_file",
                "provider": "rpc",
                "name": "pick_file",
                "description": "Pick a file.",
                "input_schema": {"type": "object"},
                "timeout_seconds": 2,
            },
            {
                "id": "rpc.open_panel",
                "provider": "rpc",
                "name": "open_panel",
                "description": "Open a panel.",
                "input_schema": {"type": "object"},
                "timeout_seconds": None,
            },
        ]
    }
    assert registry.get("rpc.pick_file") is not None
    assert registry.get("rpc.open_panel") is not None


def test_zeta_rpc_registered_tool_invokes_peer_call_tool() -> None:
    registry = CapabilityRegistry()
    event_store = zeta_events.MemoryEventStore()
    session = zeta_scope.SessionScope(
        session_id="ctx-session",
        event_sink=event_store,
        trace_store=zeta_trace.InMemoryStore(),
        tool_registry=registry,
        state_dir=Path("/tmp"),
        session_dir=Path("/tmp") / "sessions" / "ctx-session",
    )
    _, client, _ = rpc_client(session=session)
    captured: dict[str, Any] = {}

    async def fake_call_tool(
        name: str,
        params: dict[str, Any],
        *,
        timeout_seconds: int | float | None,
    ) -> dict[str, Any]:
        captured["name"] = name
        captured["params"] = params
        captured["timeout_seconds"] = timeout_seconds
        return {"ok": True, "path": "README.md"}

    cast(Any, client).call_tool = fake_call_tool

    async def run() -> dict[str, Any]:
        await zeta_rpc.tools_register(
            {
                "capabilities": [
                    {
                        "name": "pick_file",
                        "description": "Pick a file.",
                        "input_schema": {"type": "object"},
                        "timeout_seconds": 2,
                    }
                ]
            },
            client,
        )
        return await registry.invoke_async(
            "rpc.pick_file",
            {"pattern": "*.md"},
            execution_mode="direct",
        )

    result = asyncio.run(run())

    assert result == {"ok": True, "path": "README.md"}
    assert captured == {
        "name": "pick_file",
        "params": {"pattern": "*.md"},
        "timeout_seconds": 2,
    }


def test_zeta_rpc_tools_respond_resolves_pending_call() -> None:
    _, client, _ = rpc_client()

    async def run() -> None:
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        client.pending_tool_calls["call_1"] = future
        await zeta_rpc.tools_respond(
            {
                "call_id": "call_1",
                "status": "responded",
                "result": {"ok": True},
            },
            client,
        )
        assert future.result() == {"ok": True}
        assert client.pending_tool_calls["call_1"] is future

    asyncio.run(run())


def test_zeta_dispatch_terminal_queue_item_result_comes_from_lifecycle_event() -> None:
    event = Event(
        id="evt_terminal",
        event_type="runtime.queue_item.completed",
        source="zeta",
        payload={
            "queue_item_id": "qi_evt_request_zeta_session_turn",
            "event_id": "evt_request",
            "target_agent": "zeta.session.turn",
            "status": "completed",
            "result": {
                "run_id": "run_lifecycle",
                "outcome": "completed",
                "final_answer": "from lifecycle",
            },
        },
        idempotency_key=None,
        caused_by="evt_request",
        session_id="ctx-session",
        run_id="run_lifecycle",
        timestamp_ms=1,
        cursor=9,
    )

    assert zeta_dispatch.terminal_queue_item_result(
        [event],
        event_id="evt_request",
        target_agent="zeta.session.turn",
    ) == {
        "run_id": "run_lifecycle",
        "outcome": "completed",
        "final_answer": "from lifecycle",
        "final_event_cursor": "9",
    }


def test_zeta_session_turn_agent_adapts_requested_event_to_turn_runner(
    monkeypatch,
    tmp_path: Path,
) -> None:
    context = zeta_scope.SessionScope(
        session_id="ctx-session",
        event_sink=zeta_events.MemoryEventStore(),
        trace_store=zeta_trace.InMemoryStore(),
        tool_registry=CapabilityRegistry(),
        state_dir=tmp_path,
        session_dir=tmp_path / "sessions" / "ctx-session",
    )
    published: list[Event | DraftEvent] = []
    captured: dict[str, Any] = {}

    async def fake_run_session_turn(
        params: dict[str, Any],
        *,
        run_id: str,
        caused_by: str,
        publish_event: Callable[[Event | DraftEvent], None],
        runtime_context: zeta_scope.SessionScope,
        cancellation_event: asyncio.Event | None,
    ) -> dict[str, Any]:
        captured["params"] = params
        captured["run_id"] = run_id
        captured["caused_by"] = caused_by
        captured["publish_event"] = publish_event
        captured["runtime_context"] = runtime_context
        captured["cancellation_event"] = cancellation_event
        publish_event(DraftEvent("seen", "test", {}))
        return {"run_id": run_id, "outcome": "completed"}

    cancellation_event = asyncio.Event()
    monkeypatch.setattr(
        zeta_session_turn_agent, "run_session_turn", fake_run_session_turn
    )

    agent = zeta_session_turn_agent.session_turn_agent(
        context,
        publish_event=published.append,
        cancellation_event_for_run=lambda run_id: (
            cancellation_event if run_id == "run_event" else None
        ),
    )
    triggering_event = Event(
        id="evt_request",
        event_type="session.turn.requested",
        source="zeta",
        payload={"objective": "answer", "run_id": "run_event"},
        idempotency_key=None,
        caused_by=None,
        session_id="ctx-session",
        run_id="run_event",
        timestamp_ms=1,
        cursor=1,
    )
    runner = agent.run
    assert runner is not None
    result = asyncio.run(
        cast(
            Coroutine[Any, Any, dict[str, Any]],
            runner(zeta_dispatch.AgentInvocation(agent.definition, triggering_event)),
        )
    )

    assert agent.definition.agent_id == "zeta.session.turn"
    assert agent.definition.accepts(triggering_event)
    assert result == {"run_id": "run_event", "outcome": "completed"}
    assert captured == {
        "params": {"objective": "answer", "run_id": "run_event"},
        "run_id": "run_event",
        "caused_by": "evt_request",
        "publish_event": published.append,
        "runtime_context": context,
        "cancellation_event": cancellation_event,
    }
    assert [event["type"] for event in published_event_views(published)] == ["seen"]


def test_zeta_run_session_turn_records_user_message_and_returns_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    context = zeta_scope.SessionScope(
        session_id="ctx-session",
        event_sink=zeta_events.MemoryEventStore(),
        trace_store=zeta_trace.InMemoryStore(),
        tool_registry=CapabilityRegistry(),
        state_dir=tmp_path,
        session_dir=tmp_path / "sessions" / "ctx-session",
    )
    published: list[Event] = []
    captured: dict[str, Any] = {}

    async def fake_run_agent_turn(
        objective: str,
        timeline: list[Event],
        config: Any,
        **kwargs: Any,
    ) -> AgentRunResult:
        captured["objective"] = objective
        captured["timeline"] = timeline
        captured["config"] = config
        captured["kwargs"] = kwargs
        return AgentRunResult(final_answer="done")

    monkeypatch.setattr(zeta_requests, "run_agent", fake_run_agent_turn)

    result = asyncio.run(
        zeta_requests.run_session_turn(
            {"objective": "answer", "workflow": "ask", "tools": []},
            run_id="run_direct",
            caused_by="evt_request",
            publish_event=published.append,
            runtime_context=context,
            cancellation_event=None,
        )
    )

    assert result["outcome"] == "completed"
    assert result["final_answer"] == "done"
    assert result["run_id"] == "run_direct"
    assert captured["objective"] == "answer"
    assert captured["timeline"] == []
    assert captured["config"].model_session_id == "ctx-session"
    assert captured["kwargs"]["caused_by"] == "evt_request"
    assert [event.event_type for event in published] == ["zeta.user_message"]
    assert published[0].payload["content"] == "answer"
    assert published[0].run_id == "run_direct"


def test_zeta_session_run_params_capture_defaults_and_options() -> None:
    params = zeta_requests.SessionRunParams(
        objective="answer",
        tools=["read", "bash"],
        context="existing notes",
        model="gpt-test",
        max_steps=3,
        max_wall_seconds=1,
    )

    assert params.objective == "answer"
    assert params.workflow == "ask"
    assert params.tools == ["read", "bash"]
    assert params.context == "existing notes"
    assert params.model == "gpt-test"
    assert params.max_steps == 3
    assert params.max_wall_seconds == 1


def test_zeta_session_run_params_preserve_boundary_values() -> None:
    params = zeta_requests.SessionRunParams(
        objective=cast(str, 12),
        tools=cast(list[str], {"read", "bash"}),
        context=cast(str, None),
        system=cast(str, 34),
        max_wall_seconds=cast(float, "1"),
    )

    assert params.objective == 12
    assert params.tools == {"read", "bash"}
    assert params.context is None
    assert params.system == 34
    assert params.max_wall_seconds == "1"


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
    assert [event.event_type for event in outcome.lifecycle_events] == [
        "runtime.queue_item.unhandled"
    ]
    assert [event.idempotency_key for event in outcome.lifecycle_events] == [
        f"queue_item:{outcome.event.id}:unhandled"
    ]
    assert [event.event_type for event in published] == [
        "github.issue.opened",
        "runtime.queue_item.unhandled",
    ]
    assert [
        event.event_type for event in event_store.list_events(zeta_events.Filter())
    ] == ["github.issue.opened", "runtime.queue_item.unhandled"]


def test_zeta_event_dispatcher_creates_work_for_matching_agent(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    published: list[zeta_events.Event] = []
    seen: list[zeta_dispatch.AgentInvocation] = []

    async def run_agent(run: zeta_dispatch.AgentInvocation) -> dict[str, object]:
        seen.append(run)
        return {"outcome": "handled"}

    dispatcher = zeta_dispatch.EventDispatcher(
        event_store,
        executors=[
            zeta_dispatch.ExecutableAgent(
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
    assert [event.event_type for event in outcome.lifecycle_events] == [
        "runtime.queue_item.available",
        "runtime.queue_item.claimed",
        "runtime.attempt.started",
        "runtime.attempt.completed",
        "runtime.queue_item.completed",
    ]
    assert {event.caused_by for event in outcome.lifecycle_events} == {outcome.event.id}
    assert [event.payload["target_agent"] for event in outcome.lifecycle_events] == [
        "issue-triage",
        "issue-triage",
        "issue-triage",
        "issue-triage",
        "issue-triage",
    ]
    queue_item_id = f"qi_{outcome.event.id}_issue-triage"
    assert [event.idempotency_key for event in outcome.lifecycle_events] == [
        f"queue_item:{outcome.event.id}:issue-triage:available",
        f"queue_item:{outcome.event.id}:issue-triage:claimed:1",
        f"attempt:{queue_item_id}:1:started",
        f"attempt:{queue_item_id}:1:completed",
        f"queue_item:{outcome.event.id}:issue-triage:completed",
    ]
    assert zeta_dispatch.terminal_queue_item_result(
        outcome.lifecycle_events,
        event_id=outcome.event.id,
        target_agent="issue-triage",
    ) == {
        "outcome": "handled",
        "final_event_cursor": "6",
    }
    assert outcome.lifecycle_events[-1].payload == {
        **asdict(
            QueueItem(
                queue_item_id=queue_item_id,
                event_id=outcome.event.id,
                target_agent="issue-triage",
                status="completed",
            )
        ),
        "result": {"outcome": "handled"},
    }
    assert outcome.lifecycle_events[3].payload == {
        **asdict(
            Attempt(
                attempt_id=f"att_{queue_item_id}_1",
                queue_item_id=queue_item_id,
                event_id=outcome.event.id,
                attempt_number=1,
                target_agent="issue-triage",
                status="completed",
                started_at=outcome.lifecycle_events[2].payload["started_at"],
                finished_at=outcome.lifecycle_events[3].payload["finished_at"],
                session_id="repo",
            )
        ),
        "result": {"outcome": "handled"},
    }
    assert [event.event_type for event in published] == [
        "github.issue.opened",
        "runtime.queue_item.available",
        "runtime.queue_item.claimed",
        "runtime.attempt.started",
        "runtime.attempt.completed",
        "runtime.queue_item.completed",
    ]


def test_zeta_event_dispatcher_can_publish_without_routing(tmp_path: Path) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    published: list[zeta_events.Event] = []
    calls = 0

    async def run_agent(run: zeta_dispatch.AgentInvocation) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"outcome": "handled", "event": run.triggering_event.id}

    dispatcher = zeta_dispatch.EventDispatcher(
        event_store,
        executors=[
            zeta_dispatch.ExecutableAgent(
                zeta_dispatch.AgentDefinition(
                    "issue-triage",
                    (zeta_dispatch.EventPattern("github.issue.opened"),),
                ),
                run=run_agent,
            )
        ],
        publish_event=published.append,
    )

    outcome = asyncio.run(
        dispatcher.publish_event(
            zeta_events.DraftEvent(
                "github.issue.opened",
                "github",
                {"title": "Bug"},
                session_id="repo",
            ),
        )
    )

    assert outcome.inserted is True
    assert outcome.lifecycle_events == []
    assert calls == 0
    assert [event.event_type for event in published] == ["github.issue.opened"]
    assert [
        event.event_type for event in event_store.list_events(zeta_events.Filter())
    ] == ["github.issue.opened"]


def test_zeta_event_dispatcher_routes_available_work_before_running(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    calls = 0

    async def run_agent(run: zeta_dispatch.AgentInvocation) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"outcome": "handled", "event": run.triggering_event.id}

    dispatcher = zeta_dispatch.EventDispatcher(
        event_store,
        executors=[
            zeta_dispatch.ExecutableAgent(
                zeta_dispatch.AgentDefinition(
                    "issue-triage",
                    (zeta_dispatch.EventPattern("github.issue.opened"),),
                ),
                run=run_agent,
            )
        ],
    )

    accepted = asyncio.run(
        dispatcher.publish_event(
            zeta_events.DraftEvent(
                "github.issue.opened",
                "github",
                {"title": "Bug"},
                session_id="repo",
            )
        )
    )
    route = asyncio.run(dispatcher.route(accepted.event))

    queue_item_id = f"qi_{accepted.event.id}_issue-triage"
    assert calls == 0
    assert [event.event_type for event in route.lifecycle_events] == [
        "runtime.queue_item.available"
    ]
    assert route.queue_items == [
        zeta_dispatch.RoutedQueueItem(
            queue_item_id=queue_item_id,
            event_id=accepted.event.id,
            target_agent="issue-triage",
        )
    ]

    execution_events = asyncio.run(dispatcher.run_queue_item(queue_item_id))

    assert calls == 1
    assert [event.event_type for event in execution_events] == [
        "runtime.queue_item.claimed",
        "runtime.attempt.started",
        "runtime.attempt.completed",
        "runtime.queue_item.completed",
    ]
    assert zeta_dispatch.terminal_queue_item_result(
        execution_events,
        event_id=accepted.event.id,
        target_agent="issue-triage",
    ) == {
        "outcome": "handled",
        "event": accepted.event.id,
        "final_event_cursor": "6",
    }


def test_zeta_event_dispatcher_rejects_terminal_queue_item_execution(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    calls = 0

    async def run_agent(run: zeta_dispatch.AgentInvocation) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"outcome": "handled", "event": run.triggering_event.id}

    dispatcher = zeta_dispatch.EventDispatcher(
        event_store,
        executors=[
            zeta_dispatch.ExecutableAgent(
                zeta_dispatch.AgentDefinition(
                    "issue-triage",
                    (zeta_dispatch.EventPattern("github.issue.opened"),),
                ),
                run=run_agent,
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
    queue_item_id = f"qi_{outcome.event.id}_issue-triage"

    with pytest.raises(zeta_dispatch.TerminalQueueItemError) as exc_info:
        asyncio.run(dispatcher.run_queue_item(queue_item_id))

    assert calls == 1
    assert exc_info.value.queue_item_id == queue_item_id
    assert exc_info.value.event_type == "runtime.queue_item.completed"
    assert [
        event.event_type
        for event in event_store.list_events(
            zeta_events.Filter(event_type="runtime.queue_item.completed")
        )
    ] == ["runtime.queue_item.completed"]


def test_zeta_event_dispatcher_rejects_unhandled_queue_item_execution(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    dispatcher = zeta_dispatch.EventDispatcher(event_store)

    outcome = dispatch_event(
        dispatcher,
        zeta_events.DraftEvent(
            "github.issue.opened",
            "github",
            {"title": "Bug"},
            session_id="repo",
        ),
    )
    queue_item_id = f"qi_{outcome.event.id}_unhandled"

    with pytest.raises(zeta_dispatch.TerminalQueueItemError) as exc_info:
        asyncio.run(dispatcher.run_queue_item(queue_item_id))

    assert exc_info.value.queue_item_id == queue_item_id
    assert exc_info.value.event_type == "runtime.queue_item.unhandled"


def test_zeta_event_dispatcher_rejects_external_lifecycle_events(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    published: list[zeta_events.Event] = []
    dispatcher = zeta_dispatch.EventDispatcher(
        event_store,
        publish_event=published.append,
    )

    with pytest.raises(zeta_dispatch.ReservedRuntimeEventError) as exc_info:
        asyncio.run(
            dispatcher.publish_event(
                zeta_events.DraftEvent(
                    "runtime.queue_item.completed",
                    "external",
                    {"queue_item_id": "qi_1"},
                    session_id="repo",
                )
            )
        )

    assert exc_info.value.event_type == "runtime.queue_item.completed"
    assert published == []
    assert event_store.list_events(zeta_events.Filter()) == []


def test_zeta_event_dispatcher_routes_agent_published_events(tmp_path: Path) -> None:
    async def run() -> None:
        event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")

        async def run_agent(
            invocation: zeta_dispatch.AgentInvocation,
        ) -> dict[str, object]:
            published = await invocation.publish(
                zeta_events.DraftEvent(
                    "agent.note.created",
                    "agent",
                    {"body": "triaged"},
                    idempotency_key="agent-note:1",
                )
            )
            return {"outcome": "handled", "published_event_id": published.id}

        dispatcher = zeta_dispatch.EventDispatcher(
            event_store,
            executors=[
                zeta_dispatch.ExecutableAgent(
                    zeta_dispatch.AgentDefinition(
                        "issue-triage",
                        (zeta_dispatch.EventPattern("github.issue.opened"),),
                    ),
                    run=run_agent,
                )
            ],
        )

        outcome = await dispatcher.publish_and_run(
            zeta_events.DraftEvent(
                "github.issue.opened",
                "github",
                {"title": "Bug"},
                session_id="repo",
                turn_id="turn-1",
            )
        )

        queue_item_id = f"qi_{outcome.event.id}_issue-triage"
        attempt_id = f"att_{queue_item_id}_1"
        stored_events = event_store.list_events(zeta_events.Filter())
        published_note = next(
            event for event in stored_events if event.event_type == "agent.note.created"
        )
        completed_queue_item = [
            event
            for event in stored_events
            if event.event_type == "runtime.queue_item.completed"
            and event.payload["target_agent"] == "issue-triage"
        ][0]

        assert published_note.caused_by == outcome.event.id
        assert published_note.session_id == "repo"
        assert published_note.turn_id == "turn-1"
        assert published_note.payload == {
            "body": "triaged",
            "_zeta_queue_item_id": queue_item_id,
            "_zeta_attempt_id": attempt_id,
            "_zeta_target_agent": "issue-triage",
            "_zeta_triggering_event_id": outcome.event.id,
        }
        assert completed_queue_item.payload["result"] == {
            "outcome": "handled",
            "published_event_id": published_note.id,
        }
        assert [event.event_type for event in stored_events] == [
            "github.issue.opened",
            "runtime.queue_item.available",
            "runtime.queue_item.claimed",
            "runtime.attempt.started",
            "agent.note.created",
            "runtime.queue_item.unhandled",
            "runtime.attempt.completed",
            "runtime.queue_item.completed",
        ]

    asyncio.run(run())


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
            executors=[
                zeta_dispatch.ExecutableAgent(
                    zeta_dispatch.AgentDefinition(
                        "agent.one",
                        (zeta_dispatch.EventPattern("github.issue.opened"),),
                    ),
                    run=run_agent,
                ),
                zeta_dispatch.ExecutableAgent(
                    zeta_dispatch.AgentDefinition(
                        "agent.two",
                        (zeta_dispatch.EventPattern("github.issue.opened"),),
                    ),
                    run=run_agent,
                ),
            ],
        )

        outcome = await dispatcher.publish_and_run(
            zeta_events.DraftEvent("github.issue.opened", "github", {})
        )

        assert started == ["agent.one", "agent.two"]
        assert [event.event_type for event in outcome.lifecycle_events] == [
            "runtime.queue_item.available",
            "runtime.queue_item.available",
            "runtime.queue_item.claimed",
            "runtime.attempt.started",
            "runtime.attempt.completed",
            "runtime.queue_item.completed",
            "runtime.queue_item.claimed",
            "runtime.attempt.started",
            "runtime.attempt.completed",
            "runtime.queue_item.completed",
        ]
        assert [
            event.payload["result"]["agent"]
            for event in outcome.lifecycle_events
            if event.event_type == "runtime.queue_item.completed"
        ] == [
            "agent.one",
            "agent.two",
        ]

    asyncio.run(run())


def test_zeta_event_dispatcher_matches_exact_event_type(tmp_path: Path) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    calls: list[str] = []

    dispatcher = zeta_dispatch.EventDispatcher(
        event_store,
        executors=[
            zeta_dispatch.ExecutableAgent(
                zeta_dispatch.AgentDefinition(
                    "exact-agent",
                    (zeta_dispatch.EventPattern("github.issue.opened"),),
                ),
                run=record_exact_agent_call(calls),
            ),
            zeta_dispatch.ExecutableAgent(
                zeta_dispatch.AgentDefinition(
                    "other-agent",
                    (zeta_dispatch.EventPattern("github.issue.closed"),),
                ),
                run=record_exact_agent_call(calls),
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
    assert [event.payload["target_agent"] for event in outcome.lifecycle_events] == [
        "exact-agent",
        "exact-agent",
        "exact-agent",
        "exact-agent",
        "exact-agent",
    ]


def test_zeta_event_dispatcher_records_unhandled_work_without_runner(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    published: list[zeta_events.Event] = []
    dispatcher = zeta_dispatch.EventDispatcher(
        event_store,
        routes=[
            zeta_dispatch.AgentRoute(
                "issue-triage",
                (zeta_dispatch.EventPattern("github.issue.opened"),),
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

    assert [event.event_type for event in outcome.lifecycle_events] == [
        "runtime.queue_item.available",
        "runtime.queue_item.unhandled",
    ]
    assert [event.idempotency_key for event in outcome.lifecycle_events] == [
        f"queue_item:{outcome.event.id}:issue-triage:available",
        f"queue_item:{outcome.event.id}:issue-triage:unhandled",
    ]
    assert outcome.lifecycle_events[0].payload == asdict(
        QueueItem(
            queue_item_id=f"qi_{outcome.event.id}_issue-triage",
            event_id=outcome.event.id,
            target_agent="issue-triage",
            status="available",
        )
    )
    assert outcome.lifecycle_events[1].payload == {
        **asdict(
            QueueItem(
                queue_item_id=f"qi_{outcome.event.id}_issue-triage",
                event_id=outcome.event.id,
                target_agent="issue-triage",
                status="unhandled",
            )
        ),
        "error": "no executor registered for 'issue-triage'",
    }
    assert [event.event_type for event in published] == [
        "github.issue.opened",
        "runtime.queue_item.available",
        "runtime.queue_item.unhandled",
    ]


def test_zeta_event_dispatcher_does_not_route_duplicate_events(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    calls = 0

    async def run_agent(run: zeta_dispatch.AgentInvocation) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"outcome": "handled"}

    dispatcher = zeta_dispatch.EventDispatcher(
        event_store,
        executors=[
            zeta_dispatch.ExecutableAgent(
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
    assert second.lifecycle_events == []
    assert [
        event.event_type for event in event_store.list_events(zeta_events.Filter())
    ] == [
        "github.issue.opened",
        "runtime.queue_item.available",
        "runtime.queue_item.claimed",
        "runtime.attempt.started",
        "runtime.attempt.completed",
        "runtime.queue_item.completed",
    ]


def test_zeta_event_dispatcher_records_failed_work(tmp_path: Path) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")

    async def fail_agent(run: zeta_dispatch.AgentInvocation) -> dict[str, object]:
        del run
        raise RuntimeError("boom")

    dispatcher = zeta_dispatch.EventDispatcher(
        event_store,
        executors=[
            zeta_dispatch.ExecutableAgent(
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

    assert [event.event_type for event in outcome.lifecycle_events] == [
        "runtime.queue_item.available",
        "runtime.queue_item.claimed",
        "runtime.attempt.started",
        "runtime.attempt.failed",
        "runtime.queue_item.failed",
    ]
    assert outcome.lifecycle_events[-1].payload["status"] == "failed"
    queue_item_id = f"qi_{outcome.event.id}_issue-triage"
    assert [event.idempotency_key for event in outcome.lifecycle_events] == [
        f"queue_item:{outcome.event.id}:issue-triage:available",
        f"queue_item:{outcome.event.id}:issue-triage:claimed:1",
        f"attempt:{queue_item_id}:1:started",
        f"attempt:{queue_item_id}:1:failed",
        f"queue_item:{outcome.event.id}:issue-triage:failed",
    ]
    assert zeta_dispatch.terminal_queue_item_result(
        outcome.lifecycle_events,
        event_id=outcome.event.id,
        target_agent="issue-triage",
    ) == {
        "outcome": "failed",
        "error": "RuntimeError: boom",
        "final_event_cursor": "6",
    }
    assert outcome.lifecycle_events[-1].payload == {
        **asdict(
            QueueItem(
                queue_item_id=queue_item_id,
                event_id=outcome.event.id,
                target_agent="issue-triage",
                status="failed",
            )
        ),
        "error": "RuntimeError: boom",
    }
    assert outcome.lifecycle_events[3].payload == asdict(
        Attempt(
            attempt_id=f"att_{queue_item_id}_1",
            queue_item_id=queue_item_id,
            event_id=outcome.event.id,
            attempt_number=1,
            target_agent="issue-triage",
            status="failed",
            started_at=outcome.lifecycle_events[2].payload["started_at"],
            finished_at=outcome.lifecycle_events[3].payload["finished_at"],
            error="RuntimeError: boom",
            session_id="repo",
        )
    )


def test_zeta_event_dispatcher_can_retry_failed_work(tmp_path: Path) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    attempts = 0

    async def flaky_agent(run: zeta_dispatch.AgentInvocation) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("boom")
        return {"outcome": "handled", "attempt_id": run.attempt_id}

    dispatcher = zeta_dispatch.EventDispatcher(
        event_store,
        executors=[
            zeta_dispatch.ExecutableAgent(
                zeta_dispatch.AgentDefinition(
                    "issue-triage",
                    (zeta_dispatch.EventPattern("github.issue.opened"),),
                ),
                run=flaky_agent,
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
    queue_item_id = f"qi_{outcome.event.id}_issue-triage"

    retry_event = dispatcher.schedule_retry(queue_item_id)
    retry_events = asyncio.run(dispatcher.run_queue_item(queue_item_id))
    attempts_rows = event_store.list_attempts()

    assert retry_event.event_type == "runtime.queue_item.available"
    assert retry_event.idempotency_key == (
        f"queue_item:{outcome.event.id}:issue-triage:available:2"
    )
    assert [event.event_type for event in retry_events] == [
        "runtime.queue_item.claimed",
        "runtime.attempt.started",
        "runtime.attempt.completed",
        "runtime.queue_item.completed",
    ]
    assert [row["attempt_number"] for row in attempts_rows] == [1, 2]
    assert attempts_rows[0]["status"] == "failed"
    assert attempts_rows[1]["status"] == "completed"
    assert retry_events[1].payload["attempt_id"] == f"att_{queue_item_id}_2"


def test_zeta_queue_item_snapshots_project_latest_lifecycle_state(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")

    async def run_agent(run: zeta_dispatch.AgentInvocation) -> dict[str, object]:
        del run
        return {"outcome": "handled"}

    dispatcher = zeta_dispatch.EventDispatcher(
        event_store,
        executors=[
            zeta_dispatch.ExecutableAgent(
                zeta_dispatch.AgentDefinition(
                    "issue-triage",
                    (zeta_dispatch.EventPattern("github.issue.opened"),),
                ),
                run=run_agent,
            )
        ],
    )
    handled = dispatch_event(
        dispatcher,
        zeta_events.DraftEvent(
            "github.issue.opened",
            "github",
            {"title": "Bug"},
            session_id="repo",
        ),
    )
    unhandled = dispatch_event(
        dispatcher,
        zeta_events.DraftEvent(
            "github.issue.closed",
            "github",
            {"title": "Done"},
            session_id="repo",
        ),
    )

    snapshots = zeta_dispatch.queue_item_snapshots(
        event_store.list_events(zeta_events.Filter())
    )

    assert snapshots == [
        zeta_dispatch.QueueItemSnapshot(
            queue_item_id=f"qi_{handled.event.id}_issue-triage",
            event_id=handled.event.id,
            target_agent="issue-triage",
            status="completed",
            last_event_type="runtime.queue_item.completed",
            cursor=handled.lifecycle_events[-1].cursor,
            result={"outcome": "handled"},
            error=None,
        ),
        zeta_dispatch.QueueItemSnapshot(
            queue_item_id=f"qi_{unhandled.event.id}_unhandled",
            event_id=unhandled.event.id,
            target_agent="",
            status="unhandled",
            last_event_type="runtime.queue_item.unhandled",
            cursor=unhandled.lifecycle_events[-1].cursor,
            result=None,
            error=None,
        ),
    ]
    assert zeta_dispatch.queue_item_status_counts(snapshots) == {
        "completed": 1,
        "unhandled": 1,
    }


def test_zeta_attempt_snapshots_project_latest_lifecycle_state(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")

    async def run_agent(run: zeta_dispatch.AgentInvocation) -> dict[str, object]:
        del run
        return {"outcome": "handled"}

    dispatcher = zeta_dispatch.EventDispatcher(
        event_store,
        executors=[
            zeta_dispatch.ExecutableAgent(
                zeta_dispatch.AgentDefinition(
                    "issue-triage",
                    (zeta_dispatch.EventPattern("github.issue.opened"),),
                ),
                run=run_agent,
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
            run_id="run-1",
        ),
    )
    queue_item_id = f"qi_{outcome.event.id}_issue-triage"

    snapshots = zeta_dispatch.attempt_snapshots(
        event_store.list_events(zeta_events.Filter())
    )

    assert snapshots == [
        zeta_dispatch.AttemptSnapshot(
            attempt_id=f"att_{queue_item_id}_1",
            queue_item_id=queue_item_id,
            event_id=outcome.event.id,
            attempt_number=1,
            target_agent="issue-triage",
            status="completed",
            last_event_type="runtime.attempt.completed",
            cursor=outcome.lifecycle_events[3].cursor,
            started_at=outcome.lifecycle_events[2].payload["started_at"],
            finished_at=outcome.lifecycle_events[3].payload["finished_at"],
            error=None,
            session_id="repo",
            run_id="run-1",
            result={"outcome": "handled"},
        )
    ]
    assert zeta_dispatch.attempt_status_counts(snapshots) == {"completed": 1}


def test_zeta_sqlite_event_store_projects_runtime_lifecycle_tables(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")

    async def run_agent(run: zeta_dispatch.AgentInvocation) -> dict[str, object]:
        del run
        return {
            "final_answer": "handled",
            "tool_calls": [{"name": "read"}],
            "usage": {"input_tokens": 12, "output_tokens": 3},
        }

    dispatcher = zeta_dispatch.EventDispatcher(
        event_store,
        executors=[
            zeta_dispatch.ExecutableAgent(
                zeta_dispatch.AgentDefinition(
                    "issue-triage",
                    (zeta_dispatch.EventPattern("github.issue.opened"),),
                ),
                run=run_agent,
            )
        ],
    )
    outcome = dispatch_event(
        dispatcher,
        zeta_events.DraftEvent(
            "github.issue.opened",
            "github",
            {},
            session_id="repo",
            run_id="run-1",
        ),
    )
    queue_item_id = f"qi_{outcome.event.id}_issue-triage"
    attempt_id = f"att_{queue_item_id}_1"

    queue_row = event_store.connection.execute(
        """
        SELECT queue_item_id, event_id, target_agent, status, attempt_count,
               last_error
        FROM queue_items
        WHERE queue_item_id = ?
        """,
        (queue_item_id,),
    ).fetchone()
    attempt_row = event_store.connection.execute(
        """
        SELECT attempt_id, queue_item_id, event_id, attempt_number, target_agent,
               status, session_id, run_id, error, summary, input_tokens,
               output_tokens, tool_calls_json
        FROM attempts
        WHERE attempt_id = ?
        """,
        (attempt_id,),
    ).fetchone()
    result_row = event_store.connection.execute(
        """
        SELECT attempt_id, final_status, result_json
        FROM attempt_results
        WHERE attempt_id = ?
        """,
        (attempt_id,),
    ).fetchone()
    session_mapping_row = event_store.connection.execute(
        """
        SELECT session_id, run_id
        FROM session_mappings
        WHERE session_id = ?
        """,
        ("repo",),
    ).fetchone()

    assert dict(queue_row) == {
        "queue_item_id": queue_item_id,
        "event_id": outcome.event.id,
        "target_agent": "issue-triage",
        "status": "completed",
        "attempt_count": 1,
        "last_error": None,
    }
    assert dict(attempt_row) == {
        "attempt_id": attempt_id,
        "queue_item_id": queue_item_id,
        "event_id": outcome.event.id,
        "attempt_number": 1,
        "target_agent": "issue-triage",
        "status": "completed",
        "session_id": "repo",
        "run_id": "run-1",
        "error": None,
        "summary": "handled",
        "input_tokens": 12,
        "output_tokens": 3,
        "tool_calls_json": '[{"name":"read"}]',
    }
    assert result_row["attempt_id"] == attempt_id
    assert result_row["final_status"] == "completed"
    assert json.loads(result_row["result_json"]) == {
        "final_answer": "handled",
        "tool_calls": [{"name": "read"}],
        "usage": {"input_tokens": 12, "output_tokens": 3},
    }
    assert dict(session_mapping_row) == {"session_id": "repo", "run_id": "run-1"}


def test_zeta_sqlite_event_store_projects_attempt_result_details(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")

    async def run_agent(run: zeta_dispatch.AgentInvocation) -> dict[str, object]:
        return {
            "final_answer": "handled issue",
            "events": [{"type": "issue.triaged"}],
            "tool_calls": [{"name": "read"}],
            "usage": {"input_tokens": 12, "output_tokens": 3},
            "event_id": run.triggering_event.id,
        }

    agent = zeta_dispatch.ExecutableAgent(
        zeta_dispatch.AgentDefinition(
            "issue-triage",
            (zeta_dispatch.EventPattern("github.issue.opened"),),
        ),
        run=run_agent,
    )
    dispatcher = zeta_dispatch.EventDispatcher(event_store, executors=[agent])

    outcome = dispatch_event(
        dispatcher,
        zeta_events.DraftEvent("github.issue.opened", "github", {}),
    )
    result_row = event_store.connection.execute(
        """
        SELECT summary, result_json, events_json, tool_calls_json, usage_json
        FROM attempt_results
        WHERE attempt_id = ?
        """,
        (f"att_qi_{outcome.event.id}_issue-triage_1",),
    ).fetchone()

    assert result_row["summary"] == "handled issue"
    assert json.loads(result_row["result_json"])["event_id"] == outcome.event.id
    assert json.loads(result_row["events_json"]) == [{"type": "issue.triaged"}]
    assert json.loads(result_row["tool_calls_json"]) == [{"name": "read"}]
    assert json.loads(result_row["usage_json"]) == {
        "input_tokens": 12,
        "output_tokens": 3,
    }


def test_zeta_sqlite_event_store_claims_and_reconciles_queue_leases(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    dispatcher = zeta_dispatch.EventDispatcher(
        event_store,
        routes=[
            zeta_dispatch.AgentRoute(
                "issue-triage",
                (zeta_dispatch.EventPattern("github.issue.opened"),),
            )
        ],
    )
    accepted = asyncio.run(
        dispatcher.publish_event(
            zeta_events.DraftEvent("github.issue.opened", "github", {})
        )
    ).event
    asyncio.run(dispatcher.route(accepted))
    queue_item_id = f"qi_{accepted.id}_issue-triage"
    now_ms = accepted.timestamp_ms + 1_000

    first_claim = event_store.claim_next_queue_item(
        "worker-a",
        lease_ms=1_000,
        now_ms=now_ms,
    )
    second_claim = event_store.claim_next_queue_item(
        "worker-b",
        lease_ms=1_000,
        now_ms=now_ms,
    )
    claimed_row = event_store.connection.execute(
        """
        SELECT status, claimed_by, claimed_until
        FROM queue_items
        WHERE queue_item_id = ?
        """,
        (queue_item_id,),
    ).fetchone()
    reconciled = event_store.reconcile_expired_queue_claims(now_ms=now_ms + 1_001)
    reclaimed = event_store.claim_next_queue_item(
        "worker-b",
        lease_ms=1_000,
        now_ms=now_ms + 1_001,
    )

    assert first_claim == queue_item_id
    assert second_claim is None
    assert dict(claimed_row) == {
        "status": "claimed",
        "claimed_by": "worker-a",
        "claimed_until": now_ms + 1_000,
    }
    assert reconciled == 1
    assert reclaimed == queue_item_id


def test_zeta_sqlite_event_store_claims_pending_queue_items(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    accepted = event_store.accept(
        zeta_events.DraftEvent("github.issue.opened", "github", {})
    ).event
    queue_item_id = event_store.ensure_pending_queue_item(accepted)
    event_store.ensure_pending_queue_item(accepted)
    now_ms = accepted.timestamp_ms + 1_000

    claimed = event_store.claim_next_queue_item(
        "worker-a",
        lease_ms=1_000,
        now_ms=now_ms,
    )
    reconciled = event_store.reconcile_expired_queue_claims(now_ms=now_ms + 1_001)
    reclaimed = event_store.claim_next_queue_item(
        "worker-b",
        lease_ms=1_000,
        now_ms=now_ms + 1_001,
    )
    rows = event_store.connection.execute(
        """
        SELECT queue_item_id
        FROM queue_items
        WHERE event_id = ?
        """,
        (accepted.id,),
    ).fetchall()

    assert queue_item_id == f"qi_{accepted.id}"
    assert claimed == queue_item_id
    assert reconciled == 1
    assert reclaimed == queue_item_id
    assert [row["queue_item_id"] for row in rows] == [queue_item_id]


def test_zeta_sqlite_event_store_acquires_locks_all_or_none(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")

    acquired = event_store.acquire_locks(
        ["context:repo", "branch:main"],
        "worker-a",
        lease_ms=1_000,
        now_ms=10_000,
    )
    blocked = event_store.acquire_locks(
        ["context:repo", "branch:feature"],
        "worker-b",
        lease_ms=1_000,
        now_ms=10_100,
    )

    assert acquired is True
    assert blocked is False
    assert event_store.list_locks() == [
        {
            "key": "branch:main",
            "owner": "worker-a",
            "acquired_at": 10_000,
            "expires_at": 11_000,
        },
        {
            "key": "context:repo",
            "owner": "worker-a",
            "acquired_at": 10_000,
            "expires_at": 11_000,
        },
    ]


def test_zeta_sqlite_event_store_reconciles_and_reacquires_expired_locks(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")

    assert event_store.acquire_locks(
        ["context:repo"],
        "worker-a",
        lease_ms=1_000,
        now_ms=10_000,
    )
    assert event_store.acquire_locks(
        ["context:repo"],
        "worker-b",
        lease_ms=1_000,
        now_ms=11_001,
    )
    assert event_store.acquire_locks(
        ["context:repo"],
        "worker-b",
        lease_ms=2_000,
        now_ms=11_500,
    )
    assert event_store.reconcile_expired_locks(now_ms=12_000) == 0
    assert event_store.release_locks(["context:repo"], "worker-a") == 0
    assert event_store.release_locks(["context:repo"], "worker-b") == 1
    assert event_store.list_locks() == []


def test_zeta_cli_queue_json_projects_runtime_queue(tmp_path: Path) -> None:
    state_dir = tmp_path / ".zeta"
    event_store = zeta_events.SqliteEventStore(event_store_path(state_dir))

    async def run_agent(run: zeta_dispatch.AgentInvocation) -> dict[str, object]:
        del run
        return {"outcome": "handled"}

    dispatcher = zeta_dispatch.EventDispatcher(
        event_store,
        executors=[
            zeta_dispatch.ExecutableAgent(
                zeta_dispatch.AgentDefinition(
                    "issue-triage",
                    (zeta_dispatch.EventPattern("github.issue.opened"),),
                ),
                run=run_agent,
            )
        ],
    )
    outcome = dispatch_event(
        dispatcher,
        zeta_events.DraftEvent("github.issue.opened", "github", {}, session_id="repo"),
    )

    result = CliRunner().invoke(
        zeta_cli.cli,
        ["queue", "--project-root", str(tmp_path), "--json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.output) == [
        {
            "queue_item_id": f"qi_{outcome.event.id}_issue-triage",
            "event_id": outcome.event.id,
            "target_agent": "issue-triage",
            "status": "completed",
            "available_at": outcome.lifecycle_events[0].timestamp_ms,
            "claimed_by": None,
            "claimed_until": None,
            "attempt_count": 1,
            "last_error": None,
            "updated_at": outcome.lifecycle_events[-1].timestamp_ms,
        }
    ]


def test_zeta_cli_attempts_json_projects_runtime_attempts(tmp_path: Path) -> None:
    state_dir = tmp_path / ".zeta"
    event_store = zeta_events.SqliteEventStore(event_store_path(state_dir))

    async def run_agent(run: zeta_dispatch.AgentInvocation) -> dict[str, object]:
        del run
        return {
            "final_answer": "handled",
            "events": [{"type": "issue.triaged"}],
            "tool_calls": [{"name": "read"}],
            "usage": {"input_tokens": 12, "output_tokens": 3},
        }

    dispatcher = zeta_dispatch.EventDispatcher(
        event_store,
        executors=[
            zeta_dispatch.ExecutableAgent(
                zeta_dispatch.AgentDefinition(
                    "issue-triage",
                    (zeta_dispatch.EventPattern("github.issue.opened"),),
                ),
                run=run_agent,
            )
        ],
    )
    outcome = dispatch_event(
        dispatcher,
        zeta_events.DraftEvent("github.issue.opened", "github", {}, session_id="repo"),
    )
    queue_item_id = f"qi_{outcome.event.id}_issue-triage"

    result = CliRunner().invoke(
        zeta_cli.cli,
        ["attempts", "--project-root", str(tmp_path), "--json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.output) == [
        {
            "attempt_id": f"att_{queue_item_id}_1",
            "queue_item_id": queue_item_id,
            "event_id": outcome.event.id,
            "attempt_number": 1,
            "target_agent": "issue-triage",
            "worker_name": None,
            "status": "completed",
            "started_at": outcome.lifecycle_events[2].payload["started_at"],
            "heartbeat_at": outcome.lifecycle_events[3].timestamp_ms,
            "finished_at": outcome.lifecycle_events[3].payload["finished_at"],
            "error": None,
            "session_id": "repo",
            "run_id": None,
            "input_tokens": 12,
            "output_tokens": 3,
            "final_status": "completed",
            "summary": "handled",
            "result": {
                "final_answer": "handled",
                "events": [{"type": "issue.triaged"}],
                "tool_calls": [{"name": "read"}],
                "usage": {"input_tokens": 12, "output_tokens": 3},
            },
            "events": [{"type": "issue.triaged"}],
            "tool_calls": [{"name": "read"}],
            "usage": {"input_tokens": 12, "output_tokens": 3},
        }
    ]


def test_zeta_cli_events_json_lists_durable_events(tmp_path: Path) -> None:
    state_dir = tmp_path / ".zeta"
    event_store = zeta_events.SqliteEventStore(event_store_path(state_dir))
    event = event_store.accept(
        zeta_events.DraftEvent(
            "github.issue.opened",
            "github",
            {"title": "Bug"},
            idempotency_key="issue-1",
            session_id="repo",
            run_id="run-1",
        )
    ).event

    result = CliRunner().invoke(
        zeta_cli.cli,
        ["events", "--project-root", str(tmp_path), "--json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.output) == [
        {
            "id": event.id,
            "type": "github.issue.opened",
            "source": "github",
            "payload": {"title": "Bug"},
            "idempotency_key": "issue-1",
            "caused_by": None,
            "session_id": "repo",
            "run_id": "run-1",
            "turn_id": None,
            "timestamp_ms": event.timestamp_ms,
            "cursor": event.cursor,
        }
    ]


def test_zeta_cli_events_filters_default_listing(tmp_path: Path) -> None:
    state_dir = tmp_path / ".zeta"
    event_store = zeta_events.SqliteEventStore(event_store_path(state_dir))
    event_store.accept(
        zeta_events.DraftEvent("github.issue.opened", "github", {}, session_id="repo")
    )
    selected = event_store.accept(
        zeta_events.DraftEvent("runtime.queue_item.available", "zeta", {})
    ).event

    result = CliRunner().invoke(
        zeta_cli.cli,
        [
            "events",
            "--project-root",
            str(tmp_path),
            "--type-prefix",
            "runtime.",
            "--limit",
            "1",
        ],
    )

    assert result.exit_code == 0
    assert result.output == (
        f"{selected.cursor}\truntime.queue_item.available\tzeta\t{selected.id}\n"
    )


def test_zeta_cli_status_counts_runtime_queue(tmp_path: Path) -> None:
    state_dir = tmp_path / ".zeta"
    event_store = zeta_events.SqliteEventStore(event_store_path(state_dir))
    dispatcher = zeta_dispatch.EventDispatcher(event_store)

    dispatch_event(
        dispatcher,
        zeta_events.DraftEvent("github.issue.opened", "github", {}, session_id="repo"),
    )

    result = CliRunner().invoke(
        zeta_cli.cli,
        ["status", "--project-root", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert result.output == "unhandled: 1\n"


def test_zeta_cli_run_once_routes_unhandled_event(tmp_path: Path) -> None:
    state_dir = tmp_path / ".zeta"
    event_store = zeta_events.SqliteEventStore(event_store_path(state_dir))
    event = event_store.accept(
        zeta_events.DraftEvent("github.issue.opened", "github", {}, session_id="repo")
    ).event

    result = CliRunner().invoke(
        zeta_cli.cli,
        ["run", "--project-root", str(tmp_path), "--once"],
    )
    snapshots = zeta_dispatch.queue_item_snapshots(
        event_store.list_events(zeta_events.Filter())
    )

    assert result.exit_code == 0
    assert result.output == f"routed {event.id}\n"
    assert [snapshot.status for snapshot in snapshots] == ["unhandled"]


def test_zeta_local_runtime_builds_project_services(tmp_path: Path) -> None:
    runtime = zeta_process.build_runtime(project_root=tmp_path)

    try:
        assert runtime.project_root == tmp_path.resolve()
        assert runtime.state_dir == tmp_path.resolve() / ".zeta"
        assert runtime.executors == ()
        assert runtime.events.path == event_store_path(runtime.state_dir)
    finally:
        runtime.close()


def test_zeta_local_runtime_run_once_executes_available_queue_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / ".zeta"
    event_store = zeta_events.SqliteEventStore(event_store_path(state_dir))
    event = event_store.accept(
        zeta_events.DraftEvent("github.issue.opened", "github", {}, session_id="repo")
    ).event

    async def run_agent(run: zeta_dispatch.AgentInvocation) -> dict[str, object]:
        return {"event_id": run.triggering_event.id}

    def compile_agents(spec: object) -> list[zeta_dispatch.ExecutableAgent]:
        del spec
        return [
            zeta_dispatch.ExecutableAgent(
                zeta_dispatch.AgentDefinition(
                    "issue-triage",
                    (zeta_dispatch.EventPattern("github.issue.opened"),),
                ),
                run=run_agent,
            )
        ]

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "triage.md").write_text(
        """---
name: Triage
description: Triage issues.
accepts:
  - github.issue.opened
---
Triage the issue.
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(zeta_process, "compile_agent_definitions", compile_agents)
    runtime = zeta_process.build_runtime(project_root=tmp_path)

    try:
        message = asyncio.run(zeta_worker.run_once(runtime))
        snapshots = zeta_dispatch.queue_item_snapshots(
            event_store.list_events(zeta_events.Filter())
        )
        attempt_rows = event_store.list_attempts()
    finally:
        runtime.close()

    assert message == f"ran qi_{event.id}"
    assert attempt_rows[0]["worker_name"] == "local-runtime"
    assert snapshots == [
        zeta_dispatch.QueueItemSnapshot(
            queue_item_id=f"qi_{event.id}",
            event_id=event.id,
            target_agent="issue-triage",
            status="completed",
            last_event_type="runtime.queue_item.completed",
            cursor=snapshots[0].cursor,
            result={"event_id": event.id},
            error=None,
        )
    ]


def test_zeta_local_runtime_heartbeats_running_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    accepted = event_store.accept(
        zeta_events.DraftEvent("github.issue.opened", "github", {})
    ).event
    heartbeat_rows: list[dict[str, int]] = []

    async def run_agent(run: zeta_dispatch.AgentInvocation) -> dict[str, object]:
        deadline = asyncio.get_running_loop().time() + 1
        first_heartbeat_at: int | None = None
        while asyncio.get_running_loop().time() < deadline:
            attempts = event_store.list_attempts()
            if attempts:
                heartbeat_at = int(attempts[0]["heartbeat_at"])
                if first_heartbeat_at is None:
                    first_heartbeat_at = heartbeat_at
                elif heartbeat_at > first_heartbeat_at:
                    queue_item = event_store.list_queue_items()[0]
                    heartbeat_rows.append(
                        {
                            "heartbeat_at": heartbeat_at,
                            "claimed_until": int(queue_item["claimed_until"]),
                        }
                    )
                    return {"event_id": run.triggering_event.id}
            await asyncio.sleep(0.005)
        raise AssertionError("attempt heartbeat was not refreshed")

    agent = zeta_dispatch.ExecutableAgent(
        zeta_dispatch.AgentDefinition(
            "issue-triage",
            (zeta_dispatch.EventPattern("github.issue.opened"),),
        ),
        run=run_agent,
    )
    runtime = zeta_worker.RuntimeServices(
        project_root=tmp_path,
        state_dir=tmp_path,
        events=event_store,
        specs=(),
        executors=(agent,),
    )
    monkeypatch.setattr(zeta_worker, "ATTEMPT_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(zeta_worker, "QUEUE_LEASE_MS", 1_000)

    message = asyncio.run(zeta_worker.run_once(runtime))
    attempt = event_store.list_attempts()[0]
    queue_item = event_store.list_queue_items()[0]

    assert message == f"ran qi_{accepted.id}"
    assert heartbeat_rows
    assert attempt["status"] == "completed"
    assert int(attempt["heartbeat_at"]) >= heartbeat_rows[0]["heartbeat_at"]
    assert int(queue_item["claimed_until"]) >= heartbeat_rows[0]["claimed_until"]
    assert queue_item["claimed_by"] == "local-runtime"


def test_zeta_local_runtime_run_once_skips_leased_queue_item(tmp_path: Path) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    calls: list[zeta_dispatch.AgentInvocation] = []

    async def run_agent(run: zeta_dispatch.AgentInvocation) -> dict[str, object]:
        calls.append(run)
        return {"event_id": run.triggering_event.id}

    agent = zeta_dispatch.ExecutableAgent(
        zeta_dispatch.AgentDefinition(
            "issue-triage",
            (zeta_dispatch.EventPattern("github.issue.opened"),),
        ),
        run=run_agent,
    )
    dispatcher = zeta_dispatch.EventDispatcher(event_store, executors=[agent])
    accepted = asyncio.run(
        dispatcher.publish_event(
            zeta_events.DraftEvent("github.issue.opened", "github", {})
        )
    ).event
    asyncio.run(dispatcher.route(accepted))
    queue_item_id = f"qi_{accepted.id}_issue-triage"
    event_store.claim_next_queue_item(
        "worker-a",
        lease_ms=60_000,
        now_ms=accepted.timestamp_ms + 1_000,
    )
    runtime = zeta_worker.RuntimeServices(
        project_root=tmp_path,
        state_dir=tmp_path,
        events=event_store,
        specs=(),
        executors=(agent,),
    )

    message = asyncio.run(zeta_worker.run_once(runtime))
    queue_row = event_store.connection.execute(
        "SELECT status, claimed_by FROM queue_items WHERE queue_item_id = ?",
        (queue_item_id,),
    ).fetchone()

    assert message == "queue empty"
    assert calls == []
    assert dict(queue_row) == {"status": "claimed", "claimed_by": "worker-a"}


def test_zeta_local_runtime_run_once_releases_claim_when_lock_is_busy(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    calls: list[zeta_dispatch.AgentInvocation] = []

    async def run_agent(run: zeta_dispatch.AgentInvocation) -> dict[str, object]:
        calls.append(run)
        return {"event_id": run.triggering_event.id}

    agent = zeta_dispatch.ExecutableAgent(
        zeta_dispatch.AgentDefinition(
            "issue-triage",
            (zeta_dispatch.EventPattern("github.issue.opened"),),
            lock_keys=("context:repo",),
        ),
        run=run_agent,
    )
    dispatcher = zeta_dispatch.EventDispatcher(event_store, executors=[agent])
    accepted = asyncio.run(
        dispatcher.publish_event(
            zeta_events.DraftEvent("github.issue.opened", "github", {})
        )
    ).event
    asyncio.run(dispatcher.route(accepted))
    assert event_store.acquire_locks(
        ["context:repo"],
        "worker-a",
        lease_ms=60_000,
        now_ms=accepted.timestamp_ms + 1_000,
    )
    runtime = zeta_worker.RuntimeServices(
        project_root=tmp_path,
        state_dir=tmp_path,
        events=event_store,
        specs=(),
        executors=(agent,),
    )

    message = asyncio.run(zeta_worker.run_once(runtime))
    queue_row = event_store.connection.execute(
        "SELECT status, claimed_by FROM queue_items WHERE queue_item_id = ?",
        (f"qi_{accepted.id}_issue-triage",),
    ).fetchone()

    assert message == "queue empty"
    assert calls == []
    assert dict(queue_row) == {"status": "available", "claimed_by": None}


def test_zeta_local_runtime_run_once_skips_lock_busy_item_and_runs_next(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    calls: list[str] = []

    async def run_agent(run: zeta_dispatch.AgentInvocation) -> dict[str, object]:
        calls.append(run.agent.agent_id)
        return {"event_id": run.triggering_event.id}

    locked_agent = zeta_dispatch.ExecutableAgent(
        zeta_dispatch.AgentDefinition(
            "locked",
            (zeta_dispatch.EventPattern("repo.locked"),),
            lock_keys=("context:repo",),
        ),
        run=run_agent,
    )
    free_agent = zeta_dispatch.ExecutableAgent(
        zeta_dispatch.AgentDefinition(
            "free",
            (zeta_dispatch.EventPattern("repo.free"),),
        ),
        run=run_agent,
    )
    dispatcher = zeta_dispatch.EventDispatcher(
        event_store,
        executors=[locked_agent, free_agent],
    )
    locked_event = asyncio.run(
        dispatcher.publish_event(zeta_events.DraftEvent("repo.locked", "test", {}))
    ).event
    free_event = asyncio.run(
        dispatcher.publish_event(zeta_events.DraftEvent("repo.free", "test", {}))
    ).event
    asyncio.run(dispatcher.route(locked_event))
    asyncio.run(dispatcher.route(free_event))
    event_store.connection.execute(
        """
        UPDATE queue_items
        SET available_at = CASE queue_item_id
          WHEN ? THEN 1
          WHEN ? THEN 2
          ELSE available_at
        END
        """,
        (f"qi_{locked_event.id}_locked", f"qi_{free_event.id}_free"),
    )
    event_store.connection.commit()
    assert event_store.acquire_locks(
        ["context:repo"],
        "worker-a",
        lease_ms=60_000,
        now_ms=zeta_worker.runtime_time_ms(),
    )
    runtime = zeta_worker.RuntimeServices(
        project_root=tmp_path,
        state_dir=tmp_path,
        events=event_store,
        specs=(),
        executors=(locked_agent, free_agent),
    )

    message = asyncio.run(zeta_worker.run_once(runtime))
    queue_rows = {
        row["queue_item_id"]: row
        for row in event_store.connection.execute(
            """
            SELECT queue_item_id, status, claimed_by
            FROM queue_items
            WHERE queue_item_id IN (?, ?)
            """,
            (f"qi_{locked_event.id}_locked", f"qi_{free_event.id}_free"),
        ).fetchall()
    }

    assert message == f"ran qi_{free_event.id}_free"
    assert calls == ["free"]
    assert dict(queue_rows[f"qi_{locked_event.id}_locked"]) == {
        "queue_item_id": f"qi_{locked_event.id}_locked",
        "status": "available",
        "claimed_by": None,
    }
    assert dict(queue_rows[f"qi_{free_event.id}_free"]) == {
        "queue_item_id": f"qi_{free_event.id}_free",
        "status": "completed",
        "claimed_by": "local-runtime",
    }


def test_zeta_local_runtime_run_once_fans_out_pending_queue_item(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    accepted = event_store.accept(
        zeta_events.DraftEvent("github.issue.opened", "github", {})
    ).event
    calls: list[zeta_dispatch.AgentInvocation] = []

    async def run_agent(run: zeta_dispatch.AgentInvocation) -> dict[str, object]:
        calls.append(run)
        return {"event_id": run.triggering_event.id}

    agents = (
        zeta_dispatch.ExecutableAgent(
            zeta_dispatch.AgentDefinition(
                "agent.one",
                (zeta_dispatch.EventPattern("github.issue.opened"),),
            ),
            run=run_agent,
        ),
        zeta_dispatch.ExecutableAgent(
            zeta_dispatch.AgentDefinition(
                "agent.two",
                (zeta_dispatch.EventPattern("github.issue.opened"),),
            ),
            run=run_agent,
        ),
    )
    runtime = zeta_worker.RuntimeServices(
        project_root=tmp_path,
        state_dir=tmp_path,
        events=event_store,
        specs=(),
        executors=agents,
    )

    message = asyncio.run(zeta_worker.run_once(runtime))
    snapshots = zeta_dispatch.queue_item_snapshots(
        event_store.list_events(zeta_events.Filter())
    )

    assert message == f"routed {accepted.id}"
    assert calls == []
    assert [
        (snapshot.queue_item_id, snapshot.target_agent, snapshot.status)
        for snapshot in snapshots
    ] == [
        (f"qi_{accepted.id}", "", "completed"),
        (f"qi_{accepted.id}_agent_one", "agent.one", "available"),
        (f"qi_{accepted.id}_agent_two", "agent.two", "available"),
    ]


def test_zeta_local_runtime_run_once_handles_eventlog_rpc_request(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    stored = event_store.accept(
        DraftEvent(
            event_type="zeta.user_message",
            source="test",
            payload={"content": "hello"},
            session_id="ctx-session",
        )
    ).event
    request = event_store.accept(
        zeta_rpc.rpc_requested_draft(
            "events.list",
            {"event_type": "zeta.user_message"},
            request_id="req_runtime",
            session_id="ctx-session",
        )
    ).event
    runtime = zeta_worker.RuntimeServices(
        project_root=tmp_path,
        state_dir=tmp_path,
        events=event_store,
        specs=(),
        executors=(),
    )

    message = asyncio.run(zeta_worker.run_once(runtime))
    response = event_store.children(request.id)[0]

    assert message == f"rpc {request.id}"
    assert response.event_type == "rpc.responded"
    assert response.payload["request_id"] == "req_runtime"
    assert response.payload["result"]["events"][0]["id"] == stored.id
    assert event_store.list_queue_items() == []


@pytest.mark.parametrize(
    ("cron", "expected"),
    [
        ("34 12 * * *", True),
        ("*/5 12 * * *", False),
        ("30-40 12 * * *", True),
        ("34 13 * * *", False),
    ],
)
def test_zeta_local_runtime_cron_matcher_supports_basic_v0_shapes(
    cron: str,
    expected: bool,
) -> None:
    assert (
        zeta_scheduling.cron_matches(
            cron,
            datetime(2026, 6, 22, 12, 34, tzinfo=UTC),
        )
        is expected
    )


def test_zeta_local_runtime_emits_due_schedules_once_per_minute(
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "scheduled.md").write_text(
        """---
name: Scheduled
description: Runs on a schedule.
accepts:
  - repo.digest.requested
schedules:
  - cron: "* * * * *"
    event: repo.digest.requested
    payload:
      reason: scheduled
---
Summarize the repo.
""",
        encoding="utf-8",
    )
    runtime = zeta_process.build_runtime(project_root=tmp_path)

    try:
        first = zeta_scheduling.emit_due_schedules(
            runtime.events,
            runtime.specs,
            now=datetime(2026, 6, 22, 12, 34, 56, tzinfo=UTC),
        )
        second = zeta_scheduling.emit_due_schedules(
            runtime.events,
            runtime.specs,
            now=datetime(2026, 6, 22, 12, 34, 59, tzinfo=UTC),
        )
        events = runtime.events.list_events(zeta_events.Filter())
    finally:
        runtime.close()

    assert [event.event_type for event in first] == ["repo.digest.requested"]
    assert second == []
    assert [(event.event_type, event.payload) for event in events] == [
        ("repo.digest.requested", {"reason": "scheduled"})
    ]


def test_zeta_local_runtime_emits_generic_schedule_event(
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "scheduled.md").write_text(
        """---
name: Scheduled
description: Runs on a schedule.
accepts:
  - runtime.schedule.triggered
schedules:
  - cron: "* * * * *"
---
Summarize the repo.
""",
        encoding="utf-8",
    )
    runtime = zeta_process.build_runtime(project_root=tmp_path)

    try:
        emitted = zeta_scheduling.emit_due_schedules(
            runtime.events,
            runtime.specs,
            now=datetime(2026, 6, 22, 12, 34, 56, tzinfo=UTC),
        )
    finally:
        runtime.close()

    assert [(event.event_type, event.payload) for event in emitted] == [
        (
            "runtime.schedule.triggered",
            {"agent_name": "Scheduled", "cron": "* * * * *"},
        )
    ]


def test_zeta_local_runtime_run_once_ingests_due_schedule_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[zeta_dispatch.AgentInvocation] = []

    async def run_agent(run: zeta_dispatch.AgentInvocation) -> dict[str, object]:
        calls.append(run)
        return {"event_type": run.triggering_event.event_type}

    def compile_agents(spec: object) -> list[zeta_dispatch.ExecutableAgent]:
        del spec
        return [
            zeta_dispatch.ExecutableAgent(
                zeta_dispatch.AgentDefinition(
                    "scheduled",
                    (zeta_dispatch.EventPattern("repo.digest.requested"),),
                ),
                run=run_agent,
            )
        ]

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "scheduled.md").write_text(
        """---
name: Scheduled
description: Runs on a schedule.
accepts:
  - repo.digest.requested
schedules:
  - cron: "* * * * *"
    event: repo.digest.requested
    payload:
      reason: scheduled
---
Summarize the repo.
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(zeta_process, "compile_agent_definitions", compile_agents)
    monkeypatch.setattr(
        zeta_scheduling,
        "utc_now",
        lambda: datetime(2026, 6, 22, 12, 34, tzinfo=UTC),
    )
    runtime = zeta_process.build_runtime(project_root=tmp_path)

    try:
        message = asyncio.run(zeta_worker.run_once(runtime))
        snapshots = zeta_dispatch.queue_item_snapshots(
            runtime.events.list_events(zeta_events.Filter())
        )
    finally:
        runtime.close()

    assert message == f"ran {snapshots[0].queue_item_id}"
    assert [call.triggering_event.payload for call in calls] == [
        {"reason": "scheduled"}
    ]
    assert [snapshot.status for snapshot in snapshots] == ["completed"]


def test_zeta_local_runtime_run_forever_reuses_run_once_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / ".zeta"
    event_store = zeta_events.SqliteEventStore(event_store_path(state_dir))
    event = event_store.accept(
        zeta_events.DraftEvent("github.issue.opened", "github", {}, session_id="repo")
    ).event
    calls: list[zeta_dispatch.AgentInvocation] = []

    async def exercise() -> None:
        stop_event = asyncio.Event()

        async def run_agent(run: zeta_dispatch.AgentInvocation) -> dict[str, object]:
            calls.append(run)
            stop_event.set()
            return {"event_id": run.triggering_event.id}

        def compile_agents(spec: object) -> list[zeta_dispatch.ExecutableAgent]:
            del spec
            return [
                zeta_dispatch.ExecutableAgent(
                    zeta_dispatch.AgentDefinition(
                        "issue-triage",
                        (zeta_dispatch.EventPattern("github.issue.opened"),),
                    ),
                    run=run_agent,
                )
            ]

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "triage.md").write_text(
            """---
name: Triage
description: Triage issues.
accepts:
  - github.issue.opened
---
Triage the issue.
""",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            zeta_process,
            "compile_agent_definitions",
            compile_agents,
        )
        runtime = zeta_process.build_runtime(project_root=tmp_path)
        try:
            await zeta_worker.run_forever(
                runtime,
                poll_interval_seconds=0,
                stop_event=stop_event,
            )
        finally:
            runtime.close()

    asyncio.run(exercise())
    snapshots = zeta_dispatch.queue_item_snapshots(
        event_store.list_events(zeta_events.Filter())
    )

    assert [call.triggering_event.id for call in calls] == [event.id]
    assert [snapshot.status for snapshot in snapshots] == ["completed"]


def test_zeta_local_runtime_run_forever_respects_max_concurrent(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    events = [
        event_store.accept(
            zeta_events.DraftEvent(
                "github.issue.opened",
                "github",
                {"index": index},
            )
        ).event
        for index in range(2)
    ]
    started: list[str] = []

    async def exercise() -> None:
        stop_event = asyncio.Event()
        both_started = asyncio.Event()
        release = asyncio.Event()

        async def run_agent(run: zeta_dispatch.AgentInvocation) -> dict[str, object]:
            started.append(run.triggering_event.id)
            if len(started) == 2:
                both_started.set()
            await both_started.wait()
            release.set()
            stop_event.set()
            return {"event_id": run.triggering_event.id}

        runtime = zeta_worker.RuntimeServices(
            project_root=tmp_path,
            state_dir=tmp_path,
            events=event_store,
            specs=(),
            executors=(
                zeta_dispatch.ExecutableAgent(
                    zeta_dispatch.AgentDefinition(
                        "issue-triage",
                        (zeta_dispatch.EventPattern("github.issue.opened"),),
                    ),
                    run=run_agent,
                ),
            ),
            max_concurrent=2,
        )

        worker = asyncio.create_task(
            zeta_worker.run_forever(
                runtime,
                poll_interval_seconds=0,
                stop_event=stop_event,
            )
        )
        await asyncio.wait_for(release.wait(), timeout=1)
        await worker

    asyncio.run(exercise())
    snapshots = zeta_dispatch.queue_item_snapshots(
        event_store.list_events(zeta_events.Filter())
    )

    assert sorted(started) == sorted(event.id for event in events)
    assert [snapshot.status for snapshot in snapshots] == ["completed", "completed"]


def test_zeta_cli_run_forever_invokes_runtime_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Path] = {}

    async def run_forever(runtime: zeta_worker.RuntimeServices) -> None:
        captured["project_root"] = runtime.project_root

    monkeypatch.setattr(zeta_worker, "run_forever", run_forever)

    result = CliRunner().invoke(
        zeta_cli.cli,
        ["run", "--project-root", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert captured == {"project_root": tmp_path.resolve()}


def test_zeta_cli_run_once_executes_one_available_queue_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / ".zeta"
    event_store = zeta_events.SqliteEventStore(event_store_path(state_dir))
    event = event_store.accept(
        zeta_events.DraftEvent("github.issue.opened", "github", {}, session_id="repo")
    ).event
    calls: list[zeta_dispatch.AgentInvocation] = []

    async def run_agent(run: zeta_dispatch.AgentInvocation) -> dict[str, object]:
        calls.append(run)
        return {"outcome": "handled"}

    def compile_agents(spec: object) -> list[zeta_dispatch.ExecutableAgent]:
        del spec
        return [
            zeta_dispatch.ExecutableAgent(
                zeta_dispatch.AgentDefinition(
                    "issue-triage",
                    (zeta_dispatch.EventPattern("github.issue.opened"),),
                ),
                run=run_agent,
            )
        ]

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "issue-triage.md").write_text(
        """---
name: Issue Triage
description: Triage issues.
accepts:
  - github.issue.opened
---
Triage {{ event.payload.title }}
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(zeta_process, "compile_agent_definitions", compile_agents)

    result = CliRunner().invoke(
        zeta_cli.cli,
        ["run", "--project-root", str(tmp_path), "--once"],
    )
    snapshots = zeta_dispatch.queue_item_snapshots(
        event_store.list_events(zeta_events.Filter())
    )

    assert result.exit_code == 0
    assert result.output == f"ran qi_{event.id}\n"
    assert len(calls) == 1
    assert [snapshot.status for snapshot in snapshots] == ["completed"]


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
    monkeypatch.setattr(zeta_capability_execution, "invoke_capability", fake_invoke)

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
        zeta_capability_execution,
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
        zeta_capability_execution,
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
        zeta_capability_execution,
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

    monkeypatch.setattr(zeta_capability_execution, "invoke_capability", fake_invoke)

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
        zeta_capability_execution,
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
        zeta_capability_execution,
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

    monkeypatch.setattr(zeta_capability_execution, "invoke_capability", fake_invoke)

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
        zeta_capability_execution,
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
        zeta_capability_execution,
        "invoke_capability",
        lambda name, params, **kwargs: {"ok": True},
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

    with pytest.raises(zeta_agent.AgentRunAborted) as raised:
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
        zeta_capability_execution,
        "invoke_capability",
        lambda name, params, **kwargs: read_tool_payload(target),
    )

    with pytest.raises(zeta_agent.AgentRunAborted) as raised:
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
    monkeypatch.setattr(zeta_capability_execution, "invoke_capability", crash_invoke)

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


def test_zeta_agent_turn_runs_tool_call_without_schema_validation(monkeypatch) -> None:
    ran_with: list[dict[str, Any]] = []

    def fake_invoke(
        name: str,
        params: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        del name, kwargs
        ran_with.append(params)
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
    monkeypatch.setattr(zeta_capability_execution, "invoke_capability", fake_invoke)

    result = run_agent_turn(
        "inspect",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("read",), max_turns=1),
    )

    assert ran_with == [{"path": "README.md", "unexpected": True}]
    tool_result = next(
        event
        for event in timeline_events(result.events)
        if event.get("type") == "tool_result"
    )
    assert tool_result["result"]["ok"] is True
    assert tool_result["status"] == "completed"


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
    monkeypatch.setattr(zeta_capability_execution, "invoke_capability", fail_invoke)

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
