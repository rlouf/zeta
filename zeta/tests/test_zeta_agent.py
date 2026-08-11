"""Agent loop tests."""

import asyncio
import json
import logging
import os
import subprocess
import sys
import sysconfig
import threading
import tomllib
from collections.abc import Callable, Coroutine, Iterable, Mapping
from dataclasses import asdict, fields, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import zeta.capabilities.execution as zeta_capability_execution
import zeta.capabilities.executors as zeta_capability_executors
import zeta.cli.commands.ipc as cli_ipc
import zeta.context.transforms as zeta_content_transforms
import zeta.loop.cancellation as zeta_loop_cancellation
import zeta.loop.gateway as zeta_loop_gateway
import zeta.loop.stages.capability as loop_capability
import zeta.loop.stages.model as loop_model
import zeta.loop.stages.prompt as loop_prompt
import zeta.loop.steps as zeta_loop_steps
import zeta.loop.types as zeta_loop_types
import zeta.models.chat_completions as zeta_model
import zeta.models.endpoint as zeta_model_endpoint
import zeta.models.sse as zeta_model_sse
import zeta.models.types as zeta_model_shapes
from click import Group
from click.testing import CliRunner
from zeta.authoring import spec as zeta_agent_spec
from zeta.authoring.manifest import ManifestError
from zeta.capabilities.executors import (
    InProcessCapabilityExecutor,
    ToolExecutor,
    ToolExecutorProvider,
    ToolExecutorProviderRegistry,
)
from zeta.capabilities.registry import CapabilityRegistry, RegisteredCapability
from zeta.capabilities.types import (
    Capability,
    CapabilityId,
)
from zeta.cli import main as cli_main
from zeta.context import builder as zeta_context
from zeta.context.compaction import CompactionPolicy
from zeta.effects import DeliverySemantics
from zeta.events import DraftEvent, Event
from zeta.harness import connector_bridge as harness_connector_bridge
from zeta.harness import dispatch as harness_dispatch
from zeta.harness import queue as harness_queue
from zeta.harness import retry as harness_retry
from zeta.harness import routing as harness_routing
from zeta.harness import scheduling as harness_scheduling
from zeta.harness import session_turn as harness_session_turn
from zeta.harness import templates as harness_templates
from zeta.harness import worker as harness_worker
from zeta.harness.queue import QueueItem
from zeta.harness.sessions import submit_session_message
from zeta.harness.store import RuntimeEventStore
from zeta.ipc import connection as ipc_connection
from zeta.ipc import framing as ipc_framing
from zeta.ipc import routes as ipc_routes
from zeta.journal import drafts as zeta_event_drafts
from zeta.journal import views as zeta_event_views
from zeta.journal import wire as zeta_event_wire
from zeta.journal.memory import MemoryEventStore
from zeta.journal.sqlite import event_store_path
from zeta.journal.store import Filter
from zeta.loop import outcomes as zeta_outcomes
from zeta.loop import runtime as zeta_agent
from zeta.loop import runtime_context as zeta_runtime_context
from zeta.loop import thread_run as zeta_requests
from zeta.loop.request import ContentTransformBudget
from zeta.loop.runtime import AgentRunResult
from zeta.models.profiles import ModelSelection
from zeta.substrate import InMemoryStore
from zeta.tools import ensure_builtin_tools_registered, register_builtin_tools
from zeta_test_support import (
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

from zeta import models as zeta_models_api


def _as_async(fn: Any) -> Any:
    """Wrap a synchronous test double so it can stand in for an async function."""

    async def call(*args: Any, **kwargs: Any) -> Any:
        return fn(*args, **kwargs)

    return call


zeta_trace = SimpleNamespace(InMemoryStore=InMemoryStore)

ensure_builtin_tools_registered()

OBJECT_ID_A = "b3:" + "a" * 64
OBJECT_ID_B = "b3:" + "b" * 64
OBJECT_ID_C = "b3:" + "c" * 64
OBJECT_ID_D = "b3:" + "d" * 64


def runtime_sqlite_event_store(path: Path) -> RuntimeEventStore:
    return RuntimeEventStore.open(path)


zeta_events = SimpleNamespace(
    DraftEvent=DraftEvent,
    Event=Event,
    Filter=Filter,
    MemoryEventStore=MemoryEventStore,
    SqliteEventStore=runtime_sqlite_event_store,
)


def write_project_event_schema(
    project_root: Path,
    event_type: str,
    schema: dict[str, Any] | None = None,
) -> None:
    events_dir = project_root / "agents" / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    (events_dir / f"{event_type}.json").write_text(
        json.dumps(schema or {"type": "object"}),
        encoding="utf-8",
    )


def test_zeta_agent_run_result_payload_serializes_result_boundary() -> None:
    draft = DraftEvent(
        event_type="issue.triaged",
        source="agent",
        payload={"status": "done"},
        session_id="session-1",
        run_id="run-1",
    )
    result = AgentRunResult(
        final_answer="done",
        events=[draft],
    )

    assert zeta_outcomes.agent_run_result_payload(result) == {
        "final_answer": "done",
        "events": [asdict(draft)],
    }


def ipc_event(
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
        zeta_event_views.event_view(event)
        if isinstance(event, Event)
        else zeta_event_views.draft_event_view(event)
        for event in events
    ]


def run_agent_turn(*args: Any, **kwargs: Any) -> AgentRunResult:
    if "tool_executor" not in kwargs:
        kwargs["tool_executor"] = zeta_capability_executors.local_tool_executor(
            kwargs.get("tool_registry")
        )
    return asyncio.run(zeta_agent.run_agent_loop(*args, **kwargs))


PUBLISHED_EVENT_SCHEMAS = {
    "issue.triaged": {
        "type": "object",
        "required": ["status"],
        "properties": {"status": {"type": "string"}},
        "additionalProperties": False,
    }
}


def publish_event_tool_call(
    call_id: str,
    *,
    event_type: str = "issue.triaged",
    payload: dict[str, Any] | None = None,
    at: str | None = None,
) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "event_type": event_type,
        "payload": payload if payload is not None else {"status": "ready"},
    }
    if at is not None:
        arguments["at"] = at
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "publish_event",
            "arguments": json.dumps(arguments),
        },
    }


def wait_for_tool_call(
    call_id: str,
    *,
    event_type: str = "issue.updated",
    fields: dict[str, Any] | None = None,
    deadline: str | None = None,
) -> dict[str, Any]:
    arguments: dict[str, Any] = {"event_type": event_type}
    if fields is not None:
        arguments["fields"] = fields
    if deadline is not None:
        arguments["deadline"] = deadline
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "wait_for",
            "arguments": json.dumps(arguments),
        },
    }


def cancel_tool_call(
    call_id: str,
    *,
    handle: str = "wait_0123456789abcdef01234567",
    reason: str | None = None,
) -> dict[str, Any]:
    arguments: dict[str, Any] = {"handle": handle}
    if reason is not None:
        arguments["reason"] = reason
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "cancel",
            "arguments": json.dumps(arguments),
        },
    }


def content_tool_call(
    call_id: str,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


class PublishEventGateway:
    def __init__(self, messages: Iterable[dict[str, Any]]) -> None:
        self.messages = iter(messages)
        self.tool_names: list[list[str]] = []
        self.model_inputs: list[zeta_model_shapes.ModelInput] = []

    def available(self, request: zeta_model_shapes.ModelRequest) -> bool:
        del request
        return True

    async def generate(
        self,
        model_input: zeta_model_shapes.ModelInput,
        request: zeta_model_shapes.ModelRequest,
        *,
        stream: zeta_loop_gateway.ModelStream | None = None,
        telemetry_sink: Callable[[dict[str, Any]], None] | None = None,
        should_stop: Callable[[], str | None] | None = None,
    ) -> zeta_model_shapes.ModelOutput:
        del request, stream, telemetry_sink, should_stop
        self.model_inputs.append(model_input)
        self.tool_names.append(
            [descriptor["function"]["name"] for descriptor in (model_input.tools or [])]
        )
        return zeta_model_shapes.ModelOutput(message=next(self.messages))


def run_publish_event_calls(
    *tool_calls: dict[str, Any],
    tool_executor: ToolExecutor | None = None,
) -> AgentRunResult:
    gateway = PublishEventGateway(
        [
            *({"content": "", "tool_calls": [tool_call]} for tool_call in tool_calls),
            {"content": "done"},
        ]
    )
    kwargs: dict[str, Any] = {}
    if tool_executor is not None:
        kwargs["tool_executor"] = tool_executor
    return run_agent_turn(
        "publish",
        [],
        zeta_agent.AgentConfig(max_turns=len(tool_calls) + 1),
        model_gateway=gateway,
        publishable_events=PUBLISHED_EVENT_SCHEMAS,
        source_queue_item_id="qi-work",
        **kwargs,
    )


def run_wait_for_call(
    tool_call: dict[str, Any],
) -> tuple[AgentRunResult, PublishEventGateway]:
    gateway = PublishEventGateway(
        [
            {"content": "", "tool_calls": [tool_call]},
            {"content": "unexpected second model turn"},
        ]
    )
    result = run_agent_turn(
        "wait",
        [],
        zeta_agent.AgentConfig(max_turns=2),
        model_gateway=gateway,
        source_queue_item_id="qi-work",
    )
    return result, gateway


def run_cancel_calls(
    *tool_calls: dict[str, Any],
) -> tuple[AgentRunResult, PublishEventGateway]:
    gateway = PublishEventGateway(
        [
            *({"content": "", "tool_calls": [tool_call]} for tool_call in tool_calls),
            {"content": "done"},
        ]
    )
    result = run_agent_turn(
        "cancel",
        [],
        zeta_agent.AgentConfig(max_turns=len(tool_calls) + 1),
        model_gateway=gateway,
        source_queue_item_id="qi-work",
        source_agent_id="issue-agent",
        source_session_id="agent/issue-agent/session-1",
    )
    return result, gateway


def never_abort(*, check_deadline: bool = True) -> str | None:
    del check_deadline
    return None


def test_zeta_run_dependencies_keep_abort_signal_as_boundary() -> None:
    dependency_fields = {field.name for field in fields(zeta_agent.RunDependencies)}

    assert "abort_reason" in dependency_fields
    assert "clock" not in dependency_fields
    assert "deadline" not in dependency_fields
    assert "cancellation_event" not in dependency_fields


def test_zeta_agent_loop_requires_an_executor() -> None:
    """The loop refuses to run without an executor, at call time."""
    incomplete_call = cast(Any, zeta_agent.run_agent_loop)
    with pytest.raises(TypeError, match="tool_executor"):
        incomplete_call("answer", [], zeta_agent.AgentConfig())


def dispatch_event(
    dispatcher: harness_dispatch.QueueingDispatcher,
    draft: DraftEvent,
) -> Any:
    outcome = asyncio.run(dispatcher.publish_event(draft))
    lifecycle_events = asyncio.run(dispatcher.drain())
    return SimpleNamespace(
        event=outcome.event,
        inserted=outcome.inserted,
        lifecycle_events=lifecycle_events,
    )


async def dispatch_and_drain(
    dispatcher: harness_dispatch.QueueingDispatcher,
    draft: DraftEvent,
) -> SimpleNamespace:
    outcome = await dispatcher.publish_event(draft)
    return SimpleNamespace(
        event=outcome.event,
        inserted=outcome.inserted,
        lifecycle_events=await dispatcher.drain(),
    )


def record_exact_agent_call(
    calls: list[str],
) -> Callable[[harness_dispatch.AgentInvocation], Coroutine[Any, Any, dict[str, Any]]]:
    async def run(invocation: harness_dispatch.AgentInvocation) -> dict[str, Any]:
        calls.append(invocation.triggering_event.id)
        return {"outcome": None}

    return run


def _test_capability(
    name: str,
    *,
    provider: str = "test",
    schema: dict[str, Any] | None = None,
    run_result: dict[str, Any] | None = None,
    delivery_semantics: DeliverySemantics | None = None,
) -> RegisteredCapability:
    return RegisteredCapability(
        Capability(
            CapabilityId(provider, name),
            f"{name} test capability.",
            schema or {"type": "object"},
            delivery_semantics=delivery_semantics,
        ),
        InProcessCapabilityExecutor(
            lambda params: (
                run_result or {"ok": True, "content": [{"type": "text", "text": "ok"}]}
            ),
        ),
    )


def test_zeta_console_script_is_declared() -> None:
    pyproject = tomllib.loads(Path("zeta/pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["scripts"]["zeta"] == "zeta.cli.main:main"
    assert "setuptools-rust" in pyproject["build-system"]["requires"]
    assert pyproject["tool"]["setuptools-rust"]["bins"] == [
        {
            "target": "zeta-tui",
            "path": "../crates/zeta-tui/Cargo.toml",
        }
    ]


def test_zeta_agent_turn_carries_reasoning_into_event(monkeypatch) -> None:
    async def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        request: object = None,
        **kwargs: object,
    ) -> dict[str, Any]:
        return {"content": "done", "reasoning_content": "weighing the options"}

    monkeypatch.setattr(zeta_model_endpoint, "model_endpoint_open", lambda: True)
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

    monkeypatch.setattr(zeta_model_endpoint, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api,
        "chat_completion_messages",
        _as_async(lambda *args, **kwargs: {"content": "done"}),
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


def test_zeta_tool_result_event_payload_records_error_for_failed_content_result() -> (
    None
):
    event = zeta_capability_execution.tool_result_event_payload(
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


def test_zeta_tool_result_event_payload_uses_generic_failed_content_message() -> None:
    event = zeta_capability_execution.tool_result_event_payload(
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


def test_zeta_tool_result_event_payload_preserves_explicit_error() -> None:
    event = zeta_capability_execution.tool_result_event_payload(
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
        zeta_capability_execution.model_tool_call_event_payload(
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


def test_zeta_model_event_payload_has_boundary_dict_shape() -> None:
    assert loop_model.model_event_payload(
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
    draft = zeta_event_drafts.model_call_draft(
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
    payload = zeta_event_drafts.durable_model_event_payload(
        {
            "type": "model",
            "id": "model-1",
            "content": "done",
            "prompt_trace": {
                "prompt_object_id": OBJECT_ID_A,
                "assistant_message_object_id": OBJECT_ID_B,
            },
            "tool_call_object_ids": [OBJECT_ID_C],
            "tool_call_object_id": OBJECT_ID_D,
        }
    )

    assert payload == {
        "_timeline_type": "model",
        "content": "done",
        "prompt_trace": {
            "prompt_object_id": OBJECT_ID_A,
            "assistant_message_object_id": OBJECT_ID_B,
        },
        "tool_call_object_ids": [OBJECT_ID_C],
        "tool_call_object_id": OBJECT_ID_D,
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
    draft = zeta_event_drafts.tool_call_draft(
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
    payload = zeta_event_drafts.durable_tool_event_payload(
        {
            "type": "tool_result",
            "id": "result-1",
            "result": {"ok": True},
            "tool_call_object_id": OBJECT_ID_A,
            "tool_result_object_id": OBJECT_ID_B,
        }
    )

    assert payload == {
        "_timeline_type": "tool_result",
        "result": {"ok": True},
        "tool_call_object_id": OBJECT_ID_A,
        "tool_result_object_id": OBJECT_ID_B,
    }


def test_zeta_durable_tool_call_event_payload_keeps_domain_fields() -> None:
    payload = zeta_event_drafts.durable_tool_event_payload(
        {
            "type": "tool_call",
            "id": "call-1",
            "name": "read",
            "tool_call_object_id": OBJECT_ID_A,
        }
    )

    assert payload == {
        "_timeline_type": "tool_call",
        "name": "read",
        "tool_call_object_id": OBJECT_ID_A,
    }


def test_zeta_tool_result_event_payload_has_boundary_dict_shape() -> None:
    event = zeta_capability_execution.tool_result_event_payload(
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
    registry = CapabilityRegistry()
    ctx = zeta_agent.RunDependencies(
        event_sink=sink_events.append,
        trace_store=None,
        tool_registry=registry,
        tool_executor=zeta_capability_executors.local_tool_executor(registry),
        builder=cast(Any, None),
        abort_reason=never_abort,
    )

    event_id, tool_calls = loop_model.record_model_event(
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
    registry = CapabilityRegistry()

    ctx = zeta_agent.RunDependencies(
        event_sink=drafts.append,
        trace_store=None,
        tool_registry=registry,
        tool_executor=zeta_capability_executors.local_tool_executor(registry),
        builder=cast(Any, None),
        abort_reason=never_abort,
    )

    event_id, tool_calls = loop_model.record_model_event(
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
        tool_executor=zeta_capability_executors.local_tool_executor(registry),
    )

    result = asyncio.run(
        loop_capability.handle_tool_call(
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "read", "arguments": '{"path": "README.md"}'},
            },
            allowed_capabilities=allowed_capabilities,
            tool_schema=registry.model_tool_schema(allowed_capabilities),
            index=0,
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


def test_zeta_capability_records_and_propagates_effect_identity() -> None:
    drafts: list[DraftEvent] = []
    received_effect_keys: list[str | None] = []

    async def execute(
        _params: dict[str, Any],
        *,
        effect_key: str | None = None,
    ) -> dict[str, Any]:
        received_effect_keys.append(effect_key)
        return {"ok": True}

    registry = CapabilityRegistry()
    registry.register(
        RegisteredCapability(
            Capability(
                CapabilityId("test", "write"),
                "Writes data.",
                {"type": "object"},
                delivery_semantics="idempotent_with_key",
            ),
            execute,
        )
    )
    ctx = zeta_capability_execution.CapabilityExecutionContext(
        event_sink=drafts.append,
        trace_store=None,
        tool_registry=registry,
        tool_executor=zeta_capability_executors.local_tool_executor(registry),
        effect_scope="qi_work_1",
    )
    tool_call = {
        "id": "call-1",
        "type": "function",
        "function": {"name": "write", "arguments": '{"path":"a.txt"}'},
    }

    asyncio.run(
        loop_capability.handle_tool_call(
            tool_call,
            allowed_capabilities=("test.write",),
            tool_schema=registry.model_tool_schema(("test.write",)),
            index=0,
            ctx=ctx,
        )
    )
    tool_call["id"] = "call-from-retry"
    asyncio.run(
        loop_capability.handle_tool_call(
            tool_call,
            allowed_capabilities=("test.write",),
            tool_schema=registry.model_tool_schema(("test.write",)),
            index=0,
            ctx=ctx,
        )
    )

    effect_drafts = [
        draft for draft in drafts if draft.event_type.startswith("runtime.effect.")
    ]
    assert received_effect_keys[0] is not None
    assert received_effect_keys == [received_effect_keys[0], received_effect_keys[0]]
    assert [draft.event_type for draft in effect_drafts] == [
        "runtime.effect.planned",
        "runtime.effect.started",
        "runtime.effect.completed",
        "runtime.effect.planned",
        "runtime.effect.started",
        "runtime.effect.completed",
    ]
    assert {draft.payload["queue_item_id"] for draft in effect_drafts} == {"qi_work_1"}
    assert {draft.payload["effect_key"] for draft in effect_drafts} == {
        received_effect_keys[0]
    }


def test_zeta_unsafe_capability_failure_is_recorded_as_ambiguous() -> None:
    drafts: list[DraftEvent] = []
    registry = CapabilityRegistry()
    registry.register(
        _test_capability(
            "bash",
            run_result={"ok": False, "error": {"code": "failed", "message": "boom"}},
            delivery_semantics="unsafe_to_retry",
        )
    )
    ctx = zeta_capability_execution.CapabilityExecutionContext(
        event_sink=drafts.append,
        trace_store=None,
        tool_registry=registry,
        tool_executor=zeta_capability_executors.local_tool_executor(registry),
        effect_scope="qi_work_1",
    )

    asyncio.run(
        loop_capability.handle_tool_call(
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "bash", "arguments": '{"command":"post"}'},
            },
            allowed_capabilities=("test.bash",),
            tool_schema=registry.model_tool_schema(("test.bash",)),
            index=0,
            ctx=ctx,
        )
    )

    assert [
        draft.event_type
        for draft in drafts
        if draft.event_type.startswith("runtime.effect.")
    ] == [
        "runtime.effect.planned",
        "runtime.effect.started",
        "runtime.effect.ambiguous",
    ]


def test_zeta_assistant_message_round_trips_content_to_model_event() -> None:
    assistant = loop_model.AssistantMessage.from_provider({"content": "done"})

    assert assistant.content == "done"
    assert assistant.reasoning_content == ""
    assert assistant.tool_calls == ()
    assert assistant.to_provider() == {"content": "done"}
    assert loop_model.model_event_payload(assistant.to_provider()) == {
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

    assistant = loop_model.AssistantMessage.from_provider(provider_payload)

    assert assistant.tool_calls == (
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "read", "arguments": "{}"},
        },
    )
    assert loop_model.assistant_tool_calls(assistant.to_provider()) == [
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "read", "arguments": "{}"},
        }
    ]


def test_zeta_assistant_message_preserves_reasoning_content() -> None:
    assistant = loop_model.AssistantMessage.from_provider(
        {"content": "done", "reasoning_content": "thinking"}
    )

    assert assistant.reasoning_content == "thinking"
    assert loop_model.model_event_payload(assistant.to_provider()) == {
        "type": "model",
        "reasoning": "thinking",
        "content": "done",
    }


def test_zeta_model_turn_carries_typed_assistant_message() -> None:
    assistant = loop_model.AssistantMessage.from_provider({"content": "done"})
    turn = loop_model.ModelTurn(
        assistant=assistant,
        streamed_content=True,
        model_telemetry={"input_tokens": 1},
        prompt_trace=None,
    )

    assert turn.assistant is assistant
    assert turn.assistant.to_provider() == {"content": "done"}
    assert turn.assistant.content == "done"


def test_zeta_request_assistant_message_returns_model_output(monkeypatch) -> None:
    async def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        request: object = None,
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
        loop_model.request_assistant_message(
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
            content_components: Iterable[zeta_context.PromptComponent] = (),
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
                content_components=content_components,
                tools=tools,
                tool_choice=tool_choice,
                max_tokens=max_tokens,
                selected_model=selected_model,
                thinking=thinking,
            )

        async def commit_prompt_plan(
            self,
            plan: zeta_context.PromptPlan,
        ) -> zeta_context.StoredPrompt:
            self.committed = True
            return await super().commit_prompt_plan(plan)

    async def fake_request_assistant_message(
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
        loop_model,
        "request_assistant_message",
        fake_request_assistant_message,
    )
    state = zeta_agent.RunState()
    builder = PlanOnlyPromptBuilder()
    registry = CapabilityRegistry()
    ctx = zeta_agent.RunDependencies(
        event_sink=None,
        trace_store=None,
        tool_registry=registry,
        tool_executor=zeta_capability_executors.local_tool_executor(registry),
        builder=builder,
        abort_reason=never_abort,
    )

    turn = asyncio.run(
        loop_model.request_model_turn(
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


async def test_zeta_build_prompt_step_returns_committed_model_input() -> None:
    store = zeta_trace.InMemoryStore()
    state = zeta_agent.RunState()

    prepared_prompt, model_input = await loop_prompt.build_prompt_step(
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
        def available(self, request: zeta_model_shapes.ModelRequest) -> bool:
            return True

        async def generate(
            self,
            model_input: zeta_model_shapes.ModelInput,
            request: zeta_model_shapes.ModelRequest,
            *,
            stream: zeta_loop_gateway.ModelStream | None = None,
            telemetry_sink: Callable[[dict[str, Any]], None] | None = None,
            should_stop: Callable[[], str | None] | None = None,
        ) -> zeta_model_shapes.ModelOutput:
            del request, stream
            assert model_input.messages == [{"role": "user", "content": "answer"}]
            assert model_input.tools == []
            if telemetry_sink is not None:
                telemetry_sink({"usage": {"prompt_tokens": 1}})
            return zeta_model_shapes.ModelOutput(message={"content": "done"})

    state = zeta_agent.RunState()

    model_output, streamed_content, model_telemetry = asyncio.run(
        loop_model.call_model_step(
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
        def available(self, request: zeta_model_shapes.ModelRequest) -> bool:
            return True

        async def generate(
            self,
            model_input: zeta_model_shapes.ModelInput,
            request: zeta_model_shapes.ModelRequest,
            *,
            stream: zeta_loop_gateway.ModelStream | None = None,
            telemetry_sink: Callable[[dict[str, Any]], None] | None = None,
            should_stop: Callable[[], str | None] | None = None,
        ) -> zeta_model_shapes.ModelOutput:
            del model_input, request, telemetry_sink
            assert status_events == ["enter"]
            assert stream is not None
            stream.reasoning_delta("checking")
            return zeta_model_shapes.ModelOutput(message={"content": "done"})

    state = zeta_agent.RunState()

    asyncio.run(
        loop_model.call_model_step(
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
        def available(self, request: zeta_model_shapes.ModelRequest) -> bool:
            return True

        async def generate(
            self,
            model_input: zeta_model_shapes.ModelInput,
            request: zeta_model_shapes.ModelRequest,
            *,
            stream: zeta_loop_gateway.ModelStream | None = None,
            telemetry_sink: Callable[[dict[str, Any]], None] | None = None,
            should_stop: Callable[[], str | None] | None = None,
        ) -> zeta_model_shapes.ModelOutput:
            del request, stream, telemetry_sink
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
        def available(self, request: zeta_model_shapes.ModelRequest) -> bool:
            return True

        async def generate(
            self,
            model_input: zeta_model_shapes.ModelInput,
            request: zeta_model_shapes.ModelRequest,
            *,
            stream: zeta_loop_gateway.ModelStream | None = None,
            telemetry_sink: Callable[[dict[str, Any]], None] | None = None,
            should_stop: Callable[[], str | None] | None = None,
        ) -> zeta_model_shapes.ModelOutput:
            del request, stream, telemetry_sink
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
                zeta_agent.run_agent_loop(
                    "first",
                    [],
                    zeta_agent.AgentConfig(max_turns=1),
                    model_gateway=gateway,
                    tool_executor=zeta_capability_executors.local_tool_executor(),
                ),
                zeta_agent.run_agent_loop(
                    "second",
                    [],
                    zeta_agent.AgentConfig(max_turns=1),
                    model_gateway=gateway,
                    tool_executor=zeta_capability_executors.local_tool_executor(),
                ),
            ),
            timeout=3,
        )

        assert {first.final_answer, second.final_answer} == {"first", "second"}

    asyncio.run(run())
    assert set(seen) == {"first", "second"}


def test_zeta_publish_event_is_visible_only_with_publishes() -> None:
    without_publishes = PublishEventGateway([{"content": "done"}])
    with_publishes = PublishEventGateway([{"content": "done"}])

    run_agent_turn(
        "answer",
        [],
        zeta_agent.AgentConfig(max_turns=1),
        model_gateway=without_publishes,
    )
    run_agent_turn(
        "answer",
        [],
        zeta_agent.AgentConfig(max_turns=1),
        model_gateway=with_publishes,
        publishable_events=PUBLISHED_EVENT_SCHEMAS,
        source_queue_item_id="qi-work",
    )

    assert "publish_event" not in without_publishes.tool_names[0]
    assert "publish_event" in with_publishes.tool_names[0]


def test_zeta_content_tools_update_and_query_the_run_workspace() -> None:
    registry = CapabilityRegistry()
    register_builtin_tools(registry)
    workspace = zeta_content_transforms.ContentWorkspace(
        InMemoryStore(),
        run_id="run-content",
        session_id="session-content",
        owner="writer",
    )
    initial_head = workspace.initialize()
    gateway = PublishEventGateway(
        [
            {
                "content": "",
                "tool_calls": [
                    content_tool_call(
                        "call-transform",
                        "transform_content",
                        {
                            "expected_head": initial_head,
                            "reason": "Keep the release procedure.",
                            "inputs": {},
                            "transformation": {
                                "type": "literal",
                                "value": "Check the manifest.",
                            },
                            "destination": {
                                "key": "release/check",
                                "kind": "procedure",
                                "scope": "run",
                                "expected_object_id": None,
                            },
                        },
                    )
                ],
            },
            {
                "content": "",
                "tool_calls": [
                    content_tool_call(
                        "call-query",
                        "query_content",
                        {"key_prefix": "release/"},
                    )
                ],
            },
            {"content": "done"},
        ]
    )

    result = run_agent_turn(
        "manage content",
        [],
        zeta_agent.AgentConfig(
            max_turns=3,
            allowed_capabilities=("transform_content", "query_content"),
        ),
        model_gateway=gateway,
        tool_registry=registry,
        content_workspace=workspace,
    )

    assert gateway.tool_names[0] == ["transform_content", "query_content"]
    tool_results = [
        event
        for event in timeline_events(result.events)
        if event.get("type") == "tool_result"
    ]
    assert tool_results[0]["result"]["ok"] is True
    assert tool_results[0]["result"]["status"] == "applied"
    assert tool_results[1]["result"]["items"][0]["key"] == "release/check"
    assert tool_results[1]["result"]["items"][0]["preview"] == ("Check the manifest.")
    assert "Check the manifest." in json.dumps(gateway.model_inputs[1].messages)


def test_zeta_transform_content_records_durable_promotion_requests() -> None:
    registry = CapabilityRegistry()
    register_builtin_tools(registry)
    workspace = zeta_content_transforms.ContentWorkspace(
        InMemoryStore(),
        run_id="run-content",
        session_id="session-content",
        owner="writer",
    )
    initial_head = workspace.initialize()
    gateway = PublishEventGateway(
        [
            {
                "content": "",
                "tool_calls": [
                    content_tool_call(
                        "call-transform",
                        "transform_content",
                        {
                            "expected_head": initial_head,
                            "reason": "Use this procedure in later runs.",
                            "inputs": {},
                            "transformation": {
                                "type": "literal",
                                "value": "Run focused tests first.",
                            },
                            "destination": {
                                "key": "testing",
                                "kind": "procedure",
                                "scope": "agent",
                                "expected_object_id": None,
                            },
                        },
                    )
                ],
            },
            {"content": "done"},
        ]
    )

    result = run_agent_turn(
        "manage content",
        [],
        zeta_agent.AgentConfig(
            max_turns=2,
            allowed_capabilities=("transform_content",),
        ),
        model_gateway=gateway,
        tool_registry=registry,
        content_workspace=workspace,
    )

    assert len(result.content_promotions) == 1
    assert result.content_promotions[0].scope == "agent"
    assert zeta_outcomes.agent_run_result_payload(result)["content_promotions"] == [
        asdict(result.content_promotions[0])
    ]
    assert workspace.store.get_ref("agent/writer/content/head") is None


def test_zeta_model_content_transform_records_child_prompt_and_answer() -> None:
    registry = CapabilityRegistry()
    register_builtin_tools(registry)
    store = InMemoryStore()
    workspace = zeta_content_transforms.ContentWorkspace(
        store,
        run_id="run-content",
        session_id="session-content",
        owner="writer",
    )
    source = workspace.transform(
        {
            "expected_head": workspace.initialize(),
            "reason": "Add source material.",
            "inputs": {},
            "transformation": {"type": "literal", "value": "Release evidence."},
            "destination": {
                "key": "evidence",
                "kind": "document",
                "scope": "run",
                "expected_object_id": None,
            },
        }
    )
    gateway = PublishEventGateway(
        [
            {
                "content": "",
                "tool_calls": [
                    content_tool_call(
                        "call-transform",
                        "transform_content",
                        {
                            "expected_head": source.head,
                            "reason": "Summarize the evidence.",
                            "inputs": {"keys": ["evidence"]},
                            "transformation": {
                                "type": "model",
                                "mode": "one",
                                "instruction": "Write one short release summary.",
                            },
                            "destination": {
                                "key": "summary",
                                "kind": "procedure",
                                "scope": "run",
                                "expected_object_id": None,
                            },
                        },
                    )
                ],
            },
            {"content": "Check the release evidence."},
            {"content": "done"},
        ]
    )

    result = run_agent_turn(
        "manage content",
        [],
        zeta_agent.AgentConfig(
            max_turns=2,
            allowed_capabilities=("transform_content",),
        ),
        model_gateway=gateway,
        tool_registry=registry,
        trace_store=store,
        content_workspace=workspace,
    )

    tool_result = next(
        event
        for event in timeline_events(result.events)
        if event.get("type") == "tool_result"
    )["result"]
    assert tool_result["ok"] is True
    output_id = tool_result["object_ids"][0]
    output = store.get_object(output_id)
    assert output is not None
    assert output.data["content"] == "Check the release evidence."
    assert "Release evidence." in json.dumps(gateway.model_inputs[1].messages)
    assert gateway.model_inputs[1].tools == []
    assert "Check the release evidence." in json.dumps(gateway.model_inputs[2].messages)
    derivations = store.derivations_for_output(output_id)
    assert any(item.producer == "ModelTransform:v1" for item in derivations)
    assistant_ids = []
    for object_id in output.links:
        linked = store.get_object(object_id)
        assert linked is not None
        if linked.kind == "assistant_message":
            assistant_ids.append(object_id)
    assert len(assistant_ids) == 1
    assistant = store.get_object(assistant_ids[0])
    assert assistant is not None
    assert assistant.links
    prompt = store.get_object(assistant.links[0])
    assert prompt is not None
    assert prompt.kind == "prompt"


def test_zeta_model_content_transform_reuses_a_retry_child_result() -> None:
    registry = CapabilityRegistry()
    register_builtin_tools(registry)
    store = InMemoryStore()
    seed = zeta_content_transforms.ContentWorkspace(
        store,
        run_id="run-seed",
        session_id="session-content",
        owner="writer",
    )
    source = seed.transform(
        {
            "expected_head": seed.initialize(),
            "reason": "Keep source material.",
            "inputs": {},
            "transformation": {"type": "literal", "value": "Release evidence."},
            "destination": {
                "key": "evidence",
                "kind": "document",
                "scope": "agent",
                "expected_object_id": None,
            },
        }
    )
    seed.promote(source.promotions[0])

    def retry_workspace(run_id: str) -> zeta_content_transforms.ContentWorkspace:
        workspace = zeta_content_transforms.ContentWorkspace(
            store,
            run_id=run_id,
            session_id="session-content",
            owner="writer",
        )
        workspace.initialize()
        return workspace

    def transform_params(
        workspace: zeta_content_transforms.ContentWorkspace,
    ) -> dict[str, Any]:
        return {
            "expected_head": workspace.current_head(),
            "reason": "Summarize the evidence.",
            "inputs": {"keys": ["evidence"]},
            "transformation": {
                "type": "model",
                "mode": "one",
                "instruction": "Write one short release summary.",
            },
            "destination": {
                "key": "summary",
                "kind": "procedure",
                "scope": "run",
                "expected_object_id": None,
            },
        }

    first_workspace = retry_workspace("run-attempt-1")
    first_gateway = PublishEventGateway(
        [
            {
                "content": "",
                "tool_calls": [
                    content_tool_call(
                        "call-transform",
                        "transform_content",
                        transform_params(first_workspace),
                    )
                ],
            },
            {"content": "Cached summary."},
            {"content": "done"},
        ]
    )
    first_result = run_agent_turn(
        "manage content",
        [],
        zeta_agent.AgentConfig(
            max_turns=2,
            allowed_capabilities=("transform_content",),
        ),
        model_gateway=first_gateway,
        tool_registry=registry,
        trace_store=store,
        content_workspace=first_workspace,
        source_queue_item_id="qi_retry",
    )
    first_tool_result = next(
        event
        for event in timeline_events(first_result.events)
        if event.get("type") == "tool_result"
    )["result"]

    second_workspace = retry_workspace("run-attempt-2")
    second_gateway = PublishEventGateway(
        [
            {
                "content": "",
                "tool_calls": [
                    content_tool_call(
                        "call-transform",
                        "transform_content",
                        transform_params(second_workspace),
                    )
                ],
            },
            {"content": "done"},
        ]
    )
    second_result = run_agent_turn(
        "manage content",
        [],
        zeta_agent.AgentConfig(
            max_turns=2,
            allowed_capabilities=("transform_content",),
        ),
        model_gateway=second_gateway,
        tool_registry=registry,
        trace_store=store,
        content_workspace=second_workspace,
        source_queue_item_id="qi_retry",
    )
    second_tool_result = next(
        event
        for event in timeline_events(second_result.events)
        if event.get("type") == "tool_result"
    )["result"]
    first_output = store.get_object(first_tool_result["object_ids"][0])
    second_output = store.get_object(second_tool_result["object_ids"][0])

    assert first_output is not None
    assert second_output is not None
    assert first_output.data["content"] == "Cached summary."
    assert second_output.data["content"] == "Cached summary."
    assert first_output.links[-1] == second_output.links[-1]
    assert len(first_gateway.model_inputs) == 3
    assert len(second_gateway.model_inputs) == 2


def test_zeta_content_transform_budget_reconciles_reserved_model_tokens() -> None:
    budget = ContentTransformBudget(
        max_model_calls=2,
        max_total_tokens=5_000,
    )

    assert budget.reserve_model_calls(calls=1, input_chars=4) == 1
    budget.record_model_output(
        10,
        input_chars=4,
        total_tokens=50,
    )

    assert budget.reserved_tokens == 50
    assert budget.reserve_model_calls(calls=1, input_chars=4) == 1
    with pytest.raises(
        zeta_content_transforms.ContentValidationError,
        match="model call budget",
    ):
        budget.reserve_model_calls(calls=1, input_chars=4)


def test_zeta_model_map_transform_keeps_source_order_in_a_collection() -> None:
    registry = CapabilityRegistry()
    register_builtin_tools(registry)
    store = InMemoryStore()
    workspace = zeta_content_transforms.ContentWorkspace(
        store,
        run_id="run-content",
        session_id="session-content",
        owner="writer",
    )
    head = workspace.initialize()
    for key, value in (("a", "First source."), ("b", "Second source.")):
        changed = workspace.transform(
            {
                "expected_head": head,
                "reason": f"Add source {key}.",
                "inputs": {},
                "transformation": {"type": "literal", "value": value},
                "destination": {
                    "key": key,
                    "kind": "document",
                    "scope": "run",
                    "expected_object_id": None,
                },
            }
        )
        head = changed.head
    gateway = PublishEventGateway(
        [
            {
                "content": "",
                "tool_calls": [
                    content_tool_call(
                        "call-transform",
                        "transform_content",
                        {
                            "expected_head": head,
                            "reason": "Extract each finding.",
                            "inputs": {"keys": ["a", "b"]},
                            "transformation": {
                                "type": "model",
                                "mode": "map",
                                "instruction": "Extract one finding.",
                                "max_concurrency": 2,
                            },
                            "destination": {
                                "key": "findings",
                                "kind": "collection",
                                "scope": "run",
                                "expected_object_id": None,
                            },
                        },
                    )
                ],
            },
            {"content": "Finding A."},
            {"content": "Finding B."},
            {"content": "done"},
        ]
    )

    result = run_agent_turn(
        "map content",
        [],
        zeta_agent.AgentConfig(
            max_turns=2,
            allowed_capabilities=("transform_content",),
        ),
        model_gateway=gateway,
        tool_registry=registry,
        trace_store=store,
        content_workspace=workspace,
    )

    tool_result = next(
        event
        for event in timeline_events(result.events)
        if event.get("type") == "tool_result"
    )["result"]
    collection = store.get_object(tool_result["object_ids"][0])
    assert collection is not None
    assert collection.data["content"] == {"object_ids": list(collection.links[-2:])}
    messages = []
    for item in collection.links[-2:]:
        assistant = store.get_object(item)
        assert assistant is not None
        messages.append(assistant.data["message"]["content"])
    assert messages == [
        "Finding A.",
        "Finding B.",
    ]


def test_zeta_python_transform_composes_a_traced_map_and_reduce() -> None:
    registry = CapabilityRegistry()
    register_builtin_tools(registry)
    store = InMemoryStore()
    workspace = zeta_content_transforms.ContentWorkspace(
        store,
        run_id="run-content",
        session_id="session-content",
        owner="writer",
    )
    head = workspace.initialize()
    for key, value in (("a", "First source."), ("b", "Second source.")):
        changed = workspace.transform(
            {
                "expected_head": head,
                "reason": f"Add source {key}.",
                "inputs": {},
                "transformation": {"type": "literal", "value": value},
                "destination": {
                    "key": key,
                    "kind": "document",
                    "scope": "run",
                    "expected_object_id": None,
                },
            }
        )
        head = changed.head
    source = """
async def main(ctx, transform):
    documents = ctx.select(kind="document")
    findings = await transform(
        inputs=documents,
        transformation={
            "type": "model",
            "mode": "map",
            "instruction": "Extract one finding.",
            "max_concurrency": 2,
        },
        destination={"key": "findings", "kind": "collection", "scope": "run"},
    )
    return await transform(
        inputs=findings,
        transformation={
            "type": "model",
            "mode": "reduce",
            "instruction": "Combine the findings.",
        },
        destination={"key": "answer", "kind": "text", "scope": "run"},
    )
"""
    gateway = PublishEventGateway(
        [
            {
                "content": "",
                "tool_calls": [
                    content_tool_call(
                        "call-transform",
                        "transform_content",
                        {
                            "expected_head": head,
                            "reason": "Run a bounded evidence program.",
                            "inputs": {"keys": ["a", "b"]},
                            "transformation": {
                                "type": "python",
                                "source": source,
                                "timeout_seconds": 10,
                            },
                            "destination": {
                                "key": "answer",
                                "kind": "procedure",
                                "scope": "run",
                                "expected_object_id": None,
                            },
                        },
                    )
                ],
            },
            {"content": "Finding A."},
            {"content": "Finding B."},
            {"content": "Combined answer."},
            {"content": "done"},
        ]
    )

    result = run_agent_turn(
        "run the evidence program",
        [],
        zeta_agent.AgentConfig(
            max_turns=2,
            allowed_capabilities=("transform_content",),
        ),
        model_gateway=gateway,
        tool_registry=registry,
        trace_store=store,
        content_workspace=workspace,
    )

    tool_result = next(
        event
        for event in timeline_events(result.events)
        if event.get("type") == "tool_result"
    )["result"]
    assert tool_result["ok"] is True
    output_id = tool_result["object_ids"][0]
    output = store.get_object(output_id)
    assert output is not None
    assert output.data["content"] == "Combined answer."
    assert any(
        item.producer == "PythonTransform:v1"
        for item in store.derivations_for_output(output_id)
    )
    model_nodes = [
        (object_id, obj)
        for object_id, obj in store.objects("content_node")
        if any(
            item.producer == "ModelTransform:v1"
            for item in store.derivations_for_output(object_id)
        )
    ]
    assert {item.data["key"] for _object_id, item in model_nodes} == {
        "findings",
        "answer",
    }
    findings_id = next(
        object_id for object_id, item in model_nodes if item.data["key"] == "findings"
    )
    reduced = next(
        item for _object_id, item in model_nodes if item.data["key"] == "answer"
    )
    assert findings_id in reduced.links
    assert "Combined answer." in json.dumps(gateway.model_inputs[-1].messages)


def test_zeta_python_transform_observes_run_cancellation() -> None:
    registry = CapabilityRegistry()
    register_builtin_tools(registry)
    store = InMemoryStore()
    workspace = zeta_content_transforms.ContentWorkspace(
        store,
        run_id="run-content",
        session_id="session-content",
        owner="writer",
    )
    head = workspace.initialize()
    gateway = PublishEventGateway(
        [
            {
                "content": "",
                "tool_calls": [
                    content_tool_call(
                        "call-transform",
                        "transform_content",
                        {
                            "expected_head": head,
                            "reason": "Run cancellable Python.",
                            "inputs": {},
                            "transformation": {
                                "type": "python",
                                "source": (
                                    "def main(ctx, transform):\n"
                                    "    while True:\n"
                                    "        pass\n"
                                ),
                                "timeout_seconds": 10,
                            },
                            "destination": {
                                "key": "answer",
                                "kind": "text",
                                "scope": "run",
                                "expected_object_id": None,
                            },
                        },
                    )
                ],
            }
        ]
    )
    cancellation = threading.Event()
    cancel = threading.Timer(0.05, cancellation.set)
    cancel.start()
    try:
        with pytest.raises(zeta_loop_cancellation.AgentRunAborted) as raised:
            run_agent_turn(
                "cancel the Python program",
                [],
                zeta_agent.AgentConfig(
                    max_turns=2,
                    allowed_capabilities=("transform_content",),
                ),
                model_gateway=gateway,
                tool_registry=registry,
                trace_store=store,
                content_workspace=workspace,
                cancellation_event=cancellation,
            )
    finally:
        cancel.cancel()
        cancel.join(timeout=1)

    assert raised.value.reason == "cancelled"
    assert workspace.current_head() == head


def test_zeta_finish_returns_a_graph_object_without_another_model_turn() -> None:
    registry = CapabilityRegistry()
    register_builtin_tools(registry)
    store = InMemoryStore()
    workspace = zeta_content_transforms.ContentWorkspace(
        store,
        run_id="run-content",
        session_id="session-content",
        owner="writer",
    )
    transformed = workspace.transform(
        {
            "expected_head": workspace.initialize(),
            "reason": "Store the complete answer.",
            "inputs": {},
            "transformation": {
                "type": "literal",
                "value": "The complete graph-backed answer.",
            },
            "destination": {
                "key": "answer",
                "kind": "text",
                "scope": "run",
                "expected_object_id": None,
            },
        }
    )
    answer_id = transformed.output_ids[0]
    gateway = PublishEventGateway(
        [
            {
                "content": "",
                "tool_calls": [
                    content_tool_call(
                        "call-finish",
                        "finish",
                        {"object_id": answer_id},
                    )
                ],
            }
        ]
    )

    result = run_agent_turn(
        "return the stored answer",
        [],
        zeta_agent.AgentConfig(
            max_turns=2,
            allowed_capabilities=("finish",),
        ),
        model_gateway=gateway,
        tool_registry=registry,
        trace_store=store,
        content_workspace=workspace,
    )

    assert result.stop_reason == "tool_stop"
    assert result.final_object_id == answer_id
    assert result.final_answer == "The complete graph-backed answer."
    assert len(gateway.model_inputs) == 1
    payload = zeta_outcomes.agent_run_result_payload(result)
    assert payload["final_object_id"] == answer_id


def test_zeta_transform_content_returns_stale_head_errors() -> None:
    registry = CapabilityRegistry()
    register_builtin_tools(registry)
    workspace = zeta_content_transforms.ContentWorkspace(
        InMemoryStore(),
        run_id="run-content",
        session_id="session-content",
        owner="writer",
    )
    workspace.initialize()
    gateway = PublishEventGateway(
        [
            {
                "content": "",
                "tool_calls": [
                    content_tool_call(
                        "call-transform",
                        "transform_content",
                        {
                            "expected_head": OBJECT_ID_A,
                            "reason": "Apply stale content.",
                            "inputs": {},
                            "transformation": {"type": "literal", "value": "bad"},
                            "destination": {
                                "key": "bad",
                                "kind": "text",
                                "scope": "run",
                                "expected_object_id": None,
                            },
                        },
                    )
                ],
            },
            {"content": "done"},
        ]
    )

    result = run_agent_turn(
        "manage content",
        [],
        zeta_agent.AgentConfig(
            max_turns=2,
            allowed_capabilities=("transform_content",),
        ),
        model_gateway=gateway,
        tool_registry=registry,
        content_workspace=workspace,
    )

    tool_result = event_by_type(result.events, "tool_result")
    assert tool_result["result"]["ok"] is False
    assert tool_result["result"]["error"]["code"] == "content-conflict"
    assert result.content_promotions == []


def test_zeta_publish_event_rejects_an_existing_model_tool_name() -> None:
    input_schema = {"type": "object"}
    tool_schema = zeta_agent.CapabilityToolSchema(
        routes={
            "publish_event": zeta_agent.CapabilityToolRoute(
                capability_id="provider.publish",
                input_schema=input_schema,
                adapt_arguments=zeta_agent.identity_arguments,
            )
        },
        descriptors=[
            zeta_agent.model_descriptor(
                "publish_event",
                "Publish through the provider.",
                input_schema,
            )
        ],
    )

    with pytest.raises(ValueError, match="reserved tool name 'publish_event'"):
        zeta_agent.publish_event_tool_schema(tool_schema)


def test_zeta_publish_event_returns_a_stable_handle_and_request() -> None:
    def run_once() -> AgentRunResult:
        return run_publish_event_calls(publish_event_tool_call("call-1"))

    first = run_once()
    second = run_once()

    assert len(first.publish_event_requests) == 1
    request = first.publish_event_requests[0]
    assert request.event_type == "issue.triaged"
    assert request.payload == {"status": "ready"}
    assert request.at is None
    assert request.position == 0
    assert request.handle == second.publish_event_requests[0].handle
    tool_result = event_by_type(first.events, "tool_result")
    assert tool_result["result"]["ok"] is True
    assert request.handle in json.dumps(tool_result["result"])


def test_zeta_publish_event_rejects_an_undeclared_event_type() -> None:
    result = run_publish_event_calls(
        publish_event_tool_call("call-1", event_type="issue.closed")
    )

    assert result.publish_event_requests == []
    tool_result = event_by_type(result.events, "tool_result")
    assert tool_result["result"]["ok"] is False
    assert tool_result["status"] == "failed"


def test_zeta_publish_event_rejects_an_invalid_payload() -> None:
    result = run_publish_event_calls(
        publish_event_tool_call("call-1", payload={"status": 3})
    )

    assert result.publish_event_requests == []
    tool_result = event_by_type(result.events, "tool_result")
    assert tool_result["result"]["ok"] is False
    assert tool_result["status"] == "failed"


@pytest.mark.parametrize("at", ["not-a-date", "2030-01-02T03:04:05"])
def test_zeta_publish_event_rejects_invalid_or_naive_time(at: str) -> None:
    result = run_publish_event_calls(publish_event_tool_call("call-1", at=at))

    assert result.publish_event_requests == []
    tool_result = event_by_type(result.events, "tool_result")
    assert tool_result["result"]["ok"] is False
    assert tool_result["status"] == "failed"


def test_zeta_publish_event_normalizes_an_aware_time_to_utc() -> None:
    result = run_publish_event_calls(
        publish_event_tool_call("call-1", at="2030-01-02T05:04:05+02:00")
    )

    assert result.publish_event_requests[0].at == "2030-01-02T03:04:05+00:00"


def test_zeta_publish_event_bypasses_executor_and_effect_facts() -> None:
    class UnexpectedExecutor:
        async def call(
            self,
            capability_id: str,
            params: dict[str, Any],
            *,
            base_dir: Path | None,
            effect_key: str | None,
        ) -> dict[str, Any]:
            del capability_id, params, base_dir, effect_key
            raise AssertionError("publish_event must not use the tool executor")

        async def aclose(self) -> None:
            return None

    result = run_publish_event_calls(
        publish_event_tool_call("call-1"),
        tool_executor=UnexpectedExecutor(),
    )

    assert len(result.publish_event_requests) == 1
    assert not any(
        event.event_type.startswith("runtime.effect.") for event in result.events
    )


def test_zeta_publish_event_keeps_global_order_across_model_turns() -> None:
    result = run_publish_event_calls(
        publish_event_tool_call("call-1"),
        publish_event_tool_call(
            "call-2",
            payload={"status": "complete"},
        ),
    )

    assert [request.position for request in result.publish_event_requests] == [0, 1]
    assert [request.payload for request in result.publish_event_requests] == [
        {"status": "ready"},
        {"status": "complete"},
    ]
    assert len({request.handle for request in result.publish_event_requests}) == 2


def test_zeta_wait_for_is_visible_only_to_authored_agent_runs() -> None:
    session_gateway = PublishEventGateway([{"content": "done"}])
    authored_gateway = PublishEventGateway([{"content": "done"}])

    run_agent_turn(
        "answer",
        [],
        zeta_agent.AgentConfig(max_turns=1),
        model_gateway=session_gateway,
    )
    run_agent_turn(
        "answer",
        [],
        zeta_agent.AgentConfig(max_turns=1),
        model_gateway=authored_gateway,
        source_queue_item_id="qi-work",
    )

    assert "wait_for" not in session_gateway.tool_names[0]
    assert "wait_for" in authored_gateway.tool_names[0]


def test_zeta_wait_for_rejects_an_existing_model_tool_name() -> None:
    input_schema = {"type": "object"}
    tool_schema = zeta_agent.CapabilityToolSchema(
        routes={
            "wait_for": zeta_agent.CapabilityToolRoute(
                capability_id="provider.wait",
                input_schema=input_schema,
                adapt_arguments=zeta_agent.identity_arguments,
            )
        },
        descriptors=[
            zeta_agent.model_descriptor(
                "wait_for",
                "Wait through the provider.",
                input_schema,
            )
        ],
    )

    with pytest.raises(ValueError, match="reserved tool name 'wait_for'"):
        zeta_agent.wait_for_tool_schema(tool_schema)


def test_zeta_wait_for_returns_a_stable_handle_and_stops_the_run() -> None:
    def run_once() -> tuple[AgentRunResult, PublishEventGateway]:
        return run_wait_for_call(
            wait_for_tool_call(
                "call-1",
                fields={"repository": "zeta"},
            )
        )

    first, first_gateway = run_once()
    second, _ = run_once()

    assert first.stop_reason == "tool_stop"
    assert first.final_answer == ""
    assert len(first_gateway.tool_names) == 1
    assert len(first.wait_requests) == 1
    request = first.wait_requests[0]
    assert request.event_type == "issue.updated"
    assert request.fields == {"repository": "zeta"}
    assert request.deadline is None
    assert request.position == 0
    assert request.handle.startswith("wait_")
    assert request.handle == second.wait_requests[0].handle
    tool_result = event_by_type(first.events, "tool_result")
    assert tool_result["result"] == {
        "ok": True,
        "handle": request.handle,
        "stop": True,
    }


def test_zeta_wait_for_defaults_to_empty_fields_and_serializes_the_request() -> None:
    result, _ = run_wait_for_call(wait_for_tool_call("call-1"))

    request = result.wait_requests[0]
    assert request.fields == {}
    assert zeta_outcomes.agent_run_result_payload(result)["wait_requests"] == [
        asdict(request)
    ]


def test_zeta_wait_for_rejects_an_empty_event_type() -> None:
    result, gateway = run_wait_for_call(wait_for_tool_call("call-1", event_type=""))

    assert result.wait_requests == []
    assert result.stop_reason == "finished"
    assert len(gateway.tool_names) == 2
    tool_result = event_by_type(result.events, "tool_result")
    assert tool_result["result"]["ok"] is False


@pytest.mark.parametrize("deadline", ["not-a-date", "2030-01-02T03:04:05"])
def test_zeta_wait_for_rejects_invalid_or_naive_deadline(deadline: str) -> None:
    result, gateway = run_wait_for_call(wait_for_tool_call("call-1", deadline=deadline))

    assert result.wait_requests == []
    assert result.stop_reason == "finished"
    assert len(gateway.tool_names) == 2
    tool_result = event_by_type(result.events, "tool_result")
    assert tool_result["result"]["ok"] is False
    assert tool_result["result"]["error"]["code"] == "invalid-wait-deadline"


def test_zeta_wait_for_normalizes_an_aware_deadline_to_utc() -> None:
    result, _ = run_wait_for_call(
        wait_for_tool_call("call-1", deadline="2030-01-02T05:04:05+02:00")
    )

    assert result.wait_requests[0].deadline == "2030-01-02T03:04:05+00:00"


def test_zeta_cancel_is_visible_only_to_authored_agent_runs() -> None:
    session_gateway = PublishEventGateway([{"content": "done"}])
    authored_gateway = PublishEventGateway([{"content": "done"}])

    run_agent_turn(
        "answer",
        [],
        zeta_agent.AgentConfig(max_turns=1),
        model_gateway=session_gateway,
    )
    run_agent_turn(
        "answer",
        [],
        zeta_agent.AgentConfig(max_turns=1),
        model_gateway=authored_gateway,
        source_queue_item_id="qi-work",
        source_agent_id="issue-agent",
        source_session_id="agent/issue-agent/session-1",
    )

    assert "cancel" not in session_gateway.tool_names[0]
    assert "cancel" in authored_gateway.tool_names[0]


def test_zeta_cancel_rejects_an_existing_model_tool_name() -> None:
    input_schema = {"type": "object"}
    tool_schema = zeta_agent.CapabilityToolSchema(
        routes={
            "cancel": zeta_agent.CapabilityToolRoute(
                capability_id="provider.cancel",
                input_schema=input_schema,
                adapt_arguments=zeta_agent.identity_arguments,
            )
        },
        descriptors=[
            zeta_agent.model_descriptor(
                "cancel",
                "Cancel through the provider.",
                input_schema,
            )
        ],
    )

    with pytest.raises(ValueError, match="reserved tool name 'cancel'"):
        zeta_agent.cancel_tool_schema(tool_schema)


def test_zeta_cancel_records_source_identity_and_does_not_stop() -> None:
    result, gateway = run_cancel_calls(
        cancel_tool_call("call-1", reason="No longer needed")
    )

    assert result.stop_reason == "finished"
    assert len(gateway.tool_names) == 2
    assert len(result.cancel_requests) == 1
    request = result.cancel_requests[0]
    assert request.handle == "wait_0123456789abcdef01234567"
    assert request.reason == "No longer needed"
    assert request.source_agent_id == "issue-agent"
    assert request.source_session_id == "agent/issue-agent/session-1"
    assert request.position == 0
    assert zeta_outcomes.agent_run_result_payload(result)["cancel_requests"] == [
        asdict(request)
    ]
    tool_result = event_by_type(result.events, "tool_result")
    assert tool_result["result"] == {
        "ok": True,
        "handle": request.handle,
        "status": "requested",
    }


@pytest.mark.parametrize(
    ("handle", "reason"),
    [
        ("unknown_012345", None),
        ("", None),
        ("wait_0123456789abcdef01234567", ""),
    ],
)
def test_zeta_cancel_rejects_invalid_arguments(
    handle: str,
    reason: str | None,
) -> None:
    result, gateway = run_cancel_calls(
        cancel_tool_call("call-1", handle=handle, reason=reason)
    )

    assert result.cancel_requests == []
    assert result.stop_reason == "finished"
    assert len(gateway.tool_names) == 2
    tool_result = event_by_type(result.events, "tool_result")
    assert tool_result["result"]["ok"] is False


def test_zeta_control_requests_keep_global_tool_call_positions() -> None:
    gateway = PublishEventGateway(
        [
            {"content": "", "tool_calls": [publish_event_tool_call("call-1")]},
            {"content": "", "tool_calls": [cancel_tool_call("call-2")]},
            {"content": "done"},
        ]
    )

    result = run_agent_turn(
        "control",
        [],
        zeta_agent.AgentConfig(max_turns=3),
        model_gateway=gateway,
        publishable_events=PUBLISHED_EVENT_SCHEMAS,
        source_queue_item_id="qi-work",
        source_agent_id="issue-agent",
        source_session_id="agent/issue-agent/session-1",
    )

    assert result.publish_event_requests[0].position == 0
    assert result.cancel_requests[0].position == 1


def test_zeta_step_model_without_tool_calls_returns_info_and_stops() -> None:
    class FakeGateway:
        def available(self, request: zeta_model_shapes.ModelRequest) -> bool:
            del request
            return True

        async def generate(
            self,
            model_input: zeta_model_shapes.ModelInput,
            request: zeta_model_shapes.ModelRequest,
            *,
            stream: zeta_loop_gateway.ModelStream | None = None,
            telemetry_sink: Callable[[dict[str, Any]], None] | None = None,
            should_stop: Callable[[], str | None] | None = None,
        ) -> zeta_model_shapes.ModelOutput:
            del model_input, request, stream
            if telemetry_sink is not None:
                telemetry_sink({"usage": {"input_tokens": 1}})
            return zeta_model_shapes.ModelOutput(message={"content": "done"})

    registry = CapabilityRegistry()
    state = zeta_agent.RunState()
    ctx = zeta_agent.RunDependencies(
        event_sink=None,
        trace_store=None,
        tool_registry=registry,
        tool_executor=zeta_capability_executors.local_tool_executor(registry),
        builder=zeta_context.PromptBuilder(),
        abort_reason=never_abort,
        model_gateway=FakeGateway(),
    )

    state, info = asyncio.run(
        zeta_loop_steps.step(
            state,
            objective="answer",
            timeline=[],
            config=zeta_agent.AgentConfig(),
            allowed_capabilities=(),
            context="",
            tool_schema=registry.model_tool_schema(()),
            tools=[],
            ctx=ctx,
        )
    )

    assert info.kind == "model"
    assert info.final_answer == "done"
    assert info.model_telemetry == {"usage": {"input_tokens": 1}}
    assert info.appended_events == tuple(state.events[-1:])
    assert state.stop == "finished"
    assert state.pending_tool_calls == []
    assert timeline_events(list(info.appended_events))[0]["content"] == "done"


def test_zeta_step_model_with_tool_calls_records_pending_tools() -> None:
    tool_calls = tool_call_fixture("call-1", name="read", path="README.md")

    class FakeGateway:
        def available(self, request: zeta_model_shapes.ModelRequest) -> bool:
            del request
            return True

        async def generate(
            self,
            model_input: zeta_model_shapes.ModelInput,
            request: zeta_model_shapes.ModelRequest,
            *,
            stream: zeta_loop_gateway.ModelStream | None = None,
            telemetry_sink: Callable[[dict[str, Any]], None] | None = None,
            should_stop: Callable[[], str | None] | None = None,
        ) -> zeta_model_shapes.ModelOutput:
            del model_input, request, stream
            if telemetry_sink is not None:
                telemetry_sink({"usage": {"input_tokens": 2}})
            return zeta_model_shapes.ModelOutput(
                message={"content": "", "tool_calls": tool_calls}
            )

    registry = CapabilityRegistry()
    state = zeta_agent.RunState()
    ctx = zeta_agent.RunDependencies(
        event_sink=None,
        trace_store=None,
        tool_registry=registry,
        tool_executor=zeta_capability_executors.local_tool_executor(registry),
        builder=zeta_context.PromptBuilder(),
        abort_reason=never_abort,
        model_gateway=FakeGateway(),
    )

    state, info = asyncio.run(
        zeta_loop_steps.step(
            state,
            objective="answer",
            timeline=[],
            config=zeta_agent.AgentConfig(),
            allowed_capabilities=(),
            context="",
            tool_schema=registry.model_tool_schema(()),
            tools=[],
            ctx=ctx,
        )
    )

    assert info.kind == "model"
    assert info.final_answer == ""
    assert state.stop is None
    assert state.pending_tool_calls == tool_calls
    assert state.pending_model_telemetry == {"usage": {"input_tokens": 2}}
    assert isinstance(state.pending_tool_parent_id, str)
    projected = timeline_events(list(info.appended_events))
    assert projected[0]["type"] == "model"
    assert projected[0]["tool_calls"] == tool_calls


def test_zeta_step_pending_tools_returns_info_and_clears_pending_tools() -> None:
    registry = CapabilityRegistry()
    registry.register(
        _test_capability(
            "read",
            run_result={"ok": True, "content": [{"type": "text", "text": "done"}]},
        )
    )
    allowed_capabilities = ("test.read",)
    state = zeta_agent.RunState(
        pending_tool_calls=tool_call_fixture("call-1", name="read", path="README.md"),
        pending_model_telemetry={"usage": {"input_tokens": 3}},
        pending_tool_parent_id="assistant-1",
    )
    ctx = zeta_agent.RunDependencies(
        event_sink=None,
        trace_store=None,
        tool_registry=registry,
        tool_executor=zeta_capability_executors.local_tool_executor(registry),
        builder=zeta_context.PromptBuilder(),
        abort_reason=never_abort,
    )

    state, info = asyncio.run(
        zeta_loop_steps.step(
            state,
            objective="answer",
            timeline=[],
            config=zeta_agent.AgentConfig(),
            allowed_capabilities=allowed_capabilities,
            context="",
            tool_schema=registry.model_tool_schema(allowed_capabilities),
            tools=[],
            ctx=ctx,
        )
    )

    assert info.kind == "tools"
    assert info.appended_events == tuple(state.events)
    assert state.stop is None
    assert state.pending_tool_calls == []
    assert state.pending_model_telemetry == {}
    assert state.pending_tool_parent_id is None
    projected = timeline_events(list(info.appended_events))
    assert [event["type"] for event in projected] == ["tool_call", "tool_result"]
    assert projected[0]["caused_by"] == "assistant-1"
    assert projected[1]["caused_by"] == "assistant-1"
    assert projected[1]["model_telemetry"] == {"usage": {"input_tokens": 3}}


def test_zeta_step_tools_does_not_commit_partial_batch_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run_capability_step(*args: object, **kwargs: object) -> object:
        tool_call = args[0]
        if isinstance(tool_call, dict):
            tool_call_payload = cast(dict[str, Any], tool_call)
            if tool_call_payload.get("id") == "call-2":
                raise RuntimeError("tool batch interrupted")
        return zeta_capability_execution.CapabilityCallResult(
            events=[
                zeta_events.DraftEvent(
                    "tool_result",
                    "zeta",
                    {"tool_call_id": "call-1", "result": {"ok": True}},
                )
            ]
        )

    monkeypatch.setattr(
        zeta_loop_steps, "run_capability_step", fake_run_capability_step
    )
    registry = CapabilityRegistry()
    state = zeta_agent.RunState(
        pending_tool_calls=[
            *tool_call_fixture("call-1", name="read", path="README.md"),
            *tool_call_fixture("call-2", name="read", path="README.md"),
        ],
        pending_tool_parent_id="assistant-1",
    )
    ctx = zeta_agent.RunDependencies(
        event_sink=None,
        trace_store=None,
        tool_registry=registry,
        tool_executor=zeta_capability_executors.local_tool_executor(registry),
        builder=zeta_context.PromptBuilder(store=zeta_trace.InMemoryStore()),
        abort_reason=never_abort,
    )

    with pytest.raises(RuntimeError, match="tool batch interrupted"):
        asyncio.run(
            zeta_loop_steps.step_tools(
                state,
                config=zeta_agent.AgentConfig(),
                allowed_capabilities=(),
                tool_schema=registry.model_tool_schema(()),
                ctx=ctx,
            )
        )

    assert state.events == []


async def test_zeta_record_assistant_step_links_output_to_prompt() -> None:
    store = zeta_trace.InMemoryStore()
    state = zeta_agent.RunState()
    builder = zeta_context.PromptBuilder(store=store)
    prepared_prompt, _ = await loop_prompt.build_prompt_step(
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

    assistant, prompt_trace = loop_model.record_assistant_step(
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
    tool_schema = registry.model_tool_schema(())
    tool_call = {"id": "call-1", "function": {"name": "read", "arguments": "{}"}}
    ctx = zeta_agent.RunDependencies(
        event_sink=None,
        trace_store=None,
        tool_registry=registry,
        tool_executor=zeta_capability_executors.local_tool_executor(registry),
        builder=zeta_context.PromptBuilder(),
        abort_reason=never_abort,
    )

    def fake_handle_tool_call(
        received: dict[str, Any],
        **kwargs: object,
    ) -> loop_capability.CapabilityCallResult:
        assert received == tool_call
        assert kwargs["index"] == 0
        return loop_capability.CapabilityCallResult(
            events=[
                zeta_event_drafts.draft_from_runtime_event(
                    {"type": "tool_call", "id": "call-1", "tool_call_id": "call-1"},
                    session_id=None,
                    turn_id=None,
                ),
                zeta_event_drafts.draft_from_runtime_event(
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

    monkeypatch.setattr(loop_capability, "handle_tool_call", fake_handle_tool_call)

    result = asyncio.run(
        loop_capability.run_capability_step(
            tool_call,
            index=0,
            config=zeta_agent.AgentConfig(),
            allowed_capabilities=(),
            tool_schema=tool_schema,
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


def test_zeta_run_capability_step_dispatches_to_injected_executor() -> None:
    state = zeta_agent.RunState()
    calls: list[tuple[str, dict[str, Any]]] = []
    registry = CapabilityRegistry()

    def fail_in_process(_params: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("registry executor should not run")

    capability = RegisteredCapability(
        Capability(
            CapabilityId("test", "read"),
            "Read fixture.",
            {"type": "object"},
        ),
        InProcessCapabilityExecutor(fail_in_process),
    )
    registry.register(capability)

    class FakeExecutor:
        async def call(
            self,
            capability_id: str,
            params: dict[str, Any],
            *,
            base_dir: Path | None,
            effect_key: str | None,
        ) -> dict[str, Any]:
            del base_dir, effect_key
            calls.append((capability_id, params))
            return {"ok": True, "content": [{"type": "text", "text": "host"}]}

        async def aclose(self) -> None:
            return None

    allowed_capabilities = ("test.read",)
    ctx = zeta_agent.RunDependencies(
        event_sink=None,
        trace_store=None,
        tool_registry=registry,
        tool_executor=FakeExecutor(),
        builder=zeta_context.PromptBuilder(),
        abort_reason=never_abort,
    )

    result = asyncio.run(
        loop_capability.run_capability_step(
            {
                "id": "call-1",
                "function": {"name": "read", "arguments": '{"path": "README.md"}'},
            },
            index=0,
            config=zeta_agent.AgentConfig(),
            allowed_capabilities=allowed_capabilities,
            tool_schema=registry.model_tool_schema(allowed_capabilities),
            model_telemetry={},
            assistant_event_id="assistant-1",
            state=state,
            ctx=ctx,
        )
    )

    assert calls == [("test.read", {"path": "README.md"})]
    projected = timeline_events(result.events)
    assert projected[-1]["type"] == "tool_result"
    assert projected[-1]["result"] == {
        "ok": True,
        "content": [{"type": "text", "text": "host"}],
    }


def test_zeta_run_capability_step_records_executor_refusal() -> None:
    state = zeta_agent.RunState()
    registry = CapabilityRegistry()
    capability = _test_capability("read")
    registry.register(capability)
    allowed_capabilities = ("test.read",)

    class RefusingExecutor:
        async def call(
            self,
            capability_id: str,
            params: dict[str, Any],
            *,
            base_dir: Path | None,
            effect_key: str | None,
        ) -> dict[str, Any]:
            del params, base_dir, effect_key
            return {
                "ok": False,
                "error": {
                    "code": "unknown-tool",
                    "message": f"unknown tool: {capability_id}",
                    "data": {"capability_id": capability_id},
                },
            }

        async def aclose(self) -> None:
            return None

    ctx = zeta_agent.RunDependencies(
        event_sink=None,
        trace_store=None,
        tool_registry=registry,
        tool_executor=RefusingExecutor(),
        builder=zeta_context.PromptBuilder(),
        abort_reason=never_abort,
    )

    result = asyncio.run(
        loop_capability.run_capability_step(
            {"id": "call-1", "function": {"name": "read", "arguments": "{}"}},
            index=0,
            config=zeta_agent.AgentConfig(),
            allowed_capabilities=allowed_capabilities,
            tool_schema=registry.model_tool_schema(allowed_capabilities),
            model_telemetry={},
            assistant_event_id="assistant-1",
            state=state,
            ctx=ctx,
        )
    )

    projected = timeline_events(result.events)
    assert projected[-1]["type"] == "tool_result"
    assert projected[-1]["status"] == "refused"
    assert projected[-1]["result"]["error"] == {
        "code": "unknown-tool",
        "message": "unknown tool: test.read",
        "data": {"capability_id": "test.read"},
    }


def test_zeta_run_capability_step_reconciles_existing_terminal_result(
    monkeypatch,
) -> None:
    state = zeta_agent.RunState(
        events=[
            zeta_event_drafts.draft_from_runtime_event(
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
    tool_schema = registry.model_tool_schema(())
    invoked = False
    ctx = zeta_agent.RunDependencies(
        event_sink=None,
        trace_store=None,
        tool_registry=registry,
        tool_executor=zeta_capability_executors.local_tool_executor(registry),
        builder=zeta_context.PromptBuilder(),
        abort_reason=never_abort,
    )

    def fail_handle_tool_call(
        *args: object, **kwargs: object
    ) -> loop_capability.CapabilityCallResult:
        nonlocal invoked
        invoked = True
        return loop_capability.CapabilityCallResult(events=[])

    monkeypatch.setattr(loop_capability, "handle_tool_call", fail_handle_tool_call)

    result = asyncio.run(
        loop_capability.run_capability_step(
            {"id": "call-1", "function": {"name": "read", "arguments": "{}"}},
            index=0,
            config=zeta_agent.AgentConfig(),
            allowed_capabilities=(),
            tool_schema=tool_schema,
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


class IpcMemoryTransport(asyncio.Transport):
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.closed = False

    def write(self, data: bytes | bytearray | memoryview) -> None:
        self.buffer.extend(data)

    def is_closing(self) -> bool:
        return True

    def close(self) -> None:
        self.closed = True

    def getvalue(self) -> str:
        return self.buffer.decode()


class IpcImmediateDrainProtocol(asyncio.Protocol):
    async def _drain_helper(self) -> None:
        return None


_IPC_STREAM_LOOP = asyncio.new_event_loop()


def ipc_streams(
    input_text: str = "",
    output: IpcMemoryTransport | None = None,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, IpcMemoryTransport]:
    reader = asyncio.StreamReader(
        limit=ipc_framing.MAX_FRAME_BYTES,
        loop=_IPC_STREAM_LOOP,
    )
    if input_text:
        reader.feed_data(input_text.encode())
    reader.feed_eof()
    output = output or IpcMemoryTransport()
    writer = asyncio.StreamWriter(
        output,
        IpcImmediateDrainProtocol(),
        None,
        _IPC_STREAM_LOOP,
    )
    return reader, writer, output


def ipc_messages(output: IpcMemoryTransport) -> list[dict[str, Any]]:
    return [json.loads(line) for line in output.getvalue().splitlines()]


def ipc_client(
    input_stream: asyncio.StreamReader | None = None,
    output: IpcMemoryTransport | None = None,
    *,
    session: zeta_runtime_context.RuntimeContext | None = None,
    dispatcher: harness_dispatch.QueueingDispatcher | None = None,
    initialized: bool = True,
) -> tuple[
    ipc_connection.JsonRpcConnection,
    ipc_routes.IpcClient,
    ipc_connection.JsonRpcRouter,
]:
    reader, writer, output = ipc_streams(output=output)
    connection = ipc_connection.JsonRpcConnection(input_stream or reader, writer)
    if initialized:
        connection.initialized = True
        connection.peer_name = "test"
        connection.roles = frozenset({"client"})
    if session is None:
        event_store = zeta_events.MemoryEventStore()
        session = zeta_runtime_context.RuntimeContext(
            session_id="ctx-session",
            event_sink=event_store,
            trace_store=zeta_trace.InMemoryStore(),
            tool_registry=CapabilityRegistry(),
            state_dir=Path("/tmp"),
            session_dir=Path("/tmp") / "sessions" / "ctx-session",
        )

    def notify_event(event: Event) -> None:
        asyncio.create_task(
            connection.notify("event", {"event": zeta_event_wire.event_to_wire(event)})
        )

    if dispatcher is None:
        dispatcher = harness_dispatch.QueueingDispatcher(
            session.event_sink,
            publish_event=notify_event,
        )
    client = ipc_routes.IpcClient(
        connection=connection,
        session=session,
        dispatcher=dispatcher,
    )
    router = ipc_routes.build_ipc_router(client)
    return connection, client, router


def ipc_client_without_connection(
    *,
    session: zeta_runtime_context.RuntimeContext | None = None,
) -> ipc_routes.IpcClient:
    if session is None:
        event_store = zeta_events.MemoryEventStore()
        session = zeta_runtime_context.RuntimeContext(
            session_id="ctx-session",
            event_sink=event_store,
            trace_store=zeta_trace.InMemoryStore(),
            tool_registry=CapabilityRegistry(),
            state_dir=Path("/tmp"),
            session_dir=Path("/tmp") / "sessions" / "ctx-session",
        )
    return ipc_routes.IpcClient(
        connection=None,
        session=session,
        dispatcher=harness_dispatch.QueueingDispatcher(session.event_sink),
    )


def run_ipc_messages(
    input_text: str,
    output: IpcMemoryTransport,
    *,
    session: zeta_runtime_context.RuntimeContext | None = None,
    dispatcher: harness_dispatch.QueueingDispatcher | None = None,
) -> ipc_routes.IpcClient:
    input_stream, _, _ = ipc_streams(input_text)
    connection, client, router = ipc_client(
        input_stream,
        output,
        session=session,
        dispatcher=dispatcher,
        initialized=False,
    )
    asyncio.run(connection.serve(router))
    return client


def ipc_client_transcript(
    *messages: dict[str, Any],
    initialize_id: str | int = "initialize",
) -> str:
    initialize = {
        "jsonrpc": "2.0",
        "id": initialize_id,
        "method": "initialize",
        "params": {
            "protocol_versions": [0],
            "peer": {"name": "test-client", "version": "1"},
            "roles": ["client"],
        },
    }
    return "".join(f"{json.dumps(message)}\n" for message in (initialize, *messages))


def test_zeta_ipc_route_event_logs_dispatch_failure(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ipc_client_without_connection()

    async def failing_drain() -> list[Event]:
        raise RuntimeError("dispatch boom")

    monkeypatch.setattr(client.dispatcher, "drain", failing_drain)
    event = ipc_event("hi", cursor=1)

    with caplog.at_level(logging.ERROR, logger="zeta.ipc.routes"):
        asyncio.run(ipc_routes.route_event(client, event))

    assert any(
        "Background event routing failed" in record.getMessage()
        for record in caplog.records
    )
    assert any(record.exc_info for record in caplog.records)


def test_zeta_ipc_initialize_returns_server_metadata() -> None:
    input_text = ipc_client_transcript(initialize_id=1)
    output = IpcMemoryTransport()

    run_ipc_messages(input_text, output)

    assert ipc_messages(output) == [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "protocol_version": 0,
                "runtime": {"name": "zeta", "version": "unknown"},
                "roles": ["client"],
                "config": {},
                "heartbeat_seconds": 10,
                "max_in_flight": 64,
            },
        }
    ]


def test_zeta_ipc_parameter_errors_keep_the_request_id() -> None:
    input_text = ipc_client_transcript(
        {
            "jsonrpc": "2.0",
            "id": "bad-params",
            "method": "session.list",
            "params": {"unexpected": True},
        }
    )
    output = IpcMemoryTransport()

    run_ipc_messages(input_text, output)

    assert ipc_messages(output)[1] == {
        "jsonrpc": "2.0",
        "id": "bad-params",
        "error": {
            "code": -32602,
            "message": "unsupported parameter 'unexpected'",
        },
    }


def test_zeta_cli_exposes_ipc_stdio_without_rpc_alias() -> None:
    assert "ipc" in cli_main.cli.commands
    assert "rpc" not in cli_main.cli.commands
    ipc_command = cli_main.cli.commands["ipc"]
    assert isinstance(ipc_command, Group)
    assert set(ipc_command.commands) == {"stdio"}


def test_zeta_ipc_stdio_accepts_a_tui_shaped_transcript(tmp_path: Path) -> None:
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from zeta.cli.main import main; raise SystemExit(main())",
            "ipc",
            "stdio",
        ],
        cwd=tmp_path,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    input_stream = process.stdin
    output_stream = process.stdout
    error_stream = process.stderr
    assert input_stream is not None
    assert output_stream is not None
    assert error_stream is not None

    def request(message: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        input_stream.write(json.dumps(message) + "\n")
        input_stream.flush()
        notifications: list[dict[str, Any]] = []
        while True:
            line = output_stream.readline()
            assert line
            response = json.loads(line)
            if response.get("method") == "event":
                notifications.append(response)
                continue
            if response.get("id") == message["id"]:
                return response, notifications

    initialized, notifications = request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocol_versions": [0],
                "peer": {"name": "zeta-tui", "version": "0.1.0"},
                "roles": ["client"],
                "heartbeat_seconds": 10,
                "max_in_flight": 64,
            },
        }
    )
    assert notifications == []
    assert initialized["result"] == {
        "protocol_version": 0,
        "runtime": {
            "name": "zeta",
            "version": initialized["result"]["runtime"]["version"],
        },
        "roles": ["client"],
        "config": {},
        "heartbeat_seconds": 10,
        "max_in_flight": 64,
    }

    sessions, notifications = request(
        {"jsonrpc": "2.0", "id": 2, "method": "session.list", "params": {}}
    )
    assert sessions["result"] == {"sessions": []}
    assert notifications == []

    started, notifications = request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "session.start",
            "params": {"message": "hello", "idempotency_key": "message-1"},
        }
    )
    listed, later_notifications = request(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "events.list",
            "params": {"limit": 200},
        }
    )
    notifications.extend(later_notifications)
    assert started["result"]["status"] == "queued"
    assert notifications
    notification_cursors = [
        notification["params"]["event"]["cursor"] for notification in notifications
    ]
    assert notification_cursors == sorted(notification_cursors)
    stored_ids = {event["id"] for event in listed["result"]["events"]}
    assert all(
        notification["params"]["event"]["id"] in stored_ids
        for notification in notifications
    )
    assert all("type" in event for event in listed["result"]["events"])

    ping, notifications = request(
        {"jsonrpc": "2.0", "id": 5, "method": "ping", "params": {}}
    )
    assert ping["result"] == {}
    assert notifications == []
    shutdown, _ = request(
        {
            "jsonrpc": "2.0",
            "id": "zeta-tui-shutdown",
            "method": "shutdown",
            "params": {"reason": "test complete"},
        }
    )
    assert shutdown["result"] == {}
    input_stream.close()
    assert process.wait(timeout=10) == 0
    output_stream.close()
    error_stream.close()


def test_zeta_ipc_stdio_requires_initialization_and_client_role(tmp_path: Path) -> None:
    command = [
        sys.executable,
        "-c",
        "from zeta.cli.main import main; raise SystemExit(main())",
        "ipc",
        "stdio",
    ]
    before_initialize = subprocess.run(
        command,
        cwd=tmp_path,
        input=(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "events.list",
                    "params": {},
                }
            )
            + "\n"
        ),
        capture_output=True,
        text=True,
        timeout=10,
    )
    response = json.loads(before_initialize.stdout)
    assert response["error"]["code"] == -32600
    assert response["error"]["data"]["code"] == "not_initialized"

    transcript = "\n".join(
        json.dumps(message)
        for message in (
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocol_versions": [0],
                    "peer": {"name": "client", "version": "1"},
                    "roles": ["client"],
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "events.publish",
                "params": {"type": "note.created", "payload": {}},
            },
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "shutdown",
                "params": {},
            },
        )
    )
    initialized = subprocess.run(
        command,
        cwd=tmp_path,
        input=transcript + "\n",
        capture_output=True,
        text=True,
        timeout=10,
    )
    responses = [json.loads(line) for line in initialized.stdout.splitlines()]
    denied = next(response for response in responses if response.get("id") == 2)
    assert denied["error"]["code"] == -32601
    assert denied["error"]["data"]["code"] == "method_not_found"


def test_zeta_ipc_router_registers_only_retained_methods() -> None:
    _, _, router = ipc_client()

    assert set(router.routes) == {
        "events.publish",
        "events.list",
        "session.start",
        "session.send",
        "session.status",
        "session.list",
        "session.cancel",
    }


def test_zeta_ipc_queues_and_queries_authored_sessions(tmp_path: Path) -> None:
    event_store = RuntimeEventStore.open(tmp_path / "events.sqlite3")
    session = zeta_runtime_context.RuntimeContext(
        session_id="ipc-control",
        event_sink=event_store,
        trace_store=InMemoryStore(),
        tool_registry=CapabilityRegistry(),
        state_dir=tmp_path,
        session_dir=tmp_path / "sessions" / "ipc-control",
    )
    master = zeta_agent_spec.AgentSpec(
        slug="zeta.master",
        name="Master",
        description="Work with the user.",
        instructions="{{ event.payload.message }}",
        path=tmp_path / "master.md",
        content_address="master",
    )
    snapshot = SimpleNamespace(
        generation_id="generation-1",
        project=SimpleNamespace(specs=(master,)),
        manifest={"generation": 1},
    )
    client = ipc_routes.IpcClient(
        connection=None,
        session=session,
        dispatcher=harness_dispatch.QueueingDispatcher(event_store),
        project_snapshot=cast(Any, snapshot),
    )
    router = ipc_routes.build_ipc_router(client)

    async def request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        response = await router.response_for_message(
            {"jsonrpc": "2.0", "id": method, "method": method, "params": params}
        )
        assert response is not None
        return response

    started = asyncio.run(
        request(
            "session.start",
            {"message": "Plan the release.", "idempotency_key": "start-1"},
        )
    )["result"]
    repeated = asyncio.run(
        request(
            "session.start",
            {"message": "Plan the release.", "idempotency_key": "start-1"},
        )
    )["result"]
    sent = asyncio.run(
        request(
            "session.send",
            {
                "session_id": started["session_id"],
                "message": "Include the migration.",
                "idempotency_key": "send-1",
            },
        )
    )["result"]
    status = asyncio.run(
        request("session.status", {"session_id": started["session_id"]})
    )["result"]
    listed = asyncio.run(request("session.list", {}))["result"]

    assert repeated == started
    assert started["status"] == "queued"
    assert started["agent_id"] == "zeta.master"
    assert sent["session_id"] == started["session_id"]
    assert sent["status"] == "queued"
    assert status["status"] == "queued"
    assert status["queued_turns"] == 2
    assert listed == {"sessions": [status]}
    assert event_store.list_attempts() == []


def test_zeta_ipc_reports_unknown_and_conflicting_sessions(tmp_path: Path) -> None:
    event_store = RuntimeEventStore.open(tmp_path / "events.sqlite3")
    session = zeta_runtime_context.RuntimeContext(
        session_id="ipc-control",
        event_sink=event_store,
        trace_store=InMemoryStore(),
        tool_registry=CapabilityRegistry(),
        state_dir=tmp_path,
        session_dir=tmp_path / "sessions" / "ipc-control",
    )
    specs = tuple(
        zeta_agent_spec.AgentSpec(
            slug=agent_id,
            name=agent_id,
            description="Handles work.",
            instructions="Handle work.",
            path=tmp_path / f"{agent_id}.md",
            content_address=agent_id,
        )
        for agent_id in ("agent-a", "agent-b", "zeta.master")
    )
    snapshot = SimpleNamespace(
        generation_id="generation-1",
        project=SimpleNamespace(specs=specs),
        manifest={"generation": 1},
    )
    for index, agent_id in enumerate(("agent-a", "agent-b"), start=1):
        event_store.accept(
            DraftEvent(
                "runtime.queue_item.completed",
                "zeta",
                {
                    "queue_item_id": f"qi-conflict-{index}",
                    "event_id": f"event-{index}",
                    "target_agent": agent_id,
                    "session_id": "session-conflict",
                    "status": "completed",
                },
                session_id="session-conflict",
            )
        )
    event_store.accept(
        DraftEvent(
            "runtime.queue_item.completed",
            "zeta",
            {
                "queue_item_id": "qi-unavailable",
                "event_id": "event-unavailable",
                "target_agent": "agent-removed",
                "session_id": "session-unavailable",
                "status": "completed",
            },
            session_id="session-unavailable",
        )
    )
    client = ipc_routes.IpcClient(
        connection=None,
        session=session,
        dispatcher=harness_dispatch.QueueingDispatcher(event_store),
        project_snapshot=cast(Any, snapshot),
    )
    router = ipc_routes.build_ipc_router(client)

    unknown = asyncio.run(
        router.response_for_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session.status",
                "params": {"session_id": "session-missing"},
            }
        )
    )
    conflict = asyncio.run(
        router.response_for_message(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session.send",
                "params": {
                    "session_id": "session-conflict",
                    "message": "Continue.",
                },
            }
        )
    )
    unavailable = asyncio.run(
        router.response_for_message(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session.send",
                "params": {
                    "session_id": "session-unavailable",
                    "message": "Continue.",
                },
            }
        )
    )

    assert unknown is not None
    assert unknown["error"]["data"]["code"] == "session_not_found"
    assert conflict is not None
    assert conflict["error"]["data"]["code"] == "session_owner_conflict"
    assert unavailable is not None
    assert unavailable["error"]["data"]["code"] == "session_owner_unavailable"


def test_zeta_ipc_oversized_line_returns_parse_error_and_continues() -> None:
    class FakeWriter:
        def __init__(self) -> None:
            self.buffer = bytearray()
            self.closed = False

        def write(self, data: bytes | bytearray | memoryview) -> None:
            self.buffer.extend(data)

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    async def run_case() -> list[dict[str, Any]]:
        reader = asyncio.StreamReader(limit=32)
        reader.feed_data(
            ('{"jsonrpc":"2.0","id":1,"method":"' + ("x" * 512) + '"}\n').encode()
        )
        reader.feed_data(ipc_client_transcript(initialize_id=2).encode())
        reader.feed_eof()
        writer = FakeWriter()
        connection = ipc_connection.JsonRpcConnection(
            reader,
            cast(Any, writer),
            max_frame_bytes=256,
        )
        client = SimpleNamespace(connection=connection)
        router = ipc_connection.JsonRpcRouter(cast(Any, client))

        await connection.serve(router)
        return [json.loads(line) for line in writer.buffer.decode().splitlines()]

    messages = asyncio.run(run_case())
    assert messages[0]["error"]["code"] == -32700
    assert {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {
            "protocol_version": 0,
            "runtime": {"name": "zeta", "version": "unknown"},
            "roles": ["client"],
            "config": {},
            "heartbeat_seconds": 10,
            "max_in_flight": 64,
        },
    } in messages


def test_zeta_ipc_unknown_method_returns_structured_error() -> None:
    input_text = ipc_client_transcript(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "events.subscribe",
            "params": {"input": {}, "effect_key": "test"},
        }
    )
    output = IpcMemoryTransport()

    run_ipc_messages(input_text, output)

    messages = [message for message in ipc_messages(output) if message.get("id") == 1]
    assert messages == [
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


def test_zeta_ipc_router_response_for_message_does_not_write_to_connection() -> None:
    output = IpcMemoryTransport()
    _, _, router = ipc_client(output=output)

    response = asyncio.run(
        router.response_for_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "events.list",
                "params": {},
            }
        )
    )

    assert response == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"events": [], "next_cursor": None},
    }
    assert output.getvalue() == ""


def test_zeta_ipc_events_publish_uses_protocol_event_shape(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    session = zeta_runtime_context.RuntimeContext(
        session_id="ctx-session",
        event_sink=event_store,
        trace_store=zeta_trace.InMemoryStore(),
        tool_registry=CapabilityRegistry(),
        state_dir=tmp_path,
        session_dir=tmp_path / "sessions" / "ctx-session",
    )
    output = IpcMemoryTransport()
    _, _, router = ipc_client(output=output, session=session)

    message = asyncio.run(
        router.response_for_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "events.publish",
                "params": {
                    "type": "zeta.user_message",
                    "payload": {"content": "hello"},
                    "session_id": "ctx-session",
                    "run_id": "run_1",
                },
            }
        )
    )
    assert message is not None
    assert message["result"]["inserted"] is True
    assert message["result"]["event"]["type"] == "zeta.user_message"
    assert message["result"]["event"]["source"] == "test"
    assert message["result"]["event"]["payload"] == {"content": "hello"}
    assert message["result"]["event"]["cursor"] == 1
    assert set(message["result"]) == {"inserted", "event"}


def test_zeta_ipc_events_publish_returns_before_routing_finishes(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
        session = zeta_runtime_context.RuntimeContext(
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
            invocation: harness_dispatch.AgentInvocation,
        ) -> dict[str, object]:
            started.set()
            await release.wait()
            return {
                "outcome": "handled",
                "event_id": invocation.triggering_event.id,
            }

        dispatcher = harness_dispatch.QueueingDispatcher(
            event_store,
            executors=[
                harness_dispatch.ExecutableAgent(
                    harness_dispatch.AgentDefinition(
                        "slow-agent",
                        (harness_dispatch.EventPattern("zeta.user_message"),),
                    ),
                    run=run_agent,
                )
            ],
        )
        output = IpcMemoryTransport()
        _, _, router = ipc_client(output=output, session=session, dispatcher=dispatcher)

        await router.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "events.publish",
                "params": {
                    "type": "zeta.user_message",
                    "payload": {"content": "hello"},
                    "session_id": "ctx-session",
                },
            }
        )

        message = next(
            message for message in ipc_messages(output) if message.get("id") == 1
        )
        assert message["result"]["inserted"] is True
        assert set(message["result"]) == {"inserted", "event"}
        assert not release.is_set()

        await asyncio.wait_for(started.wait(), timeout=1)
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(run())


def test_zeta_ingress_render_template_reports_missing_field() -> None:
    draft = DraftEvent(event_type="x", source="s", payload={"channel": "C1"})

    assert harness_connector_bridge.render_template("{channel}", draft) == "C1"
    with pytest.raises(RuntimeError, match="missing field"):
        harness_connector_bridge.render_template("{absent}", draft)


def test_zeta_ipc_events_publish_rejects_lifecycle_event_ingress(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    session = zeta_runtime_context.RuntimeContext(
        session_id="ctx-session",
        event_sink=event_store,
        trace_store=zeta_trace.InMemoryStore(),
        tool_registry=CapabilityRegistry(),
        state_dir=tmp_path,
        session_dir=tmp_path / "sessions" / "ctx-session",
    )
    _, _, router = ipc_client(session=session)
    message = asyncio.run(
        router.response_for_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "events.publish",
                "params": {
                    "type": "runtime.attempt.started",
                    "payload": {"attempt_id": "att_1"},
                    "session_id": "ctx-session",
                },
            }
        )
    )
    assert message is not None
    assert message["error"]["code"] == -32602
    assert message["error"]["data"]["code"] == "reserved_runtime_event"
    assert event_store.list_events(zeta_events.Filter()) == []


def test_zeta_ipc_events_publish_reports_invalid_journal_values(tmp_path: Path) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    session = zeta_runtime_context.RuntimeContext(
        session_id="ctx-session",
        event_sink=event_store,
        trace_store=zeta_trace.InMemoryStore(),
        tool_registry=CapabilityRegistry(),
        state_dir=tmp_path,
        session_dir=tmp_path / "sessions" / "ctx-session",
    )
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "events.publish",
            "params": {
                "type": "invalid.payload",
                "payload": {"value": float("nan")},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "events.publish",
            "params": {
                "type": "invalid.session",
                "payload": {},
                "session_id": "",
            },
        },
    ]
    _, _, router = ipc_client(session=session)
    responses = [
        asyncio.run(router.response_for_message(request)) for request in requests
    ]
    messages = {message["id"]: message for message in responses if message is not None}
    for request_id in (1, 2):
        assert messages[request_id]["error"]["code"] == -32602
        assert messages[request_id]["error"]["data"]["code"] == "invalid_event"
    assert event_store.list_events(zeta_events.Filter()) == []


def test_zeta_runtime_store_resolves_duplicate_drafts_before_payload_validation(
    tmp_path: Path,
) -> None:
    event_store = RuntimeEventStore.open(tmp_path / "events.sqlite3")
    try:
        inserted = event_store.accept(
            DraftEvent(
                event_type="example.created",
                source="test",
                payload={"value": 1},
                idempotency_key="example:1",
            )
        )
        duplicate = event_store.accept(
            DraftEvent(
                event_type="example.changed",
                source="test",
                payload={"value": float("nan")},
                idempotency_key="example:1",
            )
        )
    finally:
        event_store.close()

    assert inserted.inserted
    assert not duplicate.inserted
    assert duplicate.event == inserted.event


def test_zeta_ipc_events_list_uses_event_store_filter_names(tmp_path: Path) -> None:
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
    session = zeta_runtime_context.RuntimeContext(
        session_id="ctx-session",
        event_sink=event_store,
        trace_store=zeta_trace.InMemoryStore(),
        tool_registry=CapabilityRegistry(),
        state_dir=tmp_path,
        session_dir=tmp_path / "sessions" / "ctx-session",
    )
    _, _, router = ipc_client(session=session)
    message = asyncio.run(
        router.response_for_message(
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
    )
    assert message is not None
    assert [event["payload"]["content"] for event in message["result"]["events"]] == [
        "two",
        "three",
    ]
    assert message["result"]["next_cursor"] == 3


def test_zeta_ipc_events_list_accepts_zero_and_rejects_invalid_limits(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    event_store.accept(
        DraftEvent(
            event_type="zeta.user_message",
            source="test",
            payload={"content": "hello"},
        )
    )
    session = zeta_runtime_context.RuntimeContext(
        session_id="ctx-session",
        event_sink=event_store,
        trace_store=zeta_trace.InMemoryStore(),
        tool_registry=CapabilityRegistry(),
        state_dir=tmp_path,
        session_dir=tmp_path / "sessions" / "ctx-session",
    )
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "events.list",
            "params": {"limit": 0},
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "events.list",
            "params": {"limit": -1},
        },
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "events.list",
            "params": {"limit": True},
        },
    ]
    _, _, router = ipc_client(session=session)
    responses = [
        asyncio.run(router.response_for_message(request)) for request in requests
    ]
    messages = {message["id"]: message for message in responses if message is not None}
    assert messages[1]["result"] == {"events": [], "next_cursor": None}
    for request_id in (2, 3):
        assert messages[request_id]["error"]["code"] == -32602
        assert messages[request_id]["error"]["data"]["code"] == "invalid_limit"


def test_zeta_session_turn_retry_recovers_completed_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fake_run_session_request(
        _params: dict[str, Any],
        *,
        run_id: str,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        run_ids.append(run_id)
        return {
            "run_id": run_id,
            "outcome": "completed",
            "final_answer": "done once",
            "trace": {},
        }

    event_store = RuntimeEventStore.open(tmp_path / "zeta.sqlite3")
    context = zeta_runtime_context.RuntimeContext(
        session_id="ctx-session",
        event_sink=event_store,
        trace_store=zeta_trace.InMemoryStore(),
        tool_registry=CapabilityRegistry(),
        state_dir=tmp_path,
        session_dir=tmp_path / "sessions" / "ctx-session",
    )
    run_ids: list[str] = []
    monkeypatch.setattr(
        harness_session_turn,
        "run_session_request",
        fake_run_session_request,
    )
    dispatcher = harness_dispatch.QueueingDispatcher(
        event_store,
        executors=[
            harness_session_turn.session_turn_agent(
                context,
                publish_event=lambda _event: None,
            )
        ],
    )
    params = {
        "objective": "answer",
        "tools": [],
        "idempotency_key": "logical-request-1",
    }

    try:
        first = asyncio.run(
            harness_session_turn.submit_session_turn(
                params,
                runtime_context=context,
                event_dispatcher=dispatcher,
            )
        )
        second = asyncio.run(
            harness_session_turn.submit_session_turn(
                params,
                runtime_context=context,
                event_dispatcher=dispatcher,
            )
        )
        assert len(run_ids) == 1
        assert second == first
        assert first["final_answer"] == "done once"
        assert (
            len(event_store.list_events(Filter(event_type="session.turn.requested")))
            == 1
        )
    finally:
        event_store.close()


def test_zeta_session_agent_request_uses_active_model_selection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    context = zeta_runtime_context.RuntimeContext(
        session_id="ctx-session",
        event_sink=zeta_events.MemoryEventStore(),
        trace_store=zeta_trace.InMemoryStore(),
        tool_registry=CapabilityRegistry(),
        state_dir=tmp_path,
        session_dir=tmp_path / "sessions" / "ctx-session",
    )
    selection = ModelSelection(
        profile="codex",
        model="gpt-5.5",
        url="https://chatgpt.com/backend-api",
        thinking="low",
        api="codex-responses",
    )
    captured: dict[str, Path] = {}

    def active_model_selection(*, session_dir: Path | None = None) -> ModelSelection:
        assert session_dir is not None
        captured["session_dir"] = session_dir
        return selection

    monkeypatch.setattr(
        zeta_requests,
        "active_model_selection",
        active_model_selection,
    )

    request = zeta_requests.session_agent_request_for_context(
        {"objective": "answer"},
        runtime_context=context,
    )

    assert captured["session_dir"] == context.session_dir
    assert request.config.model_profile == "codex"
    assert request.config.model_name == "gpt-5.5"
    assert request.config.model_url == "https://chatgpt.com/backend-api"
    assert request.config.thinking == "low"
    assert request.config.model_api == "codex-responses"


def test_zeta_session_agent_request_preserves_explicit_model_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    context = zeta_runtime_context.RuntimeContext(
        session_id="ctx-session",
        event_sink=zeta_events.MemoryEventStore(),
        trace_store=zeta_trace.InMemoryStore(),
        tool_registry=CapabilityRegistry(),
        state_dir=tmp_path,
        session_dir=tmp_path / "sessions" / "ctx-session",
    )

    def fail_active_model_selection(*, session_dir: Path | None = None) -> None:
        raise AssertionError("active model selection should not be resolved")

    monkeypatch.setattr(
        zeta_requests,
        "active_model_selection",
        fail_active_model_selection,
    )

    request = zeta_requests.session_agent_request_for_context(
        {
            "objective": "answer",
            "model": "explicit-model",
            "url": "http://127.0.0.1:9999/v1/chat/completions",
        },
        runtime_context=context,
    )

    assert request.config.model_name == "explicit-model"
    assert request.config.model_url == "http://127.0.0.1:9999/v1/chat/completions"


def test_zeta_ipc_session_cancel_records_a_durable_request(tmp_path: Path) -> None:
    event_store = RuntimeEventStore.open(tmp_path / "events.sqlite3")
    queued = submit_session_message(
        event_store,
        message="Long task",
        agent_id="zeta.master",
        session_id="session-1",
        project_generation="generation-1",
    )
    requested = event_store.get(queued["event_id"])
    assert requested is not None
    assert (
        event_store.claim_next_queue_item(
            "worker-1",
            lease_ms=1_000,
            now_ms=requested.timestamp_ms + 1,
        )
        is not None
    )
    session = zeta_runtime_context.RuntimeContext(
        session_id="ipc-control",
        event_sink=event_store,
        trace_store=InMemoryStore(),
        tool_registry=CapabilityRegistry(),
        state_dir=tmp_path,
        session_dir=tmp_path / "sessions" / "ipc-control",
    )
    client = ipc_client_without_connection(session=session)

    result = asyncio.run(
        ipc_routes.session_cancel(
            {"run_id": queued["run_id"], "reason": "user changed direction"},
            client,
        )
    )

    assert result == {
        "cancelled": True,
        "changed": True,
        "run_id": queued["run_id"],
        "queue_item_id": queued["queue_item_id"],
        "session_id": "session-1",
        "status": "cancelling",
        "terminal_status": None,
    }
    assert event_store.queue_item_cancellation_requested(queued["queue_item_id"])
    event_store.close()


def test_zeta_ipc_session_cancel_survives_a_new_client(tmp_path: Path) -> None:
    event_store = RuntimeEventStore.open(tmp_path / "events.sqlite3")
    queued = submit_session_message(
        event_store,
        message="Queued task",
        agent_id="zeta.master",
        session_id="session-1",
        project_generation="generation-1",
    )
    session = zeta_runtime_context.RuntimeContext(
        session_id="new-client",
        event_sink=event_store,
        trace_store=InMemoryStore(),
        tool_registry=CapabilityRegistry(),
        state_dir=tmp_path,
        session_dir=tmp_path / "sessions" / "new-client",
    )
    client = ipc_client_without_connection(session=session)

    cancelled = asyncio.run(
        ipc_routes.session_cancel({"run_id": queued["run_id"]}, client)
    )
    repeated = asyncio.run(
        ipc_routes.session_cancel({"run_id": queued["run_id"]}, client)
    )
    unknown = asyncio.run(ipc_routes.session_cancel({"run_id": "run_unknown"}, client))

    assert cancelled["status"] == "cancelled"
    assert cancelled["changed"] is True
    assert repeated["status"] == "already_cancelled"
    assert repeated["changed"] is False
    assert unknown == {
        "cancelled": False,
        "changed": False,
        "run_id": "run_unknown",
        "queue_item_id": None,
        "session_id": None,
        "status": "unknown",
        "terminal_status": None,
    }
    event_store.close()


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

    assert harness_queue.terminal_queue_item_result(
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
    context = zeta_runtime_context.RuntimeContext(
        session_id="ctx-session",
        event_sink=zeta_events.MemoryEventStore(),
        trace_store=zeta_trace.InMemoryStore(),
        tool_registry=CapabilityRegistry(),
        state_dir=tmp_path,
        session_dir=tmp_path / "sessions" / "ctx-session",
    )
    published: list[Event | DraftEvent] = []
    captured: dict[str, Any] = {}

    async def fake_run_session_request(
        params: dict[str, Any],
        *,
        run_id: str,
        caused_by: str,
        publish_event: Callable[[Event | DraftEvent], None],
        runtime_context: zeta_runtime_context.RuntimeContext,
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
        harness_session_turn, "run_session_request", fake_run_session_request
    )

    agent = harness_session_turn.session_turn_agent(
        context,
        publish_event=published.append,
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
            runner(
                harness_dispatch.AgentInvocation(
                    agent.definition,
                    triggering_event,
                    cancellation_event=cancellation_event,
                )
            ),
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


def test_zeta_run_agent_records_user_message_and_returns_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    context = zeta_runtime_context.RuntimeContext(
        session_id="ctx-session",
        event_sink=zeta_events.MemoryEventStore(),
        trace_store=zeta_trace.InMemoryStore(),
        tool_registry=CapabilityRegistry(),
        state_dir=tmp_path,
        session_dir=tmp_path / "sessions" / "ctx-session",
    )
    published: list[Event] = []
    captured: dict[str, Any] = {}

    async def fake_run_agent_loop(
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

    monkeypatch.setattr(zeta_agent, "run_agent_loop", fake_run_agent_loop)

    result = asyncio.run(
        zeta_agent.run_agent(
            zeta_agent.AgentRunRequest(
                objective="answer",
                runtime="zeta-test",
                tools=(),
                context="",
                config=zeta_agent.AgentConfig(
                    model_profile="qwen",
                    model_name="qwen3.6-27b-q8-local",
                    model_url="http://127.0.0.1:8080/v1/chat/completions",
                    model_api="chat-completions",
                ),
            ),
            run_id="run_direct",
            caused_by="evt_request",
            publish_event=published.append,
            runtime_context=context,
            cancellation_event=None,
            tool_executor=zeta_capability_executors.local_tool_executor(
                context.tool_registry
            ),
        )
    )

    assert result.final_answer == "done"
    assert captured["objective"] == "answer"
    assert captured["timeline"] == []
    assert captured["config"].model_session_id == "ctx-session"
    assert captured["kwargs"]["caused_by"] == "evt_request"
    assert [event.event_type for event in published] == ["zeta.user_message"]
    assert published[0].payload["content"] == "answer"
    assert published[0].payload["model"] == {
        "profile": "qwen",
        "model": "qwen3.6-27b-q8-local",
        "url": "http://127.0.0.1:8080/v1/chat/completions",
        "api": "chat-completions",
    }
    assert published[0].run_id == "run_direct"


def test_zeta_run_agent_exposes_query_log_and_returns_prior_session_history(
    tmp_path: Path,
) -> None:
    registry = CapabilityRegistry()
    register_builtin_tools(registry)
    event_store = zeta_events.MemoryEventStore()
    event_store.accept(
        DraftEvent(
            "zeta.user_message",
            "zeta",
            {"content": "repair the parser"},
            session_id="ctx-session",
            run_id="run-prior-1111",
        )
    )
    event_store.accept(
        DraftEvent(
            "runtime.queue_item.completed",
            "zeta",
            {
                "target_agent": "zeta.session.turn",
                "result": {
                    "outcome": "completed",
                    "final_answer": "parser repaired",
                },
            },
            session_id="ctx-session",
            run_id="run-prior-1111",
        )
    )
    context = zeta_runtime_context.RuntimeContext(
        session_id="ctx-session",
        event_sink=event_store,
        trace_store=zeta_trace.InMemoryStore(session_id="ctx-session"),
        tool_registry=registry,
        state_dir=tmp_path,
        session_dir=tmp_path / "sessions" / "ctx-session",
    )
    model_inputs: list[zeta_model_shapes.ModelInput] = []
    responses = iter(
        [
            zeta_model_shapes.ModelOutput(
                message={
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-query-log",
                            "type": "function",
                            "function": {
                                "name": "query_log",
                                "arguments": "{}",
                            },
                        }
                    ],
                }
            ),
            zeta_model_shapes.ModelOutput(message={"content": "history recovered"}),
        ]
    )

    class FakeGateway:
        def available(self, request: zeta_model_shapes.ModelRequest) -> bool:
            del request
            return True

        async def generate(
            self,
            model_input: zeta_model_shapes.ModelInput,
            request: zeta_model_shapes.ModelRequest,
            *,
            stream: zeta_loop_gateway.ModelStream | None = None,
            telemetry_sink: Callable[[dict[str, Any]], None] | None = None,
            should_stop: Callable[[], str | None] | None = None,
        ) -> zeta_model_shapes.ModelOutput:
            del request, stream, telemetry_sink, should_stop
            model_inputs.append(model_input)
            return next(responses)

    result = asyncio.run(
        zeta_agent.run_agent(
            zeta_agent.AgentRunRequest(
                objective="inspect prior work",
                runtime="zeta-test",
                tools=("query_log",),
                context="",
                config=zeta_agent.AgentConfig(max_turns=2),
            ),
            run_id="run-current-2222",
            caused_by="evt-request",
            publish_event=lambda event: None,
            runtime_context=context,
            cancellation_event=None,
            model_gateway=FakeGateway(),
            tool_executor=zeta_capability_executors.local_tool_executor(registry),
        )
    )

    first_tools = model_inputs[0].tools
    assert first_tools is not None
    tool_names = [tool["function"]["name"] for tool in first_tools]
    second_prompt = json.dumps(model_inputs[1].messages)
    assert tool_names == ["query_log"]
    assert "run-prior-1111" in second_prompt
    assert "repair the parser" in second_prompt
    assert "run-current-2222" not in second_prompt
    assert result.final_answer == "history recovered"


def test_zeta_run_agent_queries_the_active_context_budget(tmp_path: Path) -> None:
    registry = CapabilityRegistry()
    register_builtin_tools(registry)
    context = zeta_runtime_context.RuntimeContext(
        session_id="ctx-session",
        event_sink=zeta_events.MemoryEventStore(),
        trace_store=zeta_trace.InMemoryStore(session_id="ctx-session"),
        tool_registry=registry,
        state_dir=tmp_path,
        session_dir=tmp_path / "sessions" / "ctx-session",
    )
    model_inputs: list[zeta_model_shapes.ModelInput] = []
    responses = iter(
        [
            zeta_model_shapes.ModelOutput(
                message={
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-query-context-budget",
                            "type": "function",
                            "function": {
                                "name": "query_context_budget",
                                "arguments": "{}",
                            },
                        }
                    ],
                }
            ),
            zeta_model_shapes.ModelOutput(message={"content": "budget checked"}),
        ]
    )

    class FakeGateway:
        def available(self, request: zeta_model_shapes.ModelRequest) -> bool:
            del request
            return True

        async def generate(
            self,
            model_input: zeta_model_shapes.ModelInput,
            request: zeta_model_shapes.ModelRequest,
            *,
            stream: zeta_loop_gateway.ModelStream | None = None,
            telemetry_sink: Callable[[dict[str, Any]], None] | None = None,
            should_stop: Callable[[], str | None] | None = None,
        ) -> zeta_model_shapes.ModelOutput:
            del request, stream, should_stop
            model_inputs.append(model_input)
            if len(model_inputs) == 1 and telemetry_sink is not None:
                telemetry_sink(
                    {
                        "usage": {"prompt_tokens": 10_000},
                        "model_context_tokens": 32_768,
                    }
                )
            return next(responses)

    class RejectingExecutor:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def call(
            self,
            capability_id: str,
            params: dict[str, Any],
            *,
            base_dir: Path | None,
            effect_key: str | None,
        ) -> dict[str, Any]:
            del params, base_dir, effect_key
            self.calls.append(capability_id)
            raise AssertionError(
                "query_context_budget must not reach the tool executor"
            )

        async def aclose(self) -> None:
            return None

    executor = RejectingExecutor()
    result = asyncio.run(
        zeta_agent.run_agent(
            zeta_agent.AgentRunRequest(
                objective="inspect the active context",
                runtime="zeta-test",
                tools=("query_context_budget",),
                context="",
                config=zeta_agent.AgentConfig(
                    max_turns=2,
                    compaction_policy=CompactionPolicy(
                        strategy="structural_trim",
                        max_context_tokens=15_000,
                    ),
                ),
            ),
            run_id="run-current-2222",
            caused_by="evt-request",
            publish_event=lambda event: None,
            runtime_context=context,
            cancellation_event=None,
            model_gateway=FakeGateway(),
            tool_executor=executor,
        )
    )

    first_tools = model_inputs[0].tools
    assert first_tools is not None
    assert [tool["function"]["name"] for tool in first_tools] == [
        "query_context_budget"
    ]
    tool_message = next(
        message for message in model_inputs[1].messages if message["role"] == "tool"
    )
    assert json.loads(tool_message["content"]) == {
        "ok": True,
        "context_window_tokens": 32_768,
        "prompt_tokens": 10_000,
        "prompt_tokens_source": "provider",
        "reserved_output_tokens": 8_192,
        "remaining_tokens": 14_576,
        "usage_ratio": pytest.approx(10_000 / 24_576),
        "compaction_strategy": "structural_trim",
        "compaction_threshold_tokens": 15_000,
    }
    assert executor.calls == []
    assert result.final_answer == "budget checked"


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
    assert params.tools == ["read", "bash"]
    assert params.context == "existing notes"
    assert params.model == "gpt-test"
    assert params.max_steps == 3
    assert params.max_wall_seconds == 1


def test_zeta_session_turns_use_a_protocol_neutral_runtime_name() -> None:
    params = zeta_requests.SessionRunParams(objective="answer")

    assert params.run_payload("run-1")["runtime"] == "zeta-session"
    assert (
        zeta_requests.session_agent_request({"objective": "answer"}).runtime
        == "zeta-session"
    )


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


def test_zeta_session_run_params_reject_empty_idempotency_key() -> None:
    with pytest.raises(
        zeta_requests.SessionRequestError,
        match="idempotency_key must be a non-empty string",
    ):
        zeta_requests.session_run_params({"objective": "answer", "idempotency_key": ""})


def test_zeta_event_trigger_rule_matches_exact_and_prefix() -> None:
    exact = harness_dispatch.EventPattern("session.turn.requested")
    prefix = harness_dispatch.EventPattern("github.issue.*")
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


def test_zeta_dispatcher_fencing_is_explicit_by_mode(tmp_path: Path) -> None:
    events = RuntimeEventStore.open(tmp_path / "events.sqlite3")

    base = harness_dispatch.QueueingDispatcher(events)
    assert base._queue_claim_is_current("qi_anything") is True

    daemon = harness_dispatch.QueueingDispatcher(
        events,
        events,
        worker_name="worker",
        claim_token="token",
    )
    assert daemon._queue_claim_is_current("qi_anything") is False


def test_zeta_queueing_dispatcher_defers_agent_published_work(
    tmp_path: Path,
) -> None:
    child_calls = 0

    async def run_parent(
        invocation: harness_dispatch.AgentInvocation,
    ) -> dict[str, object]:
        await invocation.publish(
            zeta_events.DraftEvent("child.requested", "agent:parent", {})
        )
        return {"outcome": "parent-complete"}

    async def run_child(
        _invocation: harness_dispatch.AgentInvocation,
    ) -> dict[str, object]:
        nonlocal child_calls
        child_calls += 1
        return {"outcome": "child-complete"}

    executors = (
        harness_dispatch.ExecutableAgent(
            harness_dispatch.AgentDefinition(
                "parent",
                (harness_dispatch.EventPattern("parent.requested"),),
            ),
            run=run_parent,
        ),
        harness_dispatch.ExecutableAgent(
            harness_dispatch.AgentDefinition(
                "child",
                (harness_dispatch.EventPattern("child.requested"),),
            ),
            run=run_child,
        ),
    )
    store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    dispatcher = harness_dispatch.QueueingDispatcher(
        store,
        store,
        executors=executors,
    )

    asyncio.run(
        dispatcher.publish_event(zeta_events.DraftEvent("parent.requested", "test", {}))
    )
    first = asyncio.run(dispatcher.run_next())
    assert first is not None
    _, lifecycle_events = first
    child_event = store.list_events(zeta_events.Filter(event_type="child.requested"))[0]
    child_item = store.queue_item(f"qi_{child_event.id}")

    assert child_calls == 0
    assert lifecycle_events[-1].event_type == "runtime.queue_item.completed"
    assert child_item is not None
    assert child_item["status"] == "pending"

    asyncio.run(
        harness_worker.run_available_queue_item(
            store,
            executors,
            worker_name="test-worker",
        )
    )
    assert child_calls == 1


def test_zeta_retry_policy_computes_backoff_and_classifies_errors() -> None:
    policy = harness_retry.RetryPolicy(
        max_attempts=3,
        backoff_base_seconds=2.0,
        backoff_factor=3.0,
        backoff_max_seconds=10.0,
    )

    assert [policy.delay_seconds(attempt) for attempt in (1, 2, 3)] == [
        2.0,
        6.0,
        10.0,
    ]
    assert policy.deterministic_jitter_seconds("qi_1", spread_seconds=5.0) == (
        policy.deterministic_jitter_seconds("qi_1", spread_seconds=5.0)
    )
    assert policy.classify("agent_spec_invalid") == "permanent"
    assert policy.classify("provider_timeout") == "retryable"


def test_zeta_sqlite_event_store_serializes_threaded_appends(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    append_started = threading.Event()
    release_append = threading.Event()
    first_projection = threading.Event()
    errors: list[BaseException] = []
    original_projection = event_store.events._index_one_runtime_event

    def blocked_first_projection(event: Event) -> None:
        if not first_projection.is_set():
            first_projection.set()
            append_started.set()
            assert release_append.wait(timeout=2.0)
        original_projection(event)

    event_store.events._index_one_runtime_event = blocked_first_projection

    def append_event(event_id: str) -> None:
        try:
            event_store.append(
                Event(
                    id=event_id,
                    event_type="github.issue.opened",
                    source="github",
                    payload={"id": event_id},
                    idempotency_key=None,
                    caused_by=None,
                    session_id=None,
                    run_id=None,
                    turn_id=None,
                    timestamp_ms=1,
                )
            )
        except BaseException as error:
            errors.append(error)

    first = threading.Thread(target=append_event, args=("evt_thread_1",))
    second = threading.Thread(target=append_event, args=("evt_thread_2",))

    first.start()
    assert append_started.wait(timeout=2.0)
    second.start()
    release_append.set()
    first.join(timeout=2.0)
    second.join(timeout=2.0)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert [event.id for event in event_store.list_events(Filter())] == [
        "evt_thread_1",
        "evt_thread_2",
    ]


def test_zeta_sqlite_event_store_lists_newest_events_with_limit(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    for index in range(1, 4):
        event_store.append(
            Event(
                id=f"evt_{index}",
                event_type="github.issue.opened",
                source="github",
                payload={"id": index},
                idempotency_key=None,
                caused_by=None,
                session_id=None,
                run_id=None,
                turn_id=None,
                timestamp_ms=index,
            )
        )

    events = event_store.list_events(Filter(limit=2, newest_first=True))

    assert [event.id for event in events] == ["evt_3", "evt_2"]


def test_zeta_sqlite_event_store_rebuilds_projection_tables(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")

    async def run_agent(run: harness_dispatch.AgentInvocation) -> dict[str, object]:
        return {
            "final_answer": "handled issue",
            "events": [{"type": "issue.triaged", "event": run.triggering_event.id}],
            "tool_calls": [{"name": "read"}],
            "usage": {"input_tokens": 12, "output_tokens": 3},
        }

    dispatcher = harness_dispatch.QueueingDispatcher(
        event_store,
        executors=[
            harness_dispatch.ExecutableAgent(
                harness_dispatch.AgentDefinition(
                    "issue-triage",
                    (harness_dispatch.EventPattern("github.issue.opened"),),
                ),
                run=run_agent,
            )
        ],
    )
    dispatch_event(
        dispatcher,
        zeta_events.DraftEvent(
            "github.issue.opened",
            "github",
            {},
            session_id="repo",
            run_id="run-1",
        ),
    )
    expected_queue_items = event_store.list_queue_items()
    expected_attempts = event_store.list_attempts()
    expected_session_mappings = [
        dict(row)
        for row in event_store.connection.execute(
            """
            SELECT session_id, run_id, updated_at
            FROM session_mappings
            ORDER BY session_id ASC
            """
        ).fetchall()
    ]

    event_store.connection.executescript(
        """
        DELETE FROM attempt_results;
        DELETE FROM attempts;
        DELETE FROM queue_items;
        DELETE FROM session_mappings;
        """
    )
    event_store.connection.commit()

    rebuilt = event_store.rebuild_projections()
    replayed = event_store.rebuild_projections()

    assert rebuilt == len(event_store.list_events(zeta_events.Filter()))
    assert replayed == rebuilt
    assert event_store.list_queue_items() == expected_queue_items
    assert event_store.list_attempts() == expected_attempts
    assert [
        dict(row)
        for row in event_store.connection.execute(
            """
            SELECT session_id, run_id, updated_at
            FROM session_mappings
            ORDER BY session_id ASC
            """
        ).fetchall()
    ] == expected_session_mappings


def test_zeta_sqlite_event_store_rebuild_discards_coordination_state(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    dispatcher = harness_dispatch.QueueingDispatcher(
        event_store,
        routes=[
            harness_dispatch.AgentRoute(
                "issue-triage",
                (harness_dispatch.EventPattern("github.issue.opened"),),
            )
        ],
    )
    accepted = asyncio.run(
        dispatcher.publish_event(
            zeta_events.DraftEvent("github.issue.opened", "github", {})
        )
    ).event
    queue_item_id = f"qi_{accepted.id}"
    now_ms = accepted.timestamp_ms + 1
    claim = event_store.claim_next_queue_item(
        "worker-a",
        lease_ms=60_000,
        now_ms=now_ms,
    )
    assert claim is not None
    event_store.append(
        zeta_events.Event(
            id="queue-claimed",
            event_type="runtime.queue_item.claimed",
            source="zeta",
            payload={
                "queue_item_id": queue_item_id,
                "event_id": accepted.id,
                "target_agent": "issue-triage",
                "status": "claimed",
            },
            idempotency_key=None,
            caused_by=accepted.id,
            session_id=None,
            timestamp_ms=accepted.timestamp_ms + 2,
        )
    )
    assert event_store.acquire_locks(
        ["context:repo"],
        claim.token,
        lease_ms=60_000,
        now_ms=now_ms + 1,
    )

    event_store.rebuild_projections()

    item = event_store.list_queue_items()[0]
    assert item["status"] == "available"
    assert item["claimed_by"] is None
    assert item["claimed_until"] is None
    assert event_store.list_locks() == []


def test_zeta_sqlite_event_store_reconciles_claim_without_lease(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    accepted = event_store.accept(
        zeta_events.DraftEvent("github.issue.opened", "github", {})
    ).event
    event_store.ensure_pending_queue_item(accepted)
    event_store.connection.execute(
        """
        UPDATE queue_items
        SET status = 'claimed', claimed_by = 'old-worker', claimed_until = NULL
        """
    )
    event_store.connection.commit()

    reconciled = event_store.reconcile_expired_queue_claims(
        now_ms=accepted.timestamp_ms + 1
    )

    assert reconciled == 1
    assert event_store.list_queue_items()[0]["status"] == "pending"


def test_zeta_sqlite_event_store_claims_and_reconciles_queue_leases(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    dispatcher = harness_dispatch.QueueingDispatcher(
        event_store,
        routes=[
            harness_dispatch.AgentRoute(
                "issue-triage",
                (harness_dispatch.EventPattern("github.issue.opened"),),
            )
        ],
    )
    accepted = asyncio.run(
        dispatcher.publish_event(
            zeta_events.DraftEvent("github.issue.opened", "github", {})
        )
    ).event
    queue_item_id = f"qi_{accepted.id}"
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

    assert first_claim is not None
    assert first_claim.queue_item_id == queue_item_id
    assert second_claim is None
    assert dict(claimed_row) == {
        "status": "claimed",
        "claimed_by": "worker-a",
        "claimed_until": now_ms + 1_000,
    }
    assert reconciled == 1
    assert reclaimed is not None
    assert reclaimed.queue_item_id == queue_item_id


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
    assert claimed is not None
    assert claimed.queue_item_id == queue_item_id
    assert reconciled == 1
    assert reclaimed is not None
    assert reclaimed.queue_item_id == queue_item_id
    assert [row["queue_item_id"] for row in rows] == [queue_item_id]


def test_zeta_queue_claim_keeps_a_later_session_turn_behind_a_retry(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    first = event_store.accept(DraftEvent("work.first", "test", {})).event
    second = event_store.accept(DraftEvent("work.second", "test", {})).event
    other = event_store.accept(DraftEvent("work.other", "test", {})).event
    now_ms = other.timestamp_ms + 1_000

    def bind(event: Event, agent: str, session: str, not_before: int) -> None:
        event_store.accept(
            DraftEvent(
                "runtime.queue_item.available",
                "zeta",
                {
                    "queue_item_id": f"qi_{event.id}_{agent}",
                    "event_id": event.id,
                    "target_agent": agent,
                    "session_id": session,
                    "status": "available",
                    "not_before": not_before,
                },
                session_id=session,
            )
        )

    bind(first, "agent-a", "session-a", now_ms + 10_000)
    bind(second, "agent-a", "session-a", now_ms)
    bind(other, "agent-b", "session-b", now_ms)
    event_store.rebuild_projections()

    other_claim = event_store.claim_next_queue_item(
        "worker-a", lease_ms=1_000, now_ms=now_ms
    )
    blocked_claim = event_store.claim_next_queue_item(
        "worker-b", lease_ms=1_000, now_ms=now_ms
    )
    event_store.accept(
        DraftEvent(
            "runtime.queue_item.dead_lettered",
            "zeta",
            {
                "queue_item_id": f"qi_{first.id}_agent-a",
                "event_id": first.id,
                "target_agent": "agent-a",
                "session_id": "session-a",
                "status": "dead_lettered",
            },
            session_id="session-a",
        )
    )
    released_claim = event_store.claim_next_queue_item(
        "worker-b", lease_ms=1_000, now_ms=now_ms
    )

    assert other_claim is not None
    assert other_claim.queue_item_id == f"qi_{other.id}_agent-b"
    assert blocked_claim is None
    assert released_claim is not None
    assert released_claim.queue_item_id == f"qi_{second.id}_agent-a"


def test_zeta_queue_claim_runs_only_one_turn_per_session(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    first = event_store.accept(DraftEvent("work.first", "test", {})).event
    second = event_store.accept(DraftEvent("work.second", "test", {})).event
    other = event_store.accept(DraftEvent("work.other", "test", {})).event

    for event, agent, session in (
        (first, "agent-a", "session-a"),
        (second, "agent-a", "session-a"),
        (other, "agent-b", "session-b"),
    ):
        event_store.accept(
            DraftEvent(
                "runtime.queue_item.available",
                "zeta",
                {
                    "queue_item_id": f"qi_{event.id}_{agent}",
                    "event_id": event.id,
                    "target_agent": agent,
                    "session_id": session,
                    "status": "available",
                },
                session_id=session,
            )
        )
    now_ms = other.timestamp_ms + 1_000

    first_claim = event_store.claim_next_queue_item(
        "worker-a", lease_ms=1_000, now_ms=now_ms
    )
    concurrent_claim = event_store.claim_next_queue_item(
        "worker-b", lease_ms=1_000, now_ms=now_ms
    )

    assert first_claim is not None
    assert first_claim.queue_item_id == f"qi_{first.id}_agent-a"
    assert concurrent_claim is not None
    assert concurrent_claim.queue_item_id == f"qi_{other.id}_agent-b"


def test_zeta_queue_claim_does_not_pass_an_earlier_unbound_event(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    earlier = event_store.accept(DraftEvent("work.earlier", "test", {})).event
    later = event_store.accept(DraftEvent("work.later", "test", {})).event
    event_store.accept(
        DraftEvent(
            "runtime.queue_item.available",
            "zeta",
            {
                "queue_item_id": f"qi_{later.id}_agent-a",
                "event_id": later.id,
                "target_agent": "agent-a",
                "session_id": "session-a",
                "status": "available",
            },
            session_id="session-a",
        )
    )
    now_ms = later.timestamp_ms + 1_000

    first_claim = event_store.claim_next_queue_item(
        "worker-a", lease_ms=1_000, now_ms=now_ms
    )
    second_claim = event_store.claim_next_queue_item(
        "worker-b", lease_ms=1_000, now_ms=now_ms
    )

    assert first_claim is not None
    assert first_claim.queue_item_id == f"qi_{earlier.id}"
    assert second_claim is None


def test_zeta_sqlite_event_append_projects_pending_work_transactionally(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")

    accepted = event_store.accept(
        zeta_events.DraftEvent("github.issue.opened", "github", {})
    ).event

    assert event_store.list_queue_items() == [
        {
            "queue_item_id": f"qi_{accepted.id}",
            "event_id": accepted.id,
            "target_agent": "",
            "input_cursor": accepted.cursor,
            "status": "pending",
            "available_at": accepted.timestamp_ms,
            "claimed_by": None,
            "claimed_until": None,
            "attempt_count": 0,
            "last_error": None,
            "updated_at": accepted.timestamp_ms,
        }
    ]


def test_zeta_projection_rebuild_restores_unrouted_pending_work(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    accepted = event_store.accept(
        zeta_events.DraftEvent("github.issue.opened", "github", {})
    ).event
    event_store.connection.execute("DELETE FROM queue_items")
    event_store.connection.commit()

    event_store.rebuild_projections()

    assert event_store.list_queue_items()[0]["queue_item_id"] == f"qi_{accepted.id}"
    assert event_store.list_queue_items()[0]["status"] == "pending"


def test_zeta_sqlite_event_store_rejects_stale_queue_claim_tokens(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    accepted = event_store.accept(
        zeta_events.DraftEvent("github.issue.opened", "github", {})
    ).event
    queue_item_id = event_store.ensure_pending_queue_item(accepted)
    now_ms = accepted.timestamp_ms + 1_000

    first_claim = event_store.claim_next_queue_item(
        "worker",
        lease_ms=1_000,
        now_ms=now_ms,
    )
    event_store.reconcile_expired_queue_claims(now_ms=now_ms + 1_001)
    second_claim = event_store.claim_next_queue_item(
        "worker",
        lease_ms=1_000,
        now_ms=now_ms + 1_001,
    )

    assert first_claim is not None
    assert second_claim is not None
    assert first_claim.queue_item_id == queue_item_id
    assert second_claim.queue_item_id == queue_item_id
    assert first_claim.token != second_claim.token
    assert (
        event_store.release_queue_claim(
            queue_item_id,
            "worker",
            claim_token=first_claim.token,
            now_ms=now_ms + 1_002,
        )
        is False
    )
    assert event_store.list_queue_items()[0]["status"] == "claimed"
    assert (
        event_store.release_queue_claim(
            queue_item_id,
            "worker",
            claim_token=second_claim.token,
            now_ms=now_ms + 1_003,
        )
        is True
    )


def test_zeta_sqlite_event_store_rejects_stale_attempt_heartbeats(
    tmp_path: Path,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    accepted = event_store.accept(
        zeta_events.DraftEvent("github.issue.opened", "github", {})
    ).event
    queue_item_id = event_store.ensure_pending_queue_item(accepted)
    now_ms = accepted.timestamp_ms + 1_000
    first_claim = event_store.claim_next_queue_item(
        "worker",
        lease_ms=1_000,
        now_ms=now_ms,
    )
    assert first_claim is not None
    event_store.append(
        zeta_events.Event(
            id="attempt-started",
            event_type="runtime.attempt.started",
            source="zeta",
            payload={
                "attempt_id": f"att_{queue_item_id}_1",
                "queue_item_id": queue_item_id,
                "event_id": accepted.id,
                "attempt_number": 1,
                "target_agent": "issue-triage",
                "status": "running",
                "started_at": "2026-06-20T10:00:01Z",
                "worker_name": "worker",
            },
            idempotency_key=None,
            caused_by=accepted.id,
            session_id=None,
            timestamp_ms=now_ms + 1,
        )
    )
    event_store.reconcile_expired_queue_claims(now_ms=now_ms + 1_001)
    second_claim = event_store.claim_next_queue_item(
        "worker",
        lease_ms=1_000,
        now_ms=now_ms + 1_001,
    )

    assert second_claim is not None
    assert (
        event_store.heartbeat_attempt(
            f"att_{queue_item_id}_1",
            queue_item_id,
            "worker",
            claim_token=first_claim.token,
            lease_ms=1_000,
            now_ms=now_ms + 1_002,
        )
        is False
    )
    assert event_store.list_attempts()[0]["heartbeat_at"] == now_ms + 1
    assert event_store.list_queue_items()[0]["claimed_until"] == now_ms + 2_001


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


def test_zeta_sqlite_event_store_renews_locks(tmp_path: Path) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    assert event_store.acquire_locks(
        ["context:repo"],
        "claim-token",
        lease_ms=1_000,
        now_ms=10_000,
    )

    assert event_store.renew_locks(
        ["context:repo"],
        "claim-token",
        lease_ms=1_000,
        now_ms=10_500,
    )
    assert event_store.list_locks()[0]["expires_at"] == 11_500
    assert (
        event_store.renew_locks(
            ["context:repo"],
            "other-token",
            lease_ms=1_000,
            now_ms=10_600,
        )
        is False
    )
    assert (
        event_store.renew_locks(
            ["context:repo"],
            "claim-token",
            lease_ms=1_000,
            now_ms=11_501,
        )
        is False
    )


def test_zeta_cli_ps_replaces_runs_listing(tmp_path: Path) -> None:
    listing = CliRunner().invoke(
        cli_main.cli,
        ["ps", "--state-dir", str(tmp_path / ".zeta")],
    )
    removed_alias = CliRunner().invoke(cli_main.cli, ["runs"])

    assert listing.exit_code == 0
    assert listing.output == "runs empty\n"
    assert removed_alias.exit_code == 2
    assert "No such command 'runs'" in removed_alias.output


def test_zeta_cli_inspects_diffs_and_restores_agent_content(tmp_path: Path) -> None:
    state_dir = tmp_path / ".zeta"
    runtime = RuntimeEventStore.open(event_store_path(state_dir))
    store = runtime.content_store()
    workspace = zeta_content_transforms.ContentWorkspace(
        store,
        run_id="run-content",
        session_id="session-content",
        owner="writer",
    )
    first_change = workspace.transform(
        {
            "expected_head": workspace.initialize(),
            "reason": "Keep the first procedure.",
            "inputs": {},
            "transformation": {"type": "literal", "value": "First."},
            "destination": {
                "key": "procedure",
                "kind": "procedure",
                "scope": "agent",
                "expected_object_id": None,
            },
        }
    )
    first = workspace.promote(first_change.promotions[0])
    second_change = workspace.transform(
        {
            "expected_head": first_change.head,
            "reason": "Replace the procedure.",
            "inputs": {"keys": ["procedure"]},
            "transformation": {
                "type": "patch",
                "patch": {"content": "Second."},
            },
            "destination": {
                "key": "procedure",
                "kind": "procedure",
                "scope": "agent",
                "expected_object_id": first_change.output_ids[0],
            },
        }
    )
    second = workspace.promote(second_change.promotions[0])
    runtime.close()

    runner = CliRunner()
    shown = runner.invoke(
        cli_main.cli,
        [
            "agents",
            "content",
            "show",
            "writer",
            "--state-dir",
            str(state_dir),
            "--json",
        ],
    )
    logged = runner.invoke(
        cli_main.cli,
        [
            "agents",
            "content",
            "log",
            "writer",
            "--state-dir",
            str(state_dir),
            "--json",
        ],
    )
    diffed = runner.invoke(
        cli_main.cli,
        [
            "agents",
            "content",
            "diff",
            "writer",
            first,
            second,
            "--state-dir",
            str(state_dir),
            "--json",
        ],
    )
    restored = runner.invoke(
        cli_main.cli,
        [
            "agents",
            "content",
            "restore",
            "writer",
            first,
            "--state-dir",
            str(state_dir),
            "--reason",
            "The second procedure was wrong.",
            "--json",
        ],
    )
    redone = runner.invoke(
        cli_main.cli,
        [
            "agents",
            "content",
            "restore",
            "writer",
            second,
            "--state-dir",
            str(state_dir),
            "--reason",
            "Return to the newer revision.",
            "--json",
        ],
    )

    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output) == {
        "agent": "writer",
        "head": second,
        "nodes": [
            {
                "key": "procedure",
                "kind": "procedure",
                "title": None,
                "object_id": second_change.output_ids[0],
                "source_scope": "agent",
                "chars": 7,
                "preview": "Second.",
            }
        ],
    }
    assert logged.exit_code == 0, logged.output
    assert [item["head"] for item in json.loads(logged.output)] == [
        second,
        first,
    ]
    assert diffed.exit_code == 0, diffed.output
    assert json.loads(diffed.output) == {
        "agent": "writer",
        "old_head": first,
        "new_head": second,
        "added": [],
        "removed": [],
        "changed": [
            {
                "key": "procedure",
                "old_object_id": first_change.output_ids[0],
                "new_object_id": second_change.output_ids[0],
            }
        ],
    }
    assert restored.exit_code == 0, restored.output
    assert json.loads(restored.output) == {
        "agent": "writer",
        "old_head": second,
        "head": first,
        "reason": "The second procedure was wrong.",
    }
    assert redone.exit_code == 0, redone.output
    assert json.loads(redone.output) == {
        "agent": "writer",
        "old_head": first,
        "head": second,
        "reason": "Return to the newer revision.",
    }
    reopened = RuntimeEventStore.open(event_store_path(state_dir), read_only=True)
    restored_ref = reopened.content_store().get_ref("agent/writer/content/head")
    assert restored_ref is not None
    assert restored_ref.object_id == second
    assert reopened.content_store().get_object(second) is not None
    reopened.close()


def test_zeta_cli_lists_disables_and_restores_agent_tools(tmp_path: Path) -> None:
    state_dir = tmp_path / ".zeta"
    runtime = RuntimeEventStore.open(event_store_path(state_dir))
    store = runtime.content_store()
    head = zeta_content_transforms.ContentHead("agent", "writer", "writer")
    with store.batch():
        first_tool = zeta_content_transforms.put_content_node(
            store,
            zeta_content_transforms.ContentNode(
                key="tools/echo",
                kind="tool_definition",
                content={
                    "name": "echo",
                    "capability_id": "agent.writer.echo",
                    "source": "first source",
                },
            ),
        )
        first_head = zeta_content_transforms.advance_content_head(
            store,
            head,
            expected_head=None,
            nodes={"tools/echo": first_tool},
            projection_order=("tools/echo",),
            source_scopes={"tools/echo": "agent"},
            reason="Create the first tool.",
        )
        second_tool = zeta_content_transforms.put_content_node(
            store,
            zeta_content_transforms.ContentNode(
                key="tools/echo",
                kind="tool_definition",
                content={
                    "name": "echo",
                    "capability_id": "agent.writer.echo",
                    "source": "second source",
                },
            ),
        )
        second_head = zeta_content_transforms.advance_content_head(
            store,
            head,
            expected_head=first_head,
            nodes={"tools/echo": second_tool},
            projection_order=("tools/echo",),
            source_scopes={"tools/echo": "agent"},
            reason="Replace the tool.",
            source_ids=(first_tool,),
        )
    active_ref = store.get_ref("agent/writer/content/head")
    assert active_ref is not None
    assert active_ref.object_id == second_head
    runtime.close()
    persisted = RuntimeEventStore.open(event_store_path(state_dir), read_only=True)
    persisted_ref = persisted.content_store().get_ref("agent/writer/content/head")
    assert persisted_ref is not None
    assert persisted_ref.object_id == second_head
    persisted.close()

    runner = CliRunner()
    listed = runner.invoke(
        cli_main.cli,
        [
            "agents",
            "tools",
            "list",
            "writer",
            "--state-dir",
            str(state_dir),
            "--json",
        ],
    )
    shown = runner.invoke(
        cli_main.cli,
        [
            "agents",
            "tools",
            "show",
            "writer",
            "echo",
            "--state-dir",
            str(state_dir),
            "--json",
        ],
    )
    disabled = runner.invoke(
        cli_main.cli,
        [
            "agents",
            "tools",
            "disable",
            "writer",
            "echo",
            "--state-dir",
            str(state_dir),
            "--reason",
            "This version is broken.",
            "--json",
        ],
    )
    restored = runner.invoke(
        cli_main.cli,
        [
            "agents",
            "tools",
            "restore",
            "writer",
            "echo",
            first_tool,
            "--state-dir",
            str(state_dir),
            "--reason",
            "Use the first working version.",
            "--json",
        ],
    )

    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.output) == [
        {
            "key": "tools/echo",
            "name": "echo",
            "capability_id": "agent.writer.echo",
            "object_id": second_tool,
        }
    ]
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output)["source"] == "second source"
    assert disabled.exit_code == 0, disabled.output
    disabled_result = json.loads(disabled.output)
    assert disabled_result["old_head"] == second_head
    assert disabled_result["disabled_object_id"] == second_tool
    assert restored.exit_code == 0, restored.output
    restored_result = json.loads(restored.output)
    assert restored_result["old_head"] == disabled_result["head"]
    assert restored_result["object_id"] == first_tool
    reopened = RuntimeEventStore.open(event_store_path(state_dir), read_only=True)
    active = reopened.content_store().get_ref("agent/writer/content/head")
    assert active is not None
    revision = zeta_content_transforms.content_revision_from_object(
        reopened.content_store().get_object(active.object_id)
    )
    assert revision.nodes == {"tools/echo": first_tool}
    assert reopened.content_store().get_object(second_tool) is not None
    reopened.close()


def test_zeta_cli_ps_lists_and_shows_runs(tmp_path: Path) -> None:
    state_dir = tmp_path / ".zeta"
    event_store = zeta_events.SqliteEventStore(event_store_path(state_dir))
    trigger = event_store.accept(
        zeta_events.DraftEvent(
            "github.issue.opened",
            "github",
            {},
            session_id="repo",
        )
    ).event
    queue_item_id = event_store.ensure_pending_queue_item(trigger)
    event_store.accept(
        zeta_events.DraftEvent(
            "runtime.attempt.started",
            "zeta",
            {
                "attempt_id": "att_demo",
                "queue_item_id": queue_item_id,
                "event_id": trigger.id,
                "attempt_number": 1,
                "target_agent": "issue-triage",
                "status": "running",
                "started_at": "2026-07-29T12:00:00Z",
                "session_id": "repo",
                "run_id": "run_demo",
            },
            caused_by=trigger.id,
            session_id="repo",
            run_id="run_demo",
        )
    )
    event_store.close()

    listing = CliRunner().invoke(
        cli_main.cli,
        ["ps", "--state-dir", str(state_dir), "--json"],
    )
    detail = CliRunner().invoke(
        cli_main.cli,
        ["ps", "run_demo", "--state-dir", str(state_dir), "--json"],
    )
    text_detail = CliRunner().invoke(
        cli_main.cli,
        ["ps", "run_demo", "--state-dir", str(state_dir)],
    )

    assert listing.exit_code == 0
    assert json.loads(listing.output) == [
        {
            "run_id": "run_demo",
            "attempt_id": "att_demo",
            "queue_item_id": queue_item_id,
            "event_id": trigger.id,
            "trigger_event_type": "github.issue.opened",
            "target_agent": "issue-triage",
            "status": "running",
            "session_id": "repo",
            "started_at": "2026-07-29T12:00:00Z",
            "finished_at": None,
            "summary": None,
            "error": None,
            "input_tokens": None,
            "output_tokens": None,
        }
    ]
    assert detail.exit_code == 0
    assert json.loads(detail.output)["run"]["run_id"] == "run_demo"
    assert json.loads(detail.output)["trigger_event"]["id"] == trigger.id
    assert text_detail.exit_code == 0
    assert text_detail.output == (
        "run: run_demo\n"
        "status: running\n"
        "agent: issue-triage\n"
        f"trigger: github.issue.opened {trigger.id}\n"
        "session: repo\n"
        "started: 2026-07-29T12:00:00Z\n"
        "finished: -\n"
    )


def test_zeta_cli_ps_reports_unknown_run(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli_main.cli,
        [
            "ps",
            "run_missing",
            "--state-dir",
            str(tmp_path / ".zeta"),
        ],
    )

    assert result.exit_code == 1
    assert "run not found: run_missing" in result.output


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
        cli_main.cli,
        ["events", "list", "--state-dir", str(state_dir), "--json"],
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


def test_zeta_cli_events_lists_and_cancels_scheduled_events(tmp_path: Path) -> None:
    state_dir = tmp_path / ".zeta"
    event_store = zeta_events.SqliteEventStore(event_store_path(state_dir))
    event_store.accept(
        DraftEvent(
            "runtime.scheduled_event.created",
            "zeta",
            {
                "handle": "publication-1",
                "event_type": "report.ready",
                "payload": {"report_id": "report-1"},
                "publish_at": "2030-01-02T03:04:05+00:00",
                "source_agent_id": "reporter",
                "source_session_id": "session-1",
                "source_queue_item_id": "qi-report",
                "position": 0,
            },
            idempotency_key="agent.schedule:qi-report:0",
        )
    )
    event_store.close()

    listed = CliRunner().invoke(
        cli_main.cli,
        ["events", "scheduled", "--state-dir", str(state_dir), "--json"],
    )
    text_listed = CliRunner().invoke(
        cli_main.cli,
        ["events", "scheduled", "--state-dir", str(state_dir)],
    )
    cancelled = CliRunner().invoke(
        cli_main.cli,
        [
            "events",
            "cancel-scheduled",
            "publication-1",
            "--state-dir",
            str(state_dir),
        ],
    )
    repeated = CliRunner().invoke(
        cli_main.cli,
        [
            "events",
            "cancel-scheduled",
            "publication-1",
            "--state-dir",
            str(state_dir),
        ],
    )
    unknown = CliRunner().invoke(
        cli_main.cli,
        [
            "events",
            "cancel-scheduled",
            "missing",
            "--state-dir",
            str(state_dir),
        ],
    )

    assert listed.exit_code == 0
    rows = json.loads(listed.output)
    assert rows[0]["handle"] == "publication-1"
    assert rows[0]["event_type"] == "report.ready"
    assert rows[0]["payload"] == {"report_id": "report-1"}
    assert rows[0]["status"] == "pending"
    assert text_listed.exit_code == 0
    assert text_listed.output.startswith("pending\tpublication-1\treport.ready\t")
    assert cancelled.exit_code == 0
    assert cancelled.output == "cancelled publication-1\n"
    assert repeated.exit_code == 1
    assert "scheduled event is already cancelled: publication-1" in repeated.output
    assert unknown.exit_code == 1
    assert "scheduled event not found: missing" in unknown.output


def test_zeta_cli_cancel_handles_any_supported_resource(tmp_path: Path) -> None:
    state_dir = tmp_path / ".zeta"
    handle = "wait_0123456789abcdef01234567"
    event_store = zeta_events.SqliteEventStore(event_store_path(state_dir))
    event_store.accept(
        DraftEvent(
            "runtime.wait.created",
            "zeta",
            {
                "handle": handle,
                "agent_id": "issue-agent",
                "session_id": "session-1",
                "event_type": "github.issue.updated",
                "fields": {"number": 7},
                "deadline": None,
                "source_queue_item_id": "qi-source",
                "project_generation": "generation-1",
            },
            idempotency_key="agent.wait:qi-source:0",
            session_id="session-1",
        )
    )
    queued = submit_session_message(
        event_store,
        message="Cancel from the CLI",
        agent_id="zeta.master",
        session_id="session-2",
        project_generation="generation-1",
    )
    event_store.close()

    cancelled = CliRunner().invoke(
        cli_main.cli,
        [
            "cancel",
            handle,
            "--reason",
            "Issue closed",
            "--state-dir",
            str(state_dir),
            "--json",
        ],
    )
    repeated = CliRunner().invoke(
        cli_main.cli,
        ["cancel", handle, "--state-dir", str(state_dir), "--json"],
    )
    unknown = CliRunner().invoke(
        cli_main.cli,
        [
            "cancel",
            "pub_999999999999999999999999",
            "--state-dir",
            str(state_dir),
        ],
    )
    invalid = CliRunner().invoke(
        cli_main.cli,
        ["cancel", "qi-1", "--state-dir", str(state_dir)],
    )
    cancelled_run = CliRunner().invoke(
        cli_main.cli,
        ["cancel", queued["run_id"], "--state-dir", str(state_dir), "--json"],
    )

    assert cancelled.exit_code == 0
    assert json.loads(cancelled.output) == {
        "handle": handle,
        "resource_type": "wait",
        "status": "cancelled",
        "changed": True,
    }
    assert repeated.exit_code == 0
    assert json.loads(repeated.output) == {
        "handle": handle,
        "resource_type": "wait",
        "status": "cancelled",
        "changed": False,
    }
    assert unknown.exit_code == 1
    assert "unknown cancellation handle" in unknown.output
    assert invalid.exit_code == 1
    assert "handle must start with 'wait_' or 'pub_'" in invalid.output
    assert cancelled_run.exit_code == 0
    assert json.loads(cancelled_run.output) == {
        "run_id": queued["run_id"],
        "queue_item_id": queued["queue_item_id"],
        "session_id": "session-2",
        "status": "cancelled",
        "terminal_status": "cancelled",
        "changed": True,
    }

    store = RuntimeEventStore.open(event_store_path(state_dir), read_only=True)
    facts = store.list_events(Filter(event_type="runtime.wait.cancelled"))
    assert facts[0].payload["reason"] == "Issue closed"
    store.close()


def test_zeta_cli_waits_lists_active_waits(tmp_path: Path) -> None:
    state_dir = tmp_path / ".zeta"
    event_store = zeta_events.SqliteEventStore(event_store_path(state_dir))
    event_store.accept(
        DraftEvent(
            "runtime.wait.created",
            "zeta",
            {
                "handle": "wait-1",
                "agent_id": "issue-agent",
                "session_id": "session-1",
                "event_type": "github.issue.updated",
                "fields": {"number": 7},
                "deadline": None,
                "source_queue_item_id": "qi-source",
                "project_generation": "generation-1",
            },
            idempotency_key="agent.wait:qi-source:0",
            session_id="session-1",
        )
    )
    event_store.close()

    listed = CliRunner().invoke(
        cli_main.cli,
        ["waits", "list", "--state-dir", str(state_dir), "--json"],
    )
    text_listed = CliRunner().invoke(
        cli_main.cli,
        ["waits", "list", "--state-dir", str(state_dir)],
    )

    assert listed.exit_code == 0
    rows = json.loads(listed.output)
    assert rows[0]["handle"] == "wait-1"
    assert rows[0]["agent_id"] == "issue-agent"
    assert rows[0]["event_type"] == "github.issue.updated"
    assert rows[0]["fields"] == {"number": 7}
    assert rows[0]["status"] == "active"
    assert text_listed.exit_code == 0
    assert text_listed.output == (
        "active\twait-1\tissue-agent\tgithub.issue.updated\t-\n"
    )


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
        cli_main.cli,
        [
            "events",
            "list",
            "--state-dir",
            str(state_dir),
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


def test_zeta_cli_events_discovers_parent_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    nested = project / "src" / "package"
    nested.mkdir(parents=True)
    event_store = zeta_events.SqliteEventStore(event_store_path(project / ".zeta"))
    event = event_store.accept(
        zeta_events.DraftEvent("project.discovered", "test", {})
    ).event
    event_store.close()
    monkeypatch.delenv("ZETA_STATE_DIR", raising=False)
    monkeypatch.chdir(nested)

    result = CliRunner().invoke(cli_main.cli, ["events", "list", "--json"])

    assert result.exit_code == 0
    assert [row["id"] for row in json.loads(result.output)] == [event.id]


def test_zeta_cli_events_parent_state_dir_overrides_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    explicit_state = tmp_path / "explicit"
    explicit_store = zeta_events.SqliteEventStore(event_store_path(explicit_state))
    root = explicit_store.accept(
        zeta_events.DraftEvent("issue.opened", "github", {})
    ).event
    child = explicit_store.accept(
        zeta_events.DraftEvent("issue.closed", "github", {}, caused_by=root.id)
    ).event
    explicit_store.close()
    monkeypatch.setenv("ZETA_STATE_DIR", str(tmp_path / "environment"))

    result = CliRunner().invoke(
        cli_main.cli,
        ["events", "chain", child.id, "--state-dir", str(explicit_state)],
    )

    assert result.exit_code == 0
    assert root.id in result.output
    assert child.id in result.output


def test_zeta_cli_event_relationship_leaves_use_explicit_state(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / ".zeta"
    event_store = zeta_events.SqliteEventStore(event_store_path(state_dir))
    root = event_store.accept(
        zeta_events.DraftEvent(
            "issue.opened",
            "github",
            {},
            turn_id="turn_demo",
        )
    ).event
    child = event_store.accept(
        zeta_events.DraftEvent(
            "issue.triaged",
            "agent:triage",
            {},
            caused_by=root.id,
            turn_id="turn_demo",
        )
    ).event
    grandchild = event_store.accept(
        zeta_events.DraftEvent(
            "issue.labeled",
            "agent:triage",
            {},
            caused_by=child.id,
        )
    ).event
    event_store.close()

    runner = CliRunner()
    selected_root = runner.invoke(
        cli_main.cli,
        ["events", "root", child.id, "--state-dir", str(state_dir), "--json"],
    )
    descendants = runner.invoke(
        cli_main.cli,
        ["events", "descendants", root.id, "--state-dir", str(state_dir), "--json"],
    )
    turn = runner.invoke(
        cli_main.cli,
        ["events", "turn", "turn_demo", "--state-dir", str(state_dir), "--json"],
    )

    assert selected_root.exit_code == 0
    assert json.loads(selected_root.output)["id"] == root.id
    assert descendants.exit_code == 0
    assert [row["id"] for row in json.loads(descendants.output)] == [
        child.id,
        grandchild.id,
    ]
    assert turn.exit_code == 0
    assert [row["id"] for row in json.loads(turn.output)] == [root.id, child.id]


@pytest.mark.parametrize(
    ("command", "expected_exit_code", "expected_output"),
    [
        (["events", "chain", "missing"], 0, "event not found: missing"),
        (["ps", "missing"], 1, "run not found: missing"),
        (["traces", "log"], 0, "no trace objects recorded"),
        (
            ["schedules", "status"],
            0,
            "schedules empty",
        ),
    ],
)
def test_zeta_cli_nested_state_dir_works_after_subcommand(
    command: list[str],
    expected_exit_code: int,
    expected_output: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    (project / "agents").mkdir(parents=True)
    environment_state = tmp_path / "environment-state"
    environment_state.write_text("not a directory", encoding="utf-8")
    explicit_state = tmp_path / "explicit-state"
    monkeypatch.setenv("ZETA_STATE_DIR", str(environment_state))

    if command[:2] == ["schedules", "status"]:
        command = [*command, "--project-root", str(project)]
    result = CliRunner().invoke(
        cli_main.cli,
        [*command, "--state-dir", str(explicit_state)],
    )

    assert result.exit_code == expected_exit_code
    assert expected_output in result.output
    assert not explicit_state.exists()


@pytest.mark.parametrize(
    "command",
    [
        ["events", "publish", "--help"],
        ["events", "chain", "--help"],
        ["events", "root", "--help"],
        ["events", "descendants", "--help"],
        ["events", "turn", "--help"],
        ["events", "list", "--help"],
        ["queue", "list", "--help"],
        ["queue", "status", "--help"],
        ["attempts", "list", "--help"],
        ["sessions", "start", "--help"],
        ["sessions", "send", "--help"],
        ["sessions", "status", "--help"],
        ["sessions", "list", "--help"],
        ["ps", "--help"],
        ["schedules", "status", "--help"],
        ["traces", "log", "--help"],
        ["traces", "reinit-store", "--help"],
        ["traces", "tools", "--help"],
        ["traces", "grep", "--help"],
        ["traces", "show", "--help"],
        ["traces", "closure", "--help"],
        ["traces", "tree", "--help"],
        ["traces", "diff", "--help"],
        ["traces", "replay", "--help"],
        ["traces", "refs", "--help"],
        ["traces", "prompts", "--help"],
    ],
)
def test_zeta_cli_stateful_leaves_accept_state_dir(
    command: list[str],
) -> None:
    result = CliRunner().invoke(cli_main.cli, command)

    assert result.exit_code == 0
    assert "--state-dir" in result.output
    if command[0] == "traces" and command[1] != "reinit-store":
        assert "--session" in result.output


@pytest.mark.parametrize(
    "namespace",
    ["queue", "attempts", "events", "sessions", "schedules", "traces"],
)
def test_zeta_cli_resource_groups_reject_state_dir(
    namespace: str,
    tmp_path: Path,
) -> None:
    result = CliRunner().invoke(
        cli_main.cli,
        [
            namespace,
            "--state-dir",
            str(tmp_path / "parent"),
        ],
    )

    assert result.exit_code == 2
    assert "No such option '--state-dir'" in result.output


@pytest.mark.parametrize(
    ("namespace", "leaves"),
    [
        ("queue", ("list", "status")),
        ("attempts", ("list",)),
        ("sessions", ("start", "send", "status", "list")),
        (
            "events",
            ("list", "publish", "chain", "root", "descendants", "turn"),
        ),
        ("schedules", ("status",)),
        ("agents", ("new",)),
        ("models", ("list", "show")),
        (
            "traces",
            (
                "log",
                "reinit-store",
                "tools",
                "grep",
                "show",
                "closure",
                "tree",
                "diff",
                "replay",
                "refs",
                "prompts",
            ),
        ),
        ("ipc", ("stdio",)),
    ],
)
def test_zeta_cli_resource_namespaces_only_show_help(
    namespace: str,
    leaves: tuple[str, ...],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / ".zeta"
    monkeypatch.setenv("ZETA_STATE_DIR", str(state_dir))

    result = CliRunner().invoke(cli_main.cli, [namespace])

    assert result.exit_code == 2
    assert result.output.startswith(f"Usage: cli {namespace}")
    for leaf in leaves:
        assert leaf in result.output
    assert not state_dir.exists()


@pytest.mark.parametrize(
    "command",
    [
        ["status", "--help"],
        ["schedule", "status"],
        ["agent", "--help"],
        ["model", "--help"],
        ["run", "show", "run_missing"],
        ["rpc", "stdio"],
        ["schedules", "run"],
        ["schedules", "--once"],
    ],
)
def test_zeta_cli_removed_spellings_fail(
    command: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdio_calls = 0

    def run_stdio(_input: object, _output: object) -> None:
        nonlocal stdio_calls
        stdio_calls += 1

    monkeypatch.setattr(cli_ipc, "run_stdio", run_stdio)

    result = CliRunner().invoke(cli_main.cli, command)

    assert result.exit_code == 2
    assert stdio_calls == 0


def test_zeta_cli_ipc_stdio_runs_the_stdio_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[object, object]] = []

    def run_stdio(input_stream: object, output_stream: object) -> None:
        captured.append((input_stream, output_stream))

    monkeypatch.setattr(cli_ipc, "run_stdio", run_stdio)

    result = CliRunner().invoke(cli_main.cli, ["ipc", "stdio"])

    assert result.exit_code == 0
    assert len(captured) == 1


def test_zeta_cli_sessions_detach_and_keep_rpc_status_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    (project_root / "agents").mkdir(parents=True)
    state_dir = tmp_path / "state"
    runner = CliRunner()

    started = runner.invoke(
        cli_main.cli,
        [
            "sessions",
            "start",
            "Plan the release.",
            "--project-root",
            str(project_root),
            "--state-dir",
            str(state_dir),
            "--idempotency-key",
            "start-1",
            "--json",
        ],
    )
    assert started.exit_code == 0
    start_result = json.loads(started.output)
    session_id = start_result["session_id"]

    sent = runner.invoke(
        cli_main.cli,
        [
            "sessions",
            "send",
            session_id,
            "Include the migration.",
            "--project-root",
            str(project_root),
            "--state-dir",
            str(state_dir),
            "--idempotency-key",
            "send-1",
            "--json",
        ],
    )
    status = runner.invoke(
        cli_main.cli,
        ["sessions", "status", session_id, "--state-dir", str(state_dir), "--json"],
    )
    listed = runner.invoke(
        cli_main.cli,
        ["sessions", "list", "--state-dir", str(state_dir), "--json"],
    )

    assert sent.exit_code == 0
    assert status.exit_code == 0
    assert listed.exit_code == 0
    queued_status = json.loads(status.output)
    assert start_result["status"] == "queued"
    assert json.loads(sent.output)["status"] == "queued"
    assert queued_status["queued_turns"] == 2
    assert json.loads(listed.output) == [queued_status]

    objectives: list[str] = []

    async def fake_run_agent(request: Any, **_kwargs: Any) -> AgentRunResult:
        objectives.append(request.objective)
        return AgentRunResult(final_answer="done")

    monkeypatch.setattr(harness_worker, "run_agent", fake_run_agent)
    registry = CapabilityRegistry()
    register_builtin_tools(registry)
    runtime = harness_worker.build_worker_services(
        project_root=project_root,
        state_dir=state_dir,
        tool_registry=registry,
    )
    with asyncio.Runner() as async_runner:
        try:
            async_runner.run(harness_worker.run_until_idle(runtime))
        finally:
            async_runner.run(runtime.aclose())

    completed = runner.invoke(
        cli_main.cli,
        ["sessions", "status", session_id, "--state-dir", str(state_dir), "--json"],
    )

    assert objectives == ["Plan the release.", "Include the migration."]
    assert completed.exit_code == 0
    completed_status = json.loads(completed.output)
    assert completed_status["status"] == "idle"
    assert completed_status["queued_turns"] == 0


@pytest.mark.parametrize(
    "command",
    [
        ["queue", "list", "--help"],
        ["queue", "status", "--help"],
        ["attempts", "list", "--help"],
        ["sessions", "status", "--help"],
        ["sessions", "list", "--help"],
        ["events", "list", "--help"],
        ["events", "chain", "--help"],
        ["ps", "--help"],
        ["traces", "--help"],
    ],
)
def test_zeta_cli_inspection_help_omits_project_root(command: list[str]) -> None:
    result = CliRunner().invoke(cli_main.cli, command)

    assert result.exit_code == 0
    assert "--project-root" not in result.output


@pytest.mark.parametrize(
    "command",
    [
        ["queue", "list"],
        ["queue", "status"],
        ["attempts", "list"],
        ["sessions", "status", "missing"],
        ["sessions", "list"],
        ["events", "list"],
        ["events", "chain", "missing"],
        ["ps"],
        ["traces"],
    ],
)
def test_zeta_cli_inspections_reject_project_root(
    command: list[str],
    tmp_path: Path,
) -> None:
    result = CliRunner().invoke(
        cli_main.cli,
        [*command, "--project-root", str(tmp_path)],
    )

    assert result.exit_code == 2
    assert "No such option '--project-root'" in result.output


def test_zeta_cli_fresh_inspection_does_not_create_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "fresh"
    project.mkdir()
    monkeypatch.delenv("ZETA_STATE_DIR", raising=False)
    monkeypatch.chdir(project)

    result = CliRunner().invoke(cli_main.cli, ["ps"])

    assert result.exit_code == 0
    assert result.output == "runs empty\n"
    assert not (project / ".zeta").exists()
    assert not (Path.home() / ".zeta").exists()


def test_zeta_cli_main_reports_invalid_state_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / ".zeta").write_text("not a directory", encoding="utf-8")
    monkeypatch.delenv("ZETA_STATE_DIR", raising=False)
    monkeypatch.chdir(tmp_path)

    exit_code = cli_main.main(["ps"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "runtime state marker is not a directory" in captured.err
    assert "Traceback" not in captured.err


def test_zeta_cli_without_a_command_launches_the_bundled_tui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Path, list[str]]] = []

    def execv(executable: Path, arguments: list[str]) -> None:
        calls.append((executable, arguments))

    monkeypatch.setattr(os, "execv", execv)
    monkeypatch.setattr(sysconfig, "get_path", lambda name: "/venv/bin")
    monkeypatch.setattr(sys, "argv", ["/venv/bin/zeta"])

    result = CliRunner().invoke(cli_main.cli, [])

    assert result.exit_code == 0
    assert calls == [
        (
            Path("/venv/bin/zeta-tui"),
            ["/venv/bin/zeta-tui", "/venv/bin/zeta"],
        )
    ]


def test_zeta_cli_subcommand_does_not_launch_the_tui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def execv(_executable: Path, _arguments: list[str]) -> None:
        raise AssertionError("subcommands must not launch the TUI")

    monkeypatch.setattr(os, "execv", execv)

    result = CliRunner().invoke(cli_main.cli, ["ipc", "--help"])

    assert result.exit_code == 0


def test_zeta_cli_inspection_does_not_mutate_existing_state(tmp_path: Path) -> None:
    state_dir = tmp_path / ".zeta"
    event_store = zeta_events.SqliteEventStore(event_store_path(state_dir))
    event_store.accept(zeta_events.DraftEvent("project.ready", "test", {}))
    event_store.close()
    before = {
        path.name: path.read_bytes() for path in state_dir.iterdir() if path.is_file()
    }

    result = CliRunner().invoke(
        cli_main.cli,
        ["events", "list", "--state-dir", str(state_dir), "--json"],
    )
    after = {
        path.name: path.read_bytes() for path in state_dir.iterdir() if path.is_file()
    }

    assert result.exit_code == 0
    assert before == after


def test_zeta_cli_schedules_status_help_keeps_project_root() -> None:
    schedules = CliRunner().invoke(cli_main.cli, ["schedules", "--help"])
    status = CliRunner().invoke(cli_main.cli, ["schedules", "status", "--help"])

    assert schedules.exit_code == 0
    assert status.exit_code == 0
    assert "--project-root" not in schedules.output
    assert "--project-root" in status.output
    assert "--state-dir" not in schedules.output
    assert "--state-dir" in status.output


def test_zeta_cli_schedules_status_does_not_create_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    (project / "agents").mkdir(parents=True)
    monkeypatch.delenv("ZETA_STATE_DIR", raising=False)

    result = CliRunner().invoke(
        cli_main.cli,
        ["schedules", "status", "--project-root", str(project)],
    )

    assert result.exit_code == 0
    assert result.output == "schedules empty\n"
    assert not (project / ".zeta").exists()


def test_zeta_cli_events_chain_replaces_trace_causal_walk(tmp_path: Path) -> None:
    state_dir = tmp_path / ".zeta"
    event_store = zeta_events.SqliteEventStore(event_store_path(state_dir))
    root = event_store.accept(
        zeta_events.DraftEvent("issue.opened", "github", {})
    ).event
    child = event_store.accept(
        zeta_events.DraftEvent(
            "issue.triaged",
            "agent:triage",
            {},
            caused_by=root.id,
        )
    ).event
    event_store.close()

    chain = CliRunner().invoke(
        cli_main.cli,
        ["events", "chain", child.id, "--state-dir", str(state_dir)],
    )
    removed_command = CliRunner().invoke(
        cli_main.cli,
        ["events", "trace", child.id, "--state-dir", str(state_dir)],
    )

    assert chain.exit_code == 0
    assert chain.output == (
        f"{root.cursor}\tissue.opened\tgithub\t{root.id}\n"
        f"{child.cursor}\tissue.triaged\tagent:triage\t{child.id}\n"
    )
    assert removed_command.exit_code == 2
    assert "No such command 'trace'" in removed_command.output


def test_zeta_cli_queue_status_counts_runtime_queue(tmp_path: Path) -> None:
    state_dir = tmp_path / ".zeta"
    event_store = zeta_events.SqliteEventStore(event_store_path(state_dir))
    dispatcher = harness_dispatch.QueueingDispatcher(event_store)

    dispatch_event(
        dispatcher,
        zeta_events.DraftEvent("github.issue.opened", "github", {}, session_id="repo"),
    )

    result = CliRunner().invoke(
        cli_main.cli,
        ["queue", "status", "--state-dir", str(state_dir)],
    )

    assert result.exit_code == 0
    assert result.output == "unhandled: 1\n"


def test_zeta_cli_run_routes_unhandled_event(tmp_path: Path) -> None:
    state_dir = tmp_path / ".zeta"
    event_store = zeta_events.SqliteEventStore(event_store_path(state_dir))
    event_store.accept(
        zeta_events.DraftEvent("github.issue.opened", "github", {}, session_id="repo")
    )

    result = CliRunner().invoke(
        cli_main.cli,
        [
            "run",
            "--project-root",
            str(tmp_path),
            "--state-dir",
            str(tmp_path / ".zeta"),
        ],
    )
    items = harness_queue.project_queue_items(
        event_store.list_events(zeta_events.Filter())
    )

    assert result.exit_code == 0
    assert result.output == "processed 1\n"
    assert [item.status for item in items] == ["unhandled"]


def test_zeta_cli_removed_scheduler_command_does_not_publish_due_schedules(
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "scheduled.md").write_text(
        """---
name: Scheduled
description: Runs on a schedule.
schedules:
  - cron: "* * * * *"
---
Summarize the repo.
""",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli_main.cli,
        [
            "schedule",
            "--project-root",
            str(tmp_path),
            "--state-dir",
            str(tmp_path / ".zeta"),
            "--once",
        ],
    )

    assert result.exit_code == 2
    assert "No such command 'schedule'" in result.output
    assert not (tmp_path / ".zeta").exists()


def test_zeta_cli_events_publish_records_manual_event(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli_main.cli,
        [
            "events",
            "publish",
            "laptop.resumed",
            "--state-dir",
            str(tmp_path / ".zeta"),
            "--source",
            "manual",
            "--payload-json",
            '{"path":"heartbeat.txt"}',
            "--idempotency-key",
            "resume-1",
            "--json",
        ],
    )

    event_store = zeta_events.SqliteEventStore(event_store_path(tmp_path / ".zeta"))
    try:
        events = event_store.list_events(zeta_events.Filter())
    finally:
        event_store.close()

    assert result.exit_code == 0
    assert json.loads(result.output) == {
        "inserted": True,
        "event": {
            "id": events[0].id,
            "type": "laptop.resumed",
            "source": "manual",
            "payload": {"path": "heartbeat.txt"},
            "idempotency_key": "resume-1",
            "caused_by": None,
            "session_id": None,
            "run_id": None,
            "turn_id": None,
            "timestamp_ms": events[0].timestamp_ms,
            "cursor": 1,
        },
    }
    assert [event.event_type for event in events] == ["laptop.resumed"]


def test_zeta_cli_events_publish_rejects_non_object_payload(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli_main.cli,
        [
            "events",
            "publish",
            "laptop.resumed",
            "--state-dir",
            str(tmp_path / ".zeta"),
            "--payload-json",
            "[]",
        ],
    )

    assert result.exit_code != 0
    assert "payload JSON must be an object" in result.output


def test_zeta_cli_events_publish_reports_invalid_identity_payload(
    tmp_path: Path,
) -> None:
    result = CliRunner().invoke(
        cli_main.cli,
        [
            "events",
            "publish",
            "laptop.resumed",
            "--state-dir",
            str(tmp_path / ".zeta"),
            "--payload-json",
            '{"value":NaN}',
        ],
    )

    assert result.exit_code != 0
    assert "invalid event" in result.output


def test_zeta_agent_spec_parses_retry_policy(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "triage.md").write_text(
        """---
name: Triage
description: Triage issues.
accepts:
  - github.issue.opened
retry:
  max_attempts: 5
  backoff_seconds: 1.5
---
Handle the issue.
""",
        encoding="utf-8",
    )

    specs = zeta_agent_spec.load_specs(agents_dir)
    executors = harness_routing.compile_agent_definitions(specs[0])

    assert specs[0].retry == zeta_agent_spec.RetrySpec(
        max_attempts=5,
        backoff_seconds=1.5,
    )
    assert executors[0].definition.retry_policy == harness_retry.RetryPolicy(
        max_attempts=5,
        backoff_base_seconds=1.5,
    )


def test_zeta_agent_spec_rejects_invalid_retry_policy(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "triage.md").write_text(
        """---
name: Triage
description: Triage issues.
retry:
  max_attempts: no
---
Handle the issue.
""",
        encoding="utf-8",
    )

    with pytest.raises(zeta_agent_spec.SpecError) as exc_info:
        zeta_agent_spec.load_specs(agents_dir)

    assert "max_attempts must be a positive integer" in str(exc_info.value)


def test_zeta_cli_schedule_status_json_lists_next_and_last_tick(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "scheduled.md").write_text(
        """---
name: Scheduled
description: Runs on a schedule.
schedules:
  - cron: "0 8 * * *"
---
Summarize the repo.
""",
        encoding="utf-8",
    )
    event_store = zeta_events.SqliteEventStore(event_store_path(tmp_path / ".zeta"))
    specs = zeta_agent_spec.load_specs(tmp_path / "agents")
    try:
        harness_scheduling.request_due_schedules(
            event_store,
            specs,
            now=datetime(2026, 6, 22, 10, 0, tzinfo=UTC),
        )
    finally:
        event_store.close()
    monkeypatch.setattr(
        harness_scheduling,
        "utc_now",
        lambda: datetime(2026, 6, 22, 10, 5, tzinfo=UTC),
    )

    result = CliRunner().invoke(
        cli_main.cli,
        [
            "schedules",
            "status",
            "--project-root",
            str(tmp_path),
            "--state-dir",
            str(tmp_path / ".zeta"),
            "--json",
        ],
    )

    assert result.exit_code == 0
    rows = json.loads(result.output)
    assert rows == [
        {
            "agent": "scheduled",
            "cron": "0 8 * * *",
            "timezone": None,
            "status": "published",
            "last_published_at": "2026-06-22T08:00:00+00:00",
            "next_at": "2026-06-23T08:00:00+00:00",
            "reason": "same-day backfill",
        }
    ]


def test_zeta_schedule_status_builds_read_only_project_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    state_dir = tmp_path / ".zeta"
    project_root.mkdir()
    state_dir.mkdir()
    monkeypatch.delenv("ZETA_STATE_DIR", raising=False)
    runtime = harness_scheduling.build_scheduler_services(project_root=project_root)

    try:
        assert runtime.project_root == project_root.resolve()
        assert runtime.state_dir == state_dir
        assert runtime.events.events.read_only is True
        assert not event_store_path(runtime.state_dir).exists()
    finally:
        runtime.close()


def test_zeta_local_runtime_builds_project_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    state_dir = tmp_path / ".zeta"
    project_root.mkdir()
    state_dir.mkdir()
    monkeypatch.delenv("ZETA_STATE_DIR", raising=False)
    runtime = harness_worker.build_worker_services(project_root=project_root)

    try:
        assert runtime.project_root == project_root.resolve()
        assert runtime.state_dir == state_dir
        assert runtime.events.path == event_store_path(runtime.state_dir)
        assert runtime.tool_registry.get("zeta.read") is None
    finally:
        asyncio.run(runtime.aclose())


def test_zeta_local_runtime_resolves_default_model_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = ModelSelection(
        profile="qwen",
        model="qwen3-coder",
        url="http://127.0.0.1:8081/v1/chat/completions",
        thinking="high",
    )
    captured: dict[str, Path] = {}

    def active_model_selection(*, session_dir: Path | None = None) -> ModelSelection:
        assert session_dir is not None
        captured["session_dir"] = session_dir
        return selected

    monkeypatch.setattr(
        harness_worker,
        "active_model_selection",
        active_model_selection,
    )
    runtime = harness_worker.build_worker_services(
        project_root=tmp_path,
        state_dir=tmp_path / ".zeta",
    )

    try:
        assert runtime.model_selection == selected
        assert (
            captured["session_dir"]
            == tmp_path.resolve() / ".zeta" / "sessions" / "default"
        )
    finally:
        asyncio.run(runtime.aclose())


def test_zeta_local_runtime_accepts_explicit_tool_registry(tmp_path: Path) -> None:
    registry = CapabilityRegistry()
    runtime = harness_worker.build_worker_services(
        project_root=tmp_path,
        tool_registry=registry,
    )

    try:
        assert runtime.tool_registry is registry
        assert runtime.tool_registry.get("zeta.read") is None
    finally:
        asyncio.run(runtime.aclose())


def test_zeta_local_runtime_selects_tool_executor_provider_from_agent_yaml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    write_project_event_schema(tmp_path, "github.issue.opened")
    (agents_dir / "triage.md").write_text(
        """---
name: Triage
description: Triage issues.
executor:
  provider: remote
  config:
    app: zeta-tools
accepts:
  - github.issue.opened
---
Triage the issue.
""",
        encoding="utf-8",
    )

    executor_closed: list[bool] = []

    class RecordingExecutor:
        async def call(
            self,
            capability_id: str,
            params: dict[str, Any],
            *,
            base_dir: Path | None,
            effect_key: str | None,
        ) -> dict[str, Any]:
            del capability_id, params, base_dir, effect_key
            return {"ok": True}

        async def aclose(self) -> None:
            executor_closed.append(True)

    executor = RecordingExecutor()
    setup_calls: list[tuple[str, CapabilityRegistry, Mapping[str, Any]]] = []

    async def setup(
        agent_id: str,
        registry: CapabilityRegistry,
        config: Mapping[str, Any],
    ) -> ToolExecutor:
        setup_calls.append((agent_id, registry, config))
        return executor

    providers = ToolExecutorProviderRegistry()
    providers.register(ToolExecutorProvider("remote", setup))
    calls: list[dict[str, Any]] = []

    async def fake_run_agent(*args: Any, **kwargs: Any) -> AgentRunResult:
        calls.append({"args": args, "kwargs": kwargs})
        return AgentRunResult(final_answer="triaged")

    monkeypatch.setattr(harness_worker, "run_agent", fake_run_agent)
    runtime = harness_worker.build_worker_services(
        project_root=tmp_path,
        tool_executors=providers,
    )

    async def exercise() -> tuple[str, str, Event, Event]:
        first_event = runtime.events.accept(
            zeta_events.DraftEvent("github.issue.opened", "github", {"number": 1})
        ).event
        second_event = runtime.events.accept(
            zeta_events.DraftEvent("github.issue.opened", "github", {"number": 2})
        ).event
        try:
            first_message = await harness_worker.run_once(runtime)
            second_message = await harness_worker.run_once(runtime)
            return first_message, second_message, first_event, second_event
        finally:
            await runtime.aclose()

    first_message, second_message, first_event, second_event = asyncio.run(exercise())

    assert {first_message, second_message} == {
        f"ran qi_{first_event.id}",
        f"ran qi_{second_event.id}",
    }
    assert setup_calls == [("triage", runtime.tool_registry, {"app": "zeta-tools"})]
    assert executor_closed == [True]
    assert [call["kwargs"]["tool_executor"] for call in calls] == [executor, executor]


def test_zeta_worker_caches_executor_by_agent_and_config(tmp_path: Path) -> None:
    setup_calls: list[tuple[str, Mapping[str, Any]]] = []
    setups_started: set[str] = set()
    distinct_setups_started = asyncio.Event()
    closed: list[str] = []

    class RecordingExecutor:
        def __init__(self, name: str) -> None:
            self.name = name

        async def call(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            del args, kwargs
            return {"ok": True}

        async def aclose(self) -> None:
            closed.append(self.name)

    async def setup(
        agent_id: str,
        registry: CapabilityRegistry,
        config: Mapping[str, Any],
    ) -> ToolExecutor:
        del registry
        app = str(config["app"])
        setups_started.add(app)
        if len(setups_started) == 2:
            distinct_setups_started.set()
        await asyncio.wait_for(distinct_setups_started.wait(), timeout=1)
        setup_calls.append((agent_id, config))
        return RecordingExecutor(app)

    providers = ToolExecutorProviderRegistry()
    providers.register(ToolExecutorProvider("remote", setup))
    runtime = harness_worker.build_worker_services(
        project_root=tmp_path,
        tool_executors=providers,
    )
    agent = harness_routing.AgentDefinition(
        "triage",
        (),
        tool_executor=zeta_agent_spec.ExecutorSpec(
            "remote",
            {
                "app": "one",
                "options": {"region": "eu-west", "retries": 2},
            },
        ),
    )
    equivalent = replace(
        agent,
        tool_executor=zeta_agent_spec.ExecutorSpec(
            "remote",
            {
                "options": {"retries": 2, "region": "eu-west"},
                "app": "one",
            },
        ),
    )
    changed = replace(
        agent,
        tool_executor=zeta_agent_spec.ExecutorSpec("remote", {"app": "two"}),
    )

    async def exercise() -> tuple[ToolExecutor, ToolExecutor, ToolExecutor]:
        first, second, third = await asyncio.gather(
            runtime.tool_executor_for(agent),
            runtime.tool_executor_for(equivalent),
            runtime.tool_executor_for(changed),
        )
        await runtime.aclose()
        return first, second, third

    first, second, third = asyncio.run(exercise())

    assert first is second
    assert third is not first
    assert sorted(setup_calls, key=lambda item: str(item[1]["app"])) == [
        (
            "triage",
            {
                "app": "one",
                "options": {"region": "eu-west", "retries": 2},
            },
        ),
        ("triage", {"app": "two"}),
    ]
    assert sorted(closed) == ["one", "two"]


def test_zeta_worker_rejects_non_json_programmatic_executor_config(
    tmp_path: Path,
) -> None:
    runtime = harness_worker.build_worker_services(project_root=tmp_path)
    agent = harness_routing.AgentDefinition(
        "triage",
        (),
        tool_executor=zeta_agent_spec.ExecutorSpec(
            "local",
            cast(dict[str, Any], {1: "value"}),
        ),
    )

    async def exercise() -> None:
        try:
            with pytest.raises(ValueError, match="keys must be strings"):
                await runtime.tool_executor_for(agent)
        finally:
            await runtime.aclose()

    asyncio.run(exercise())


def test_zeta_worker_closes_every_executor_when_one_close_fails(
    tmp_path: Path,
) -> None:
    closed: list[str] = []

    class RecordingExecutor:
        def __init__(self, name: str) -> None:
            self.name = name

        async def call(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            del args, kwargs
            return {"ok": True}

        async def aclose(self) -> None:
            closed.append(self.name)
            if self.name == "broken":
                raise RuntimeError("close failed")

    async def setup(
        agent_id: str,
        registry: CapabilityRegistry,
        config: Mapping[str, Any],
    ) -> ToolExecutor:
        del agent_id, registry
        return RecordingExecutor(str(config["app"]))

    providers = ToolExecutorProviderRegistry()
    providers.register(ToolExecutorProvider("remote", setup))
    runtime = harness_worker.build_worker_services(
        project_root=tmp_path,
        tool_executors=providers,
    )
    broken = harness_routing.AgentDefinition(
        "broken",
        (),
        tool_executor=zeta_agent_spec.ExecutorSpec("remote", {"app": "broken"}),
    )
    healthy = harness_routing.AgentDefinition(
        "healthy",
        (),
        tool_executor=zeta_agent_spec.ExecutorSpec("remote", {"app": "healthy"}),
    )

    async def exercise() -> None:
        await runtime.tool_executor_for(broken)
        await runtime.tool_executor_for(healthy)
        with pytest.raises(ExceptionGroup, match="shutdown failed") as exc_info:
            await runtime.aclose()
        assert [str(error) for error in exc_info.value.exceptions] == ["close failed"]

    asyncio.run(exercise())

    assert closed == ["broken", "healthy"]


def test_zeta_worker_shutdown_is_shared_and_cancellation_safe(
    tmp_path: Path,
) -> None:
    close_started = asyncio.Event()
    release_close = asyncio.Event()
    closed: list[bool] = []

    class RecordingExecutor:
        async def call(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            del args, kwargs
            return {"ok": True}

        async def aclose(self) -> None:
            close_started.set()
            await release_close.wait()
            closed.append(True)

    async def setup(
        agent_id: str,
        registry: CapabilityRegistry,
        config: Mapping[str, Any],
    ) -> ToolExecutor:
        del agent_id, registry, config
        return RecordingExecutor()

    providers = ToolExecutorProviderRegistry()
    providers.register(ToolExecutorProvider("remote", setup))
    runtime = harness_worker.build_worker_services(
        project_root=tmp_path,
        tool_executors=providers,
    )
    agent = harness_routing.AgentDefinition(
        "triage",
        (),
        tool_executor=zeta_agent_spec.ExecutorSpec("remote"),
    )

    async def exercise() -> None:
        await runtime.tool_executor_for(agent)
        first_close = asyncio.create_task(runtime.aclose())
        await close_started.wait()
        second_close = asyncio.create_task(runtime.aclose())
        await asyncio.sleep(0)
        assert not second_close.done()

        first_close.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_close

        release_close.set()
        await second_close

    asyncio.run(exercise())

    assert closed == [True]


def test_zeta_worker_shutdown_owns_executor_finishing_setup(
    tmp_path: Path,
) -> None:
    setup_started = asyncio.Event()
    release_setup = asyncio.Event()
    closed: list[bool] = []

    class RecordingExecutor:
        async def call(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            del args, kwargs
            return {"ok": True}

        async def aclose(self) -> None:
            closed.append(True)
            raise RuntimeError("late close failed")

    async def setup(
        agent_id: str,
        registry: CapabilityRegistry,
        config: Mapping[str, Any],
    ) -> ToolExecutor:
        del agent_id, registry, config
        setup_started.set()
        await release_setup.wait()
        return RecordingExecutor()

    providers = ToolExecutorProviderRegistry()
    providers.register(ToolExecutorProvider("remote", setup))
    runtime = harness_worker.build_worker_services(
        project_root=tmp_path,
        tool_executors=providers,
    )
    agent = harness_routing.AgentDefinition(
        "triage",
        (),
        tool_executor=zeta_agent_spec.ExecutorSpec("remote"),
    )

    async def exercise() -> None:
        setup_task = asyncio.create_task(runtime.tool_executor_for(agent))
        await setup_started.wait()
        close_task = asyncio.create_task(runtime.aclose())
        await asyncio.sleep(0)
        release_setup.set()

        with pytest.raises(RuntimeError, match="worker services are closed"):
            await setup_task
        with pytest.raises(ExceptionGroup, match="shutdown failed") as exc_info:
            await close_task
        assert [str(error) for error in exc_info.value.exceptions] == [
            "late close failed"
        ]

    asyncio.run(exercise())

    assert closed == [True]


def test_zeta_cli_run_registers_builtin_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, CapabilityRegistry] = {}
    closed: list[bool] = []
    loops: dict[str, asyncio.AbstractEventLoop] = {}

    class Runtime:
        async def aclose(self) -> None:
            loops["close"] = asyncio.get_running_loop()
            closed.append(True)

    def build_worker_services(
        *,
        project_root: Path,
        state_dir: Path | None,
        tool_registry: CapabilityRegistry,
        connector_names: tuple[str, ...] | None,
    ) -> Runtime:
        del project_root, state_dir, connector_names
        captured["tool_registry"] = tool_registry
        return Runtime()

    async def run_once(_runtime: Runtime) -> str:
        loops["run"] = asyncio.get_running_loop()
        return "queue empty"

    monkeypatch.setattr(harness_worker, "build_worker_services", build_worker_services)
    monkeypatch.setattr(harness_worker, "run_once", run_once)

    result = CliRunner().invoke(
        cli_main.cli,
        [
            "run",
            "--project-root",
            str(tmp_path),
            "--state-dir",
            str(tmp_path / ".zeta"),
        ],
    )

    assert result.exit_code == 0
    assert captured["tool_registry"].get("zeta.write") is not None
    assert closed == [True]
    assert loops["run"] is loops["close"]


def test_zeta_local_runtime_run_once_executes_available_queue_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / ".zeta"
    event_store = zeta_events.SqliteEventStore(event_store_path(state_dir))
    event = event_store.accept(
        zeta_events.DraftEvent("github.issue.opened", "github", {}, session_id="repo")
    ).event

    async def run_agent(run: harness_dispatch.AgentInvocation) -> dict[str, object]:
        return {"event_id": run.triggering_event.id}

    def compile_agents(
        spec: object,
        **_kwargs: object,
    ) -> list[harness_dispatch.ExecutableAgent]:
        del spec
        return [
            harness_dispatch.ExecutableAgent(
                harness_dispatch.AgentDefinition(
                    "issue-triage",
                    (harness_dispatch.EventPattern("github.issue.opened"),),
                ),
                run=run_agent,
            )
        ]

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    write_project_event_schema(tmp_path, "github.issue.opened")
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
    monkeypatch.setattr(harness_worker, "compile_agent_definitions", compile_agents)
    registry = CapabilityRegistry()
    runtime = harness_worker.build_worker_services(
        project_root=tmp_path,
        state_dir=state_dir,
        tool_registry=registry,
    )

    with asyncio.Runner() as runner:
        try:
            message = runner.run(harness_worker.run_once(runtime))
            items = harness_queue.project_queue_items(
                event_store.list_events(zeta_events.Filter())
            )
            attempt_rows = event_store.list_attempts()
        finally:
            try:
                runner.run(runtime.aclose())
            finally:
                event_store.close()

    assert message == f"ran qi_{event.id}"
    assert attempt_rows[0]["worker_name"] == "local-runtime"
    assert items == [
        QueueItem(
            queue_item_id=f"qi_{event.id}",
            event_id=event.id,
            target_agent="issue-triage",
            status="completed",
        )
    ]


def test_zeta_local_runtime_run_once_resumes_a_due_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    event_store.append(
        zeta_events.Event(
            id="wait-created-1",
            event_type="runtime.wait.created",
            source="zeta",
            payload={
                "handle": "wait-1",
                "agent_id": "issue-agent",
                "session_id": "agent/issue-agent/original",
                "event_type": "github.issue.updated",
                "fields": {"issue": 5},
                "deadline": "1970-01-01T00:00:02+00:00",
                "source_queue_item_id": "qi-source",
                "project_generation": None,
            },
            idempotency_key="agent.wait:qi-source:0",
            caused_by="attempt-completed-1",
            session_id="agent/issue-agent/original",
            timestamp_ms=500,
        )
    )
    calls: list[harness_dispatch.AgentInvocation] = []

    async def run_agent(run: harness_dispatch.AgentInvocation) -> dict[str, object]:
        calls.append(run)
        return {"final_answer": "resumed"}

    agent = harness_dispatch.ExecutableAgent(
        harness_dispatch.AgentDefinition(
            "issue-agent",
            (harness_dispatch.EventPattern("work.requested"),),
        ),
        run=run_agent,
    )
    monkeypatch.setattr(harness_worker, "project_executors", lambda _runtime: (agent,))
    runtime = harness_worker.WorkerServices(
        project_root=tmp_path,
        state_dir=tmp_path,
        events=event_store,
    )

    with asyncio.Runner() as runner:
        try:
            message = runner.run(harness_worker.run_once(runtime))
            attempts = event_store.list_attempts()
        finally:
            runner.run(runtime.aclose())

    assert message.startswith("ran qi_")
    assert len(calls) == 1
    assert calls[0].triggering_event.event_type == "runtime.wait.timed_out"
    assert attempts[0]["session_id"] == "agent/issue-agent/original"


def test_zeta_worker_validates_project_event_schemas_before_compile(
    tmp_path: Path,
) -> None:
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
    runtime = harness_worker.build_worker_services(project_root=tmp_path)

    try:
        with pytest.raises(ManifestError, match="unknown event 'github.issue.opened'"):
            harness_worker.project_executors(runtime)
    finally:
        asyncio.run(runtime.aclose())


def test_zeta_worker_passes_project_event_registry_to_compiler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {"specs": []}

    def compile_agents(
        spec: object,
        **kwargs: object,
    ) -> list[harness_dispatch.ExecutableAgent]:
        captured["specs"].append(spec)
        captured["event_registry"] = kwargs["event_registry"]
        return []

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    write_project_event_schema(tmp_path, "github.issue.opened")
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
    monkeypatch.setattr(harness_worker, "compile_agent_definitions", compile_agents)
    runtime = harness_worker.build_worker_services(project_root=tmp_path)

    try:
        assert harness_worker.project_executors(runtime) == ()
    finally:
        asyncio.run(runtime.aclose())

    assert [spec.slug for spec in captured["specs"]] == ["triage", "zeta.master"]
    assert captured["event_registry"].knows("github.issue.opened")


def test_zeta_worker_agent_runner_uses_shared_runtime_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_run_agent(
        request: Any,
        **kwargs: Any,
    ) -> AgentRunResult:
        captured["request"] = request
        captured.update(kwargs)
        captured["request"] = request
        return AgentRunResult(final_answer="done")

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    write_project_event_schema(tmp_path, "github.issue.opened")
    (agents_dir / "triage.md").write_text(
        """---
name: Triage
description: Triage issues.
session: shared
accepts:
  - github.issue.opened
---
Triage the issue.
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(harness_worker, "run_agent", fake_run_agent)
    registry = CapabilityRegistry()
    runtime = harness_worker.build_worker_services(
        project_root=tmp_path,
        tool_registry=registry,
    )
    cancellation_event = asyncio.Event()

    with asyncio.Runner() as runner:
        try:
            agent = harness_worker.project_executors(runtime)[0]
            event = zeta_events.Event(
                id="evt_issue",
                event_type="github.issue.opened",
                source="github",
                payload={},
                idempotency_key=None,
                caused_by=None,
                session_id=None,
                run_id=None,
                turn_id=None,
                timestamp_ms=1,
                cursor=1,
            )
            result = runner.run(
                cast(
                    Coroutine[Any, Any, dict[str, Any]],
                    agent.run(
                        harness_dispatch.AgentInvocation(
                            agent.definition,
                            event,
                            attempt_id="att_qi_evt_issue_triage_1",
                            cancellation_event=cancellation_event,
                        )
                    ),
                ),
            )
        finally:
            runner.run(runtime.aclose())

    assert result["final_answer"] == "done"
    assert captured["runtime_context"].session_id == "agent/triage"
    assert captured["runtime_context"].tool_registry is registry
    assert captured["run_id"] == "run_att_qi_evt_issue_triage_1"
    assert captured["request"].objective == "Triage the issue."
    assert captured["cancellation_event"] is cancellation_event


def test_zeta_worker_agent_runner_uses_one_shot_runtime_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_run_agent(
        request: Any,
        **kwargs: Any,
    ) -> AgentRunResult:
        captured["request"] = request
        captured.update(kwargs)
        captured["request"] = request
        return AgentRunResult(final_answer="done")

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    write_project_event_schema(tmp_path, "github.issue.opened")
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
    monkeypatch.setattr(harness_worker, "run_agent", fake_run_agent)
    runtime = harness_worker.build_worker_services(project_root=tmp_path)

    with asyncio.Runner() as runner:
        try:
            agent = harness_worker.project_executors(runtime)[0]
            event = zeta_events.Event(
                id="evt_issue",
                event_type="github.issue.opened",
                source="github",
                payload={},
                idempotency_key=None,
                caused_by=None,
                session_id=None,
                run_id=None,
                turn_id=None,
                timestamp_ms=1,
                cursor=1,
            )
            result = runner.run(
                cast(
                    Coroutine[Any, Any, dict[str, Any]],
                    agent.run(
                        harness_dispatch.AgentInvocation(
                            agent.definition,
                            event,
                            attempt_id="att_qi_evt_issue_triage_1",
                        )
                    ),
                ),
            )
        finally:
            runner.run(runtime.aclose())

    assert result["final_answer"] == "done"
    assert captured["runtime_context"].session_id == "agent/triage/evt_issue"
    assert captured["run_id"] == "run_att_qi_evt_issue_triage_1"
    assert captured["request"].objective == "Triage the issue."


def test_zeta_worker_agent_runner_uses_runtime_model_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_run_agent(
        request: Any,
        **kwargs: Any,
    ) -> AgentRunResult:
        captured["request"] = request
        captured.update(kwargs)
        captured["request"] = request
        return AgentRunResult(final_answer="done")

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    write_project_event_schema(tmp_path, "agent.ping")
    (agents_dir / "ping.md").write_text(
        """---
name: Ping
description: Reacts to pings.
accepts:
  - agent.ping
---
Ping.
""",
        encoding="utf-8",
    )
    selection = ModelSelection(
        profile="qwen",
        model="qwen3-coder",
        url="http://127.0.0.1:8081/v1/chat/completions",
        thinking="high",
    )
    monkeypatch.setattr(harness_worker, "run_agent", fake_run_agent)
    runtime = harness_worker.build_worker_services(project_root=tmp_path)
    runtime.model_selection = selection

    with asyncio.Runner() as runner:
        try:
            agent = harness_worker.project_executors(runtime)[0]
            event = zeta_events.Event(
                id="evt_ping",
                event_type="agent.ping",
                source="manual",
                payload={},
                idempotency_key=None,
                caused_by=None,
                session_id=None,
                run_id=None,
                turn_id=None,
                timestamp_ms=1,
                cursor=1,
            )
            runner.run(
                cast(
                    Coroutine[Any, Any, dict[str, Any]],
                    agent.run(
                        harness_dispatch.AgentInvocation(
                            agent.definition,
                            event,
                            attempt_id="att_qi_evt_ping_1",
                        )
                    ),
                )
            )
        finally:
            runner.run(runtime.aclose())

    config = captured["request"].config
    assert config.model_profile == "qwen"
    assert config.model_name == "qwen3-coder"
    assert config.model_url == "http://127.0.0.1:8081/v1/chat/completions"
    assert config.thinking == "high"


def test_zeta_worker_final_answer_does_not_publish_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_run_agent(
        request: Any,
        **kwargs: Any,
    ) -> AgentRunResult:
        captured["request"] = request
        captured.update(kwargs)
        captured["request"] = request
        return AgentRunResult(final_answer="No event requested.")

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    write_project_event_schema(tmp_path, "agent.ping")
    write_project_event_schema(
        tmp_path,
        "agent.ponged",
        {
            "type": "object",
            "required": ["value"],
            "properties": {"value": {"type": "string"}},
            "additionalProperties": False,
        },
    )
    (agents_dir / "ping.md").write_text(
        """---
name: Ping
description: Reacts to pings.
accepts:
  - agent.ping
publishes:
  - agent.ponged
---
Publish a pong when one is needed.
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(harness_worker, "run_agent", fake_run_agent)
    runtime = harness_worker.build_worker_services(project_root=tmp_path)

    with asyncio.Runner() as runner:
        try:
            agent = harness_worker.project_executors(runtime)[0]
            event = zeta_events.Event(
                id="evt_ping",
                event_type="agent.ping",
                source="manual",
                payload={},
                idempotency_key=None,
                caused_by=None,
                session_id=None,
                run_id=None,
                turn_id=None,
                timestamp_ms=1,
                cursor=1,
            )
            result = runner.run(
                cast(
                    Coroutine[Any, Any, dict[str, Any]],
                    agent.run(
                        harness_dispatch.AgentInvocation(
                            agent.definition,
                            event,
                            attempt_id="att_qi_evt_ping_1",
                        )
                    ),
                )
            )
        finally:
            runner.run(runtime.aclose())

    request = captured["request"]
    assert request.publishable_events == {
        "agent.ponged": {
            "type": "object",
            "required": ["value"],
            "properties": {"value": {"type": "string"}},
            "additionalProperties": False,
        }
    }
    assert request.source_queue_item_id == "qi_evt_ping_ping"
    assert result == {"final_answer": "No event requested."}


def test_zeta_worker_preserves_explicit_publish_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run_agent(request: Any, **kwargs: Any) -> AgentRunResult:
        del request, kwargs
        return AgentRunResult(
            final_answer="done",
            publish_event_requests=[
                zeta_outcomes.PublishEventRequest(
                    handle="pub_explicit",
                    event_type="agent.ponged",
                    payload={"value": "explicit"},
                    at=None,
                    position=0,
                )
            ],
        )

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    write_project_event_schema(tmp_path, "agent.ping")
    write_project_event_schema(tmp_path, "agent.ponged")
    (agents_dir / "ping.md").write_text(
        """---
name: Ping
description: Reacts to pings.
accepts:
  - agent.ping
publishes:
  - agent.ponged
---
Publish a pong.
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(harness_worker, "run_agent", fake_run_agent)
    runtime = harness_worker.build_worker_services(project_root=tmp_path)

    with asyncio.Runner() as runner:
        try:
            agent = harness_worker.project_executors(runtime)[0]
            result = runner.run(
                cast(
                    Coroutine[Any, Any, dict[str, Any]],
                    agent.run(
                        harness_dispatch.AgentInvocation(
                            agent.definition,
                            Event(
                                id="evt_ping",
                                event_type="agent.ping",
                                source="manual",
                                payload={},
                                idempotency_key=None,
                                caused_by=None,
                                session_id=None,
                                run_id=None,
                                turn_id=None,
                                timestamp_ms=1,
                                cursor=1,
                            ),
                            queue_item_id="qi_ping",
                            attempt_id="att_qi_ping_1",
                        )
                    ),
                )
            )
        finally:
            runner.run(runtime.aclose())

    assert result["publish_event_requests"] == [
        {
            "handle": "pub_explicit",
            "event_type": "agent.ponged",
            "payload": {"value": "explicit"},
            "at": None,
            "position": 0,
        }
    ]


def test_zeta_worker_agent_runner_uses_agent_model_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_run_agent(
        request: Any,
        **kwargs: Any,
    ) -> AgentRunResult:
        captured["request"] = request
        captured.update(kwargs)
        captured["request"] = request
        return AgentRunResult(final_answer="done")

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    write_project_event_schema(tmp_path, "agent.ping")
    (agents_dir / "ping.md").write_text(
        """---
name: Ping
description: Reacts to pings.
model:
  name: qwen3.6-27b-q8-local
  url: http://127.0.0.1:8080/v1/chat/completions
accepts:
  - agent.ping
---
Ping.
""",
        encoding="utf-8",
    )
    runtime_selection = ModelSelection(
        profile="codex",
        model="gpt-5.5",
        url="https://chatgpt.com/backend-api",
        api="codex-responses",
    )
    monkeypatch.setattr(harness_worker, "run_agent", fake_run_agent)
    runtime = harness_worker.build_worker_services(project_root=tmp_path)
    runtime.model_selection = runtime_selection

    with asyncio.Runner() as runner:
        try:
            agent = harness_worker.project_executors(runtime)[0]
            event = zeta_events.Event(
                id="evt_ping",
                event_type="agent.ping",
                source="manual",
                payload={},
                idempotency_key=None,
                caused_by=None,
                session_id=None,
                run_id=None,
                turn_id=None,
                timestamp_ms=1,
                cursor=1,
            )
            runner.run(
                cast(
                    Coroutine[Any, Any, dict[str, Any]],
                    agent.run(
                        harness_dispatch.AgentInvocation(
                            agent.definition,
                            event,
                            attempt_id="att_qi_evt_ping_1",
                        )
                    ),
                )
            )
        finally:
            runner.run(runtime.aclose())

    config = captured["request"].config
    assert config.model_profile is None
    assert config.model_name == "qwen3.6-27b-q8-local"
    assert config.model_url == "http://127.0.0.1:8080/v1/chat/completions"
    assert config.model_api is None


def test_zeta_local_runtime_heartbeats_running_locks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    accepted = event_store.accept(
        zeta_events.DraftEvent("github.issue.opened", "github", {})
    ).event
    renewed_locks: list[dict[str, int]] = []

    async def run_agent(run: harness_dispatch.AgentInvocation) -> dict[str, object]:
        deadline = asyncio.get_running_loop().time() + 1
        initial_expires_at: int | None = None
        while asyncio.get_running_loop().time() < deadline:
            locks = event_store.list_locks()
            if locks:
                expires_at = int(locks[0]["expires_at"])
                if initial_expires_at is None:
                    initial_expires_at = expires_at
                elif expires_at > initial_expires_at:
                    renewed_locks.append({"expires_at": expires_at})
                    return {"event_id": run.triggering_event.id}
            await asyncio.sleep(0.005)
        raise AssertionError("lock lease was not refreshed")

    agent = harness_dispatch.ExecutableAgent(
        harness_dispatch.AgentDefinition(
            "issue-triage",
            (harness_dispatch.EventPattern("github.issue.opened"),),
            lock_keys=("context:repo",),
        ),
        run=run_agent,
    )
    monkeypatch.setattr(harness_worker, "project_executors", lambda _runtime: (agent,))
    runtime = harness_worker.WorkerServices(
        project_root=tmp_path,
        state_dir=tmp_path,
        events=event_store,
    )
    monkeypatch.setattr(harness_worker, "ATTEMPT_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(harness_worker, "QUEUE_LEASE_MS", 1_000)

    with asyncio.Runner() as runner:
        try:
            message = runner.run(harness_worker.run_once(runtime))
            locks = event_store.list_locks()
        finally:
            runner.run(runtime.aclose())

    assert message == f"ran qi_{accepted.id}"
    assert renewed_locks
    assert locks == []


def test_zeta_local_runtime_stops_after_a_durable_cancel_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    accepted = event_store.accept(
        zeta_events.DraftEvent("github.issue.opened", "github", {})
    ).event
    started = asyncio.Event()

    async def run_agent(run: harness_dispatch.AgentInvocation) -> dict[str, object]:
        cancellation_event = cast(asyncio.Event, run.cancellation_event)
        started.set()
        await asyncio.wait_for(cancellation_event.wait(), timeout=1)
        return {"outcome": "cancelled", "stop_reason": "aborted"}

    agent = harness_dispatch.ExecutableAgent(
        harness_dispatch.AgentDefinition(
            "issue-triage",
            (harness_dispatch.EventPattern("github.issue.opened"),),
        ),
        run=run_agent,
    )
    monkeypatch.setattr(harness_worker, "project_executors", lambda _runtime: (agent,))
    monkeypatch.setattr(harness_worker, "ATTEMPT_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    runtime = harness_worker.WorkerServices(
        project_root=tmp_path,
        state_dir=tmp_path,
        events=event_store,
    )

    async def exercise() -> tuple[str, Any]:
        task = asyncio.create_task(harness_worker.run_once(runtime))
        await asyncio.wait_for(started.wait(), timeout=1)
        cancellation = event_store.cancel_queue_item(f"qi_{accepted.id}")
        return await task, cancellation

    with asyncio.Runner() as runner:
        try:
            message, cancellation = runner.run(exercise())
            queue_event_types = [
                event.event_type
                for event in event_store.list_events(
                    Filter(event_type_prefix="runtime.queue_item.")
                )
            ]
            attempts = event_store.list_attempts()
        finally:
            runner.run(runtime.aclose())

    assert message == f"ran qi_{accepted.id}"
    assert cancellation.status == "cancelling"
    assert queue_event_types[-2:] == [
        "runtime.queue_item.cancel_requested",
        "runtime.queue_item.cancelled",
    ]
    assert attempts[0]["status"] == "cancelled"


def test_zeta_local_runtime_binds_the_session_before_the_agent_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    accepted = event_store.accept(
        zeta_events.DraftEvent("github.issue.opened", "github", {})
    ).event
    observed: dict[str, Any] = {}

    async def run_agent(run: harness_dispatch.AgentInvocation) -> dict[str, object]:
        observed.update(event_store.queue_item(cast(str, run.queue_item_id)) or {})
        observed["claimed_events"] = len(
            event_store.list_events(Filter(event_type="runtime.queue_item.claimed"))
        )
        return {"event_id": run.triggering_event.id}

    agent = harness_dispatch.ExecutableAgent(
        harness_dispatch.AgentDefinition(
            "issue-triage",
            (harness_dispatch.EventPattern("github.issue.opened"),),
            session="shared",
        ),
        run=run_agent,
    )
    monkeypatch.setattr(harness_worker, "project_executors", lambda _runtime: (agent,))
    runtime = harness_worker.WorkerServices(
        project_root=tmp_path,
        state_dir=tmp_path,
        events=event_store,
    )

    with asyncio.Runner() as runner:
        try:
            runner.run(harness_worker.run_once(runtime))
        finally:
            runner.run(runtime.aclose())

    assert observed["queue_item_id"] == f"qi_{accepted.id}"
    assert observed["target_agent"] == "issue-triage"
    assert observed["session_id"] == "agent/issue-triage"
    assert observed["status"] == "claimed"
    assert observed["claimed_events"] == 1


def test_zeta_local_runtime_does_not_complete_stale_queue_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    accepted = event_store.accept(
        zeta_events.DraftEvent("github.issue.opened", "github", {})
    ).event

    async def run_agent(run: harness_dispatch.AgentInvocation) -> dict[str, object]:
        now_ms = accepted.timestamp_ms + 10_000
        event_store.reconcile_expired_queue_claims(now_ms=now_ms)
        replacement = event_store.claim_next_queue_item(
            "local-runtime",
            lease_ms=60_000,
            now_ms=now_ms,
        )
        assert replacement is not None
        assert replacement.queue_item_id == run.queue_item_id
        return {"event_id": run.triggering_event.id}

    agent = harness_dispatch.ExecutableAgent(
        harness_dispatch.AgentDefinition(
            "issue-triage",
            (harness_dispatch.EventPattern("github.issue.opened"),),
        ),
        run=run_agent,
    )
    monkeypatch.setattr(harness_worker, "project_executors", lambda _runtime: (agent,))
    runtime = harness_worker.WorkerServices(
        project_root=tmp_path,
        state_dir=tmp_path,
        events=event_store,
    )
    monkeypatch.setattr(harness_worker, "QUEUE_LEASE_MS", 1_000)

    with asyncio.Runner() as runner:
        try:
            message = runner.run(harness_worker.run_once(runtime))
            event_types = [
                event.event_type
                for event in event_store.list_events(zeta_events.Filter())
            ]
            queue_item = event_store.list_queue_items()[0]
            attempt = event_store.list_attempts()[0]
        finally:
            runner.run(runtime.aclose())

    assert message == f"ran qi_{accepted.id}"
    assert "runtime.attempt.completed" not in event_types
    assert "runtime.queue_item.completed" not in event_types
    assert queue_item["status"] == "claimed"
    assert queue_item["claimed_by"] == "local-runtime"
    assert attempt["status"] == "running"


def test_zeta_local_runtime_does_not_complete_an_expired_queue_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    accepted = event_store.accept(
        zeta_events.DraftEvent("github.issue.opened", "github", {})
    ).event

    claim_time_ms = accepted.timestamp_ms + 1_000
    now_ms = [claim_time_ms]

    async def run_agent(_run: harness_dispatch.AgentInvocation) -> dict[str, object]:
        now_ms[0] = claim_time_ms + 1_001
        return {"final_answer": "stale"}

    agent = harness_dispatch.ExecutableAgent(
        harness_dispatch.AgentDefinition(
            "issue-triage",
            (harness_dispatch.EventPattern("github.issue.opened"),),
        ),
        run=run_agent,
    )
    monkeypatch.setattr(harness_worker, "project_executors", lambda _runtime: (agent,))
    runtime = harness_worker.WorkerServices(
        project_root=tmp_path,
        state_dir=tmp_path,
        events=event_store,
    )
    monkeypatch.setattr(
        harness_dispatch,
        "current_time_ms",
        lambda: now_ms[0],
    )
    monkeypatch.setattr(harness_worker, "QUEUE_LEASE_MS", 1_000)

    with asyncio.Runner() as runner:
        try:
            message = runner.run(harness_worker.run_once(runtime))
            event_types = [
                event.event_type
                for event in event_store.list_events(zeta_events.Filter())
            ]
            queue_item = event_store.list_queue_items()[0]
            attempt = event_store.list_attempts()[0]
        finally:
            runner.run(runtime.aclose())

    assert message.startswith("ran ")
    assert "runtime.attempt.completed" not in event_types
    assert "runtime.queue_item.completed" not in event_types
    assert queue_item["status"] == "claimed"
    assert attempt["status"] == "running"


def test_zeta_local_runtime_run_once_fans_out_pending_queue_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    accepted = event_store.accept(
        zeta_events.DraftEvent("github.issue.opened", "github", {})
    ).event
    calls: list[harness_dispatch.AgentInvocation] = []

    async def run_agent(run: harness_dispatch.AgentInvocation) -> dict[str, object]:
        calls.append(run)
        return {"event_id": run.triggering_event.id}

    agents = (
        harness_dispatch.ExecutableAgent(
            harness_dispatch.AgentDefinition(
                "agent.one",
                (harness_dispatch.EventPattern("github.issue.opened"),),
            ),
            run=run_agent,
        ),
        harness_dispatch.ExecutableAgent(
            harness_dispatch.AgentDefinition(
                "agent.two",
                (harness_dispatch.EventPattern("github.issue.opened"),),
            ),
            run=run_agent,
        ),
    )
    monkeypatch.setattr(harness_worker, "project_executors", lambda _runtime: agents)
    runtime = harness_worker.WorkerServices(
        project_root=tmp_path,
        state_dir=tmp_path,
        events=event_store,
    )

    with asyncio.Runner() as runner:
        try:
            message = runner.run(harness_worker.run_once(runtime))
            items = harness_queue.project_queue_items(
                event_store.list_events(zeta_events.Filter())
            )
        finally:
            runner.run(runtime.aclose())

    assert message == f"routed {accepted.id}"
    assert calls == []
    assert [(item.queue_item_id, item.target_agent, item.status) for item in items] == [
        (f"qi_{accepted.id}", "", "completed"),
        (f"qi_{accepted.id}_agent_one", "agent.one", "available"),
        (f"qi_{accepted.id}_agent_two", "agent.two", "available"),
    ]


def test_zeta_fanout_publishes_every_session_binding_before_releasing_routing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.sqlite3"
    event_store = zeta_events.SqliteEventStore(path)
    observer = zeta_events.SqliteEventStore(path)
    first = event_store.accept(DraftEvent("work.requested", "test", {})).event
    later = event_store.accept(DraftEvent("work.later", "test", {})).event
    event_store.accept(
        DraftEvent(
            "runtime.queue_item.available",
            "zeta",
            {
                "queue_item_id": f"qi_{later.id}_agent_two",
                "event_id": later.id,
                "target_agent": "agent.two",
                "session_id": "agent/agent.two",
                "status": "available",
            },
            session_id="agent/agent.two",
        )
    )
    observed_claims: list[str | None] = []

    def observe(event: Event) -> None:
        if event.event_type != "runtime.queue_item.completed" or observed_claims:
            return
        claim = observer.claim_next_queue_item(
            "observer",
            lease_ms=1_000,
            now_ms=later.timestamp_ms + 1_000,
            exclude_queue_item_ids=(f"qi_{first.id}_agent_one",),
        )
        observed_claims.append(claim.queue_item_id if claim is not None else None)

    dispatcher = harness_dispatch.QueueingDispatcher(
        event_store,
        routes=(
            harness_dispatch.AgentRoute(
                "agent.one",
                (harness_dispatch.EventPattern("work.requested"),),
                session="shared",
            ),
            harness_dispatch.AgentRoute(
                "agent.two",
                (harness_dispatch.EventPattern("work.requested"),),
                session="shared",
            ),
        ),
        publish_event=observe,
    )

    asyncio.run(dispatcher.run_next())

    assert observed_claims == [f"qi_{first.id}_agent_two"]


def test_zeta_scheduler_publishes_due_schedules_directly_once_per_minute(
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "scheduled.md").write_text(
        """---
name: Scheduled
description: Runs on a schedule.
schedules:
  - cron: "* * * * *"
---
Summarize the repo.
""",
        encoding="utf-8",
    )
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    specs = zeta_agent_spec.load_specs(tmp_path / "agents")

    try:
        first = harness_scheduling.request_due_schedules(
            event_store,
            specs,
            now=datetime(2026, 6, 22, 12, 34, 56, tzinfo=UTC),
        )
        second = harness_scheduling.request_due_schedules(
            event_store,
            specs,
            now=datetime(2026, 6, 22, 12, 34, 59, tzinfo=UTC),
        )
        events = event_store.list_events(zeta_events.Filter(event_type_prefix="agent."))
    finally:
        event_store.close()

    assert [event.event_type for event in first] == ["agent.scheduled.scheduled"]
    assert second == []
    assert [event.event_type for event in events] == ["agent.scheduled.scheduled"]
    scheduled_event = first[0]
    assert scheduled_event.source == "zeta:scheduler"
    assert scheduled_event.payload == {
        "date": "2026-06-22",
        "timestamp": "2026-06-22T12:34:00+00:00",
    }
    assert (
        scheduled_event.idempotency_key
        == "schedule:scheduled:* * * * *:2026-06-22T12:34:00+00:00"
    )


def test_zeta_scheduler_payload_uses_the_intended_local_occurrence(
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "morning-briefing.md").write_text(
        """---
name: Morning briefing
description: Prepares the morning briefing.
schedules:
  - cron: "0 7 * * *"
    timezone: America/Denver
session: "morning-briefing-{date}"
---
Prepare the briefing.
""",
        encoding="utf-8",
    )
    event_store = zeta_events.MemoryEventStore()
    spec = zeta_agent_spec.load_specs(agents_dir)[0]

    scheduled_event = harness_scheduling.request_due_schedules(
        event_store,
        [spec],
        now=datetime(2026, 8, 9, 13, 42, tzinfo=UTC),
    )[0]

    assert scheduled_event.payload == {
        "date": "2026-08-09",
        "timestamp": "2026-08-09T07:00:00-06:00",
    }
    assert (
        harness_templates.agent_session_id(
            spec.slug,
            spec.session,
            scheduled_event,
        )
        == "agent/morning-briefing/morning-briefing-2026-08-09"
    )


def test_zeta_scheduler_backfills_latest_same_day_schedule(
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "scheduled.md").write_text(
        """---
name: Scheduled
description: Runs on a schedule.
schedules:
  - cron: "0 8 * * *"
---
Summarize the repo.
""",
        encoding="utf-8",
    )
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    specs = zeta_agent_spec.load_specs(tmp_path / "agents")

    try:
        early = harness_scheduling.request_due_schedules(
            event_store,
            specs,
            now=datetime(2026, 6, 22, 7, 59, tzinfo=UTC),
        )
        late = harness_scheduling.request_due_schedules(
            event_store,
            specs,
            now=datetime(2026, 6, 22, 10, 0, tzinfo=UTC),
        )
        repeated = harness_scheduling.request_due_schedules(
            event_store,
            specs,
            now=datetime(2026, 6, 22, 10, 1, tzinfo=UTC),
        )
        durable_events = event_store.list_events(zeta_events.Filter())
        events = [
            event
            for event in durable_events
            if event.event_type == "agent.scheduled.scheduled"
        ]
        decisions = [
            event
            for event in durable_events
            if event.event_type.startswith("scheduler.")
        ]
    finally:
        event_store.close()

    assert early == []
    assert repeated == []
    assert [event.event_type for event in late] == ["agent.scheduled.scheduled"]
    assert [event.event_type for event in events] == ["agent.scheduled.scheduled"]
    assert late[0].payload == {
        "date": "2026-06-22",
        "timestamp": "2026-06-22T08:00:00+00:00",
    }
    assert [event.event_type for event in decisions] == [
        "scheduler.tick.published",
        "scheduler.tick.skipped",
    ]
    assert decisions[0].payload["status"] == "published"
    assert decisions[0].payload["reason"] == "same-day backfill"
    assert decisions[0].payload["scheduled_at"] == "2026-06-22T08:00:00+00:00"
    assert decisions[0].payload["observed_at"] == "2026-06-22T10:00:00+00:00"
    assert decisions[0].payload["published_event_id"] == late[0].id
    assert decisions[1].payload["status"] == "skipped"
    assert decisions[1].payload["reason"] == "already published"
    assert (
        late[0].idempotency_key
        == "schedule:scheduled:0 8 * * *:2026-06-22T08:00:00+00:00"
    )


def test_zeta_scheduler_does_not_backfill_previous_day_schedule(
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "scheduled.md").write_text(
        """---
name: Scheduled
description: Runs on a schedule.
schedules:
  - cron: "0 8 * * *"
---
Summarize the repo.
""",
        encoding="utf-8",
    )
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    specs = zeta_agent_spec.load_specs(tmp_path / "agents")

    try:
        earlier_events = harness_scheduling.request_due_schedules(
            event_store,
            specs,
            now=datetime(2026, 6, 21, 10, 0, tzinfo=UTC),
        )
        scheduled_events = harness_scheduling.request_due_schedules(
            event_store,
            specs,
            now=datetime(2026, 6, 23, 7, 0, tzinfo=UTC),
        )
        decisions = event_store.list_events(
            zeta_events.Filter(event_type_prefix="scheduler.tick.")
        )
    finally:
        event_store.close()

    assert [event.event_type for event in earlier_events] == [
        "agent.scheduled.scheduled"
    ]
    assert scheduled_events == []
    assert [event.event_type for event in decisions] == [
        "scheduler.tick.published",
        "scheduler.tick.missed",
    ]
    assert decisions[1].payload["status"] == "missed"
    assert decisions[1].payload["reason"] == "previous-day tick not backfilled"
    assert decisions[1].payload["scheduled_at"] == "2026-06-22T08:00:00+00:00"
    assert decisions[1].payload["observed_at"] == "2026-06-23T07:00:00+00:00"


def test_zeta_scheduler_catches_up_latest_weekly_schedule_across_days(
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "scheduled.md").write_text(
        """---
name: Scheduled
description: Runs on a schedule.
schedules:
  - cron: "0 18 * * 0"
    catchup: latest
---
Summarize the repo.
""",
        encoding="utf-8",
    )
    event_store = zeta_events.MemoryEventStore()
    specs = zeta_agent_spec.load_specs(tmp_path / "agents")

    try:
        before_due = harness_scheduling.request_due_schedules(
            event_store,
            specs,
            now=datetime(2026, 6, 19, 12, 0, tzinfo=UTC),
        )
        after_wake = harness_scheduling.request_due_schedules(
            event_store,
            specs,
            now=datetime(2026, 6, 22, 9, 0, tzinfo=UTC),
        )
        repeated = harness_scheduling.request_due_schedules(
            event_store,
            specs,
            now=datetime(2026, 6, 22, 10, 0, tzinfo=UTC),
        )
        decisions = event_store.list_events(
            zeta_events.Filter(event_type_prefix="scheduler.tick.")
        )
    finally:
        event_store.close()

    assert before_due == []
    assert [event.event_type for event in after_wake] == ["agent.scheduled.scheduled"]
    assert repeated == []
    assert after_wake[0].payload == {
        "date": "2026-06-21",
        "timestamp": "2026-06-21T18:00:00+00:00",
    }
    assert after_wake[0].idempotency_key == (
        "schedule:scheduled:0 18 * * 0:2026-06-21T18:00:00+00:00"
    )
    assert [decision.event_type for decision in decisions] == [
        "scheduler.tick.activated",
        "scheduler.tick.missed",
        "scheduler.tick.published",
        "scheduler.tick.skipped",
    ]
    assert decisions[2].payload["reason"] == "latest catch-up"
    assert decisions[2].payload["scheduled_at"] == "2026-06-21T18:00:00+00:00"
    assert decisions[2].payload["observed_at"] == "2026-06-22T09:00:00+00:00"


def test_zeta_scheduler_does_not_catch_up_before_schedule_activation(
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "scheduled.md").write_text(
        """---
name: Scheduled
description: Runs on a schedule.
schedules:
  - cron: "0 18 * * 0"
    catchup: latest
---
Summarize the repo.
""",
        encoding="utf-8",
    )
    event_store = zeta_events.MemoryEventStore()
    specs = zeta_agent_spec.load_specs(tmp_path / "agents")

    try:
        events = harness_scheduling.request_due_schedules(
            event_store,
            specs,
            now=datetime(2026, 6, 22, 9, 0, tzinfo=UTC),
        )
    finally:
        event_store.close()

    assert events == []


def test_zeta_scheduler_backfill_uses_schedule_timezone(
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "scheduled.md").write_text(
        """---
name: Scheduled
description: Runs on a schedule.
schedules:
  - cron: "0 8 * * *"
    timezone: America/Los_Angeles
---
Summarize the repo.
""",
        encoding="utf-8",
    )
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    specs = zeta_agent_spec.load_specs(tmp_path / "agents")

    try:
        events = harness_scheduling.request_due_schedules(
            event_store,
            specs,
            now=datetime(2026, 6, 22, 17, 0, tzinfo=UTC),
        )
    finally:
        event_store.close()

    assert [event.event_type for event in events] == ["agent.scheduled.scheduled"]
    assert (
        events[0].idempotency_key
        == "schedule:scheduled:0 8 * * *:2026-06-22T08:00:00-07:00"
    )


def test_zeta_scheduler_status_reports_pending_next_tick(
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "scheduled.md").write_text(
        """---
name: Scheduled
description: Runs on a schedule.
schedules:
  - cron: "0 8 * * *"
---
Summarize the repo.
""",
        encoding="utf-8",
    )
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    specs = zeta_agent_spec.load_specs(tmp_path / "agents")

    try:
        rows = harness_scheduling.schedule_status(
            event_store,
            specs,
            now=datetime(2026, 6, 22, 7, 30, tzinfo=UTC),
        )
        decisions = event_store.list_events(
            zeta_events.Filter(event_type_prefix="scheduler.tick.")
        )
    finally:
        event_store.close()

    assert decisions == []
    assert [row.status for row in rows] == ["pending"]
    assert rows[0].agent == "scheduled"
    assert rows[0].cron == "0 8 * * *"
    assert rows[0].last_published_at is None
    assert rows[0].next_at == "2026-06-22T08:00:00+00:00"
    assert rows[0].reason == "next tick is in the future"


def test_zeta_scheduler_status_reports_last_published_tick(
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "scheduled.md").write_text(
        """---
name: Scheduled
description: Runs on a schedule.
schedules:
  - cron: "0 8 * * *"
---
Summarize the repo.
""",
        encoding="utf-8",
    )
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    specs = zeta_agent_spec.load_specs(tmp_path / "agents")

    try:
        harness_scheduling.request_due_schedules(
            event_store,
            specs,
            now=datetime(2026, 6, 22, 10, 0, tzinfo=UTC),
        )
        rows = harness_scheduling.schedule_status(
            event_store,
            specs,
            now=datetime(2026, 6, 22, 10, 5, tzinfo=UTC),
        )
    finally:
        event_store.close()

    assert [row.status for row in rows] == ["published"]
    assert rows[0].last_published_at == "2026-06-22T08:00:00+00:00"
    assert rows[0].next_at == "2026-06-23T08:00:00+00:00"
    assert rows[0].reason == "same-day backfill"


def test_zeta_local_runtime_scheduled_event_is_accepted_by_agent(
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "scheduled.md").write_text(
        """---
name: Scheduled
description: Runs on a schedule.
schedules:
  - cron: "* * * * *"
---
Summarize the repo.
""",
        encoding="utf-8",
    )
    specs = zeta_agent_spec.load_specs(tmp_path / "agents")

    assert specs[0].accepts == ("agent.scheduled.scheduled",)
    assert zeta_agent_spec.matches(specs[0], "agent.scheduled.scheduled")


def test_zeta_scheduler_published_event_runs_on_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[harness_dispatch.AgentInvocation] = []

    async def run_agent(run: harness_dispatch.AgentInvocation) -> dict[str, object]:
        calls.append(run)
        return {"event_type": run.triggering_event.event_type}

    def compile_agents(
        spec: object,
        **_kwargs: object,
    ) -> list[harness_dispatch.ExecutableAgent]:
        del spec
        return [
            harness_dispatch.ExecutableAgent(
                harness_dispatch.AgentDefinition(
                    "scheduled",
                    (harness_dispatch.EventPattern("agent.scheduled.scheduled"),),
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
schedules:
  - cron: "* * * * *"
---
Summarize the repo.
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(harness_worker, "compile_agent_definitions", compile_agents)
    event_store = zeta_events.SqliteEventStore(event_store_path(tmp_path / ".zeta"))
    specs = zeta_agent_spec.load_specs(tmp_path / "agents")
    scheduled_events = harness_scheduling.request_due_schedules(
        event_store,
        specs,
        now=datetime(2026, 6, 22, 12, 34, tzinfo=UTC),
    )
    runtime = harness_worker.WorkerServices(
        project_root=tmp_path,
        state_dir=tmp_path / ".zeta",
        events=event_store,
    )

    with asyncio.Runner() as runner:
        try:
            message = runner.run(harness_worker.run_once(runtime))
            items = harness_queue.project_queue_items(
                runtime.events.list_events(zeta_events.Filter())
            )
        finally:
            runner.run(runtime.aclose())

    expected_payload = {
        "date": "2026-06-22",
        "timestamp": "2026-06-22T12:34:00+00:00",
    }
    assert [event.payload for event in scheduled_events] == [expected_payload]
    assert message == f"ran qi_{scheduled_events[0].id}"
    assert [call.triggering_event.event_type for call in calls] == [
        "agent.scheduled.scheduled"
    ]
    assert [call.triggering_event.payload for call in calls] == [expected_payload]
    assert [item.status for item in items] == ["completed"]


def test_zeta_local_runtime_run_forever_reuses_run_once_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / ".zeta"
    event_store = zeta_events.SqliteEventStore(event_store_path(state_dir))
    event = event_store.accept(
        zeta_events.DraftEvent("github.issue.opened", "github", {}, session_id="repo")
    ).event
    calls: list[harness_dispatch.AgentInvocation] = []

    async def exercise() -> None:
        stop_event = asyncio.Event()

        async def run_agent(run: harness_dispatch.AgentInvocation) -> dict[str, object]:
            calls.append(run)
            stop_event.set()
            return {"event_id": run.triggering_event.id}

        def compile_agents(
            spec: object,
            **_kwargs: object,
        ) -> list[harness_dispatch.ExecutableAgent]:
            del spec
            return [
                harness_dispatch.ExecutableAgent(
                    harness_dispatch.AgentDefinition(
                        "issue-triage",
                        (harness_dispatch.EventPattern("github.issue.opened"),),
                    ),
                    run=run_agent,
                )
            ]

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        write_project_event_schema(tmp_path, "github.issue.opened")
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
            harness_worker,
            "compile_agent_definitions",
            compile_agents,
        )
        runtime = harness_worker.build_worker_services(
            project_root=tmp_path,
            state_dir=state_dir,
        )
        try:
            await harness_worker.run_forever(
                runtime,
                poll_interval_seconds=0,
                stop_event=stop_event,
            )
        finally:
            await runtime.aclose()

    asyncio.run(exercise())
    try:
        items = harness_queue.project_queue_items(
            event_store.list_events(zeta_events.Filter())
        )
    finally:
        event_store.close()

    assert [call.triggering_event.id for call in calls] == [event.id]
    assert [item.status for item in items] == ["completed"]


def test_zeta_local_runtime_run_forever_respects_max_concurrent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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

    async def exercise() -> list[QueueItem]:
        stop_event = asyncio.Event()
        both_started = asyncio.Event()
        release = asyncio.Event()

        async def run_agent(run: harness_dispatch.AgentInvocation) -> dict[str, object]:
            started.append(run.triggering_event.id)
            if len(started) == 2:
                both_started.set()
            await both_started.wait()
            release.set()
            stop_event.set()
            return {"event_id": run.triggering_event.id}

        agent = harness_dispatch.ExecutableAgent(
            harness_dispatch.AgentDefinition(
                "issue-triage",
                (harness_dispatch.EventPattern("github.issue.opened"),),
            ),
            run=run_agent,
        )
        monkeypatch.setattr(
            harness_worker, "project_executors", lambda _runtime: (agent,)
        )
        runtime = harness_worker.WorkerServices(
            project_root=tmp_path,
            state_dir=tmp_path,
            events=event_store,
            max_concurrent=2,
        )

        try:
            worker = asyncio.create_task(
                harness_worker.run_forever(
                    runtime,
                    poll_interval_seconds=0,
                    stop_event=stop_event,
                )
            )
            await asyncio.wait_for(release.wait(), timeout=1)
            await worker
            return harness_queue.project_queue_items(
                event_store.list_events(zeta_events.Filter())
            )
        finally:
            await runtime.aclose()

    items = asyncio.run(exercise())

    assert sorted(started) == sorted(event.id for event in events)
    assert [item.status for item in items] == ["completed", "completed"]


def test_zeta_local_runtime_run_forever_logs_and_continues_after_run_once_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    runtime = harness_worker.WorkerServices(
        project_root=tmp_path,
        state_dir=tmp_path,
        events=event_store,
    )
    calls = 0

    async def run_once(runtime: harness_worker.WorkerServices) -> str:
        nonlocal calls
        del runtime
        calls += 1
        if calls == 1:
            raise RuntimeError("poisoned queue item")
        stop_event.set()
        return "queue empty"

    async def exercise() -> None:
        try:
            with caplog.at_level(logging.ERROR, logger=harness_worker.__name__):
                await harness_worker.run_forever(
                    runtime,
                    poll_interval_seconds=0,
                    stop_event=stop_event,
                )
        finally:
            await runtime.aclose()

    stop_event = asyncio.Event()
    monkeypatch.setattr(harness_worker, "run_once", run_once)

    asyncio.run(exercise())

    assert calls == 2
    assert "queue worker task failed" in caplog.text


def test_zeta_local_runtime_run_forever_reaps_done_tasks_before_refilling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    runtime = harness_worker.WorkerServices(
        project_root=tmp_path,
        state_dir=tmp_path,
        events=event_store,
        max_concurrent=2,
    )
    started = 0
    release_first_batch = asyncio.Event()
    first_batch_done = asyncio.Event()

    async def run_once(runtime: harness_worker.WorkerServices) -> str:
        nonlocal started
        del runtime
        started += 1
        if started <= 2:
            await release_first_batch.wait()
            if started == 2:
                first_batch_done.set()
            return f"ran {started}"
        stop_event.set()
        return "queue empty"

    async def exercise() -> None:
        try:
            worker = asyncio.create_task(
                harness_worker.run_forever(
                    runtime,
                    poll_interval_seconds=0,
                    stop_event=stop_event,
                )
            )
            while started < 2:
                await asyncio.sleep(0)
            release_first_batch.set()
            await asyncio.wait_for(first_batch_done.wait(), timeout=1)
            await asyncio.wait_for(worker, timeout=1)
        finally:
            await runtime.aclose()

    stop_event = asyncio.Event()
    monkeypatch.setattr(harness_worker, "run_once", run_once)

    asyncio.run(exercise())

    assert started == 4


def test_zeta_cli_serve_invokes_runtime_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    loops: dict[str, asyncio.AbstractEventLoop] = {}
    original_aclose = harness_worker.WorkerServices.aclose

    async def run_forever(runtime: harness_worker.WorkerServices) -> None:
        loops["run"] = asyncio.get_running_loop()
        captured["project_root"] = runtime.project_root

    async def aclose(runtime: harness_worker.WorkerServices) -> None:
        loops["close"] = asyncio.get_running_loop()
        await original_aclose(runtime)

    monkeypatch.setattr(harness_worker, "run_forever", run_forever)
    monkeypatch.setattr(harness_worker.WorkerServices, "aclose", aclose)

    result = CliRunner().invoke(
        cli_main.cli,
        ["serve", "--project-root", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert loops["run"] is loops["close"]
    assert captured == {"project_root": tmp_path.resolve()}


def test_zeta_cli_run_drains_available_queue_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / ".zeta"
    event_store = zeta_events.SqliteEventStore(event_store_path(state_dir))
    event_store.accept(
        zeta_events.DraftEvent("github.issue.opened", "github", {}, session_id="repo")
    )
    calls: list[harness_dispatch.AgentInvocation] = []

    async def run_agent(run: harness_dispatch.AgentInvocation) -> dict[str, object]:
        calls.append(run)
        return {"outcome": "handled"}

    def compile_agents(
        spec: object,
        **_kwargs: object,
    ) -> list[harness_dispatch.ExecutableAgent]:
        del spec
        return [
            harness_dispatch.ExecutableAgent(
                harness_dispatch.AgentDefinition(
                    "issue-triage",
                    (harness_dispatch.EventPattern("github.issue.opened"),),
                ),
                run=run_agent,
            )
        ]

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    write_project_event_schema(tmp_path, "github.issue.opened")
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
    monkeypatch.setattr(harness_worker, "compile_agent_definitions", compile_agents)

    result = CliRunner().invoke(
        cli_main.cli,
        [
            "run",
            "--project-root",
            str(tmp_path),
            "--state-dir",
            str(state_dir),
        ],
    )
    items = harness_queue.project_queue_items(
        event_store.list_events(zeta_events.Filter())
    )

    assert result.exit_code == 0
    assert result.output == "processed 1\n"
    assert len(calls) == 1
    assert [item.status for item in items] == ["completed"]


def test_zeta_agent_auto_enabled_capabilities_include_registered_tools() -> None:
    registry = CapabilityRegistry()
    registry.register(_test_capability("read", provider="host"))
    registry.register(
        _test_capability(
            "write",
            provider="rpc",
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
    captured_messages: list[list[dict[str, Any]]] = []

    async def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        request: object = None,
        **kwargs: object,
    ) -> dict[str, Any]:
        captured_messages.append(messages)
        return next(responses)

    monkeypatch.setattr(zeta_model_endpoint, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api,
        "chat_completion_messages",
        fake_chat_completion_messages,
    )

    result = run_agent_turn(
        "echo",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("ctx_echo",), max_turns=2),
        tool_registry=registry,
    )

    assert zeta_loop_types.tool_registry.get("ctx_echo") is None
    assert result.final_answer == "done"
    assert "- ctx_echo(text)" in captured_messages[0][0]["content"]
    assert "Available tools:\n(none)" not in captured_messages[0][0]["content"]
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

    async def fake_invoke(
        capability_id: str,
        params: dict[str, Any],
        **kwargs: object,
    ) -> dict[str, Any]:
        invoked.append((capability_id, params))
        return {"ok": True, "content": [{"type": "text", "text": "ok"}]}

    monkeypatch.setattr(zeta_model_endpoint, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api,
        "chat_completion_messages",
        _as_async(lambda *args, **kwargs: next(responses)),
    )
    monkeypatch.setattr(zeta_capability_executors, "invoke_capability", fake_invoke)

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

    async def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        request: object = None,
        **kwargs: object,
    ) -> dict[str, Any]:
        captured["kwargs"] = kwargs
        captured["request"] = request
        return {"content": "done"}

    monkeypatch.setattr(zeta_model_endpoint, "model_endpoint_open", lambda: True)
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

    assert captured["request"].thinking == "none"


def test_zeta_agent_event_omits_empty_reasoning() -> None:
    event = loop_model.model_event_payload({"content": "done", "reasoning_content": ""})

    assert "reasoning" not in event


def test_zeta_agent_tool_call_is_caused_by_assistant_event(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "README.md"
    target.write_text("hello\n", encoding="utf-8")
    store = zeta_trace.InMemoryStore()

    async def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        request: object = None,
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

    monkeypatch.setattr(zeta_model_endpoint, "model_endpoint_open", lambda: True)
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

    async def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        request: object = None,
        **kwargs: object,
    ) -> dict[str, Any]:
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        captured["request"] = request
        return {"content": "done"}

    monkeypatch.setattr(zeta_model_endpoint, "model_endpoint_open", lambda: True)
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

    async def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        request: object = None,
        **kwargs: object,
    ) -> dict[str, Any]:
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        captured["request"] = request
        return {"content": "done"}

    monkeypatch.setattr(zeta_model_endpoint, "model_endpoint_open", lambda: True)
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
    assert prompt.data["payload_address"] == zeta_context.payload_address(
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
    async def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        request: object = None,
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

    monkeypatch.setattr(zeta_model_endpoint, "model_endpoint_open", lambda: True)
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

    async def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        request: object = None,
        **kwargs: object,
    ) -> dict[str, Any]:
        del messages
        telemetry, response = next(responses)
        telemetry_sink = cast(
            "Callable[[dict[str, Any]], None]", kwargs["telemetry_sink"]
        )
        telemetry_sink(telemetry)
        return response

    monkeypatch.setattr(zeta_model_endpoint, "model_endpoint_open", lambda: True)
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

    monkeypatch.setattr(zeta_model_endpoint, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api,
        "chat_completion_messages",
        _as_async(lambda messages, request=None, **kwargs: next(responses)),
    )
    monkeypatch.setattr(
        zeta_capability_executors,
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

    monkeypatch.setattr(zeta_model_endpoint, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api,
        "chat_completion_messages",
        _as_async(lambda messages, request=None, **kwargs: next(responses)),
    )
    monkeypatch.setattr(
        zeta_capability_executors,
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

    async def fake_chat_completion_messages(
        *args: object,
        **kwargs: object,
    ) -> dict[str, Any]:
        del args
        stream_sink = required_stream_sink(kwargs)
        stream_sink.content_delta("hel")
        stream_sink.content_delta("lo")
        return {"content": "hello"}

    monkeypatch.setattr(zeta_model_endpoint, "model_endpoint_open", lambda: True)
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

    async def fake_chat_completion_messages(
        *args: object,
        **kwargs: object,
    ) -> dict[str, Any]:
        del args
        stream_sink = required_stream_sink(kwargs)
        stream_sink.reasoning_delta("mull")
        stream_sink.content_delta("done")
        return {"content": "done"}

    monkeypatch.setattr(zeta_model_endpoint, "model_endpoint_open", lambda: True)
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

    async def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        request: object = None,
        **kwargs: object,
    ) -> dict[str, Any]:
        captured.append(messages)
        stream_sink = required_stream_sink(kwargs)
        stream_sink.content_delta("streaming answer")
        return next(responses)

    monkeypatch.setattr(zeta_model_endpoint, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api,
        "chat_completion_messages",
        fake_chat_completion_messages,
    )
    monkeypatch.setattr(
        zeta_capability_executors,
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

    async def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        request: object = None,
        **kwargs: object,
    ) -> dict[str, Any]:
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        captured["request"] = request
        return {"content": "done"}

    monkeypatch.setattr(
        zeta_model_endpoint, "model_endpoint_open", fake_model_endpoint_open
    )
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
    assert captured["request"].model == "fast-model"
    assert captured["request"].url == "http://127.0.0.1:8081/v1/chat/completions"


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

    monkeypatch.setattr(zeta_model_endpoint, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api,
        "chat_completion_messages",
        _as_async(lambda *args, **kwargs: next(responses)),
    )

    async def fake_invoke(
        name: str, params: dict[str, Any], **kwargs: object
    ) -> dict[str, Any]:
        ran.append((name, params))
        return {"ok": True, "content": [{"type": "text", "text": name}]}

    monkeypatch.setattr(zeta_capability_executors, "invoke_capability", fake_invoke)

    result = run_agent_turn(
        "inspect",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("read", "ls"), max_turns=2),
        caused_by="prompt-event",
    )

    assert ran == [
        ("zeta.read", {"path": "README.md"}),
        ("zeta.ls", {"path": "src"}),
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

    async def fake_chat_completion_messages(
        *args: object,
        **kwargs: object,
    ) -> dict[str, Any]:
        del args
        response = next(responses)
        stream_sink = kwargs.get("stream_sink")
        if response.get("content") and stream_sink is not None:
            stream_sink = cast(zeta_model_sse.ChatCompletionStreamSink, stream_sink)
            stream_sink.content_delta(str(response["content"]))
        return response

    monkeypatch.setattr(zeta_model_endpoint, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api,
        "chat_completion_messages",
        fake_chat_completion_messages,
    )
    monkeypatch.setattr(
        zeta_capability_executors,
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

    async def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        request: object = None,
        **kwargs: object,
    ) -> dict[str, Any]:
        del kwargs
        captured["messages"] = messages
        return {"content": "done"}

    monkeypatch.setattr(zeta_model_endpoint, "model_endpoint_open", lambda: True)
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

    async def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        request: object = None,
        **kwargs: object,
    ) -> dict[str, Any]:
        del kwargs
        captured.append(messages)
        return next(responses)

    monkeypatch.setattr(zeta_model_endpoint, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api,
        "chat_completion_messages",
        fake_chat_completion_messages,
    )
    monkeypatch.setattr(
        zeta_capability_executors,
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

    monkeypatch.setattr(zeta_model_endpoint, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api,
        "chat_completion_messages",
        _as_async(
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
            }
        ),
    )

    async def fake_invoke(
        name: str, params: dict[str, Any], **kwargs: object
    ) -> dict[str, Any]:
        del name, params, kwargs
        assert [event.get("type") for event in timeline_events(streamed)] == [
            "model",
            "tool_call",
        ]
        return {"ok": True, "content": [{"type": "text", "text": "README"}]}

    monkeypatch.setattr(zeta_capability_executors, "invoke_capability", fake_invoke)

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


def test_zeta_agent_turn_stops_when_a_tool_requests_stop(monkeypatch) -> None:
    requests = 0
    store = zeta_trace.InMemoryStore()

    async def fake_chat_completion_messages(
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

    monkeypatch.setattr(zeta_model_endpoint, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api, "chat_completion_messages", fake_chat_completion_messages
    )
    monkeypatch.setattr(
        zeta_capability_executors,
        "invoke_capability",
        lambda name, params, **kwargs: {
            "ok": True,
            "stop": True,
        },
    )

    result = run_agent_turn(
        "test",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("bash",), max_turns=3),
        prompt_builder=zeta_context.PromptBuilder(store=store),
    )

    assert requests == 1
    assert result.stop_reason == "tool_stop"
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


def test_zeta_agent_turn_reports_max_turns_exhaustion(monkeypatch) -> None:
    registry = CapabilityRegistry()
    registry.register(_test_capability("inspect"))

    async def fake_chat_completion_messages(
        *args: object,
        **kwargs: object,
    ) -> dict[str, Any]:
        return {
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "inspect", "arguments": "{}"},
                }
            ]
        }

    monkeypatch.setattr(zeta_model_endpoint, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api,
        "chat_completion_messages",
        fake_chat_completion_messages,
    )

    result = run_agent_turn(
        "inspect",
        [],
        zeta_agent.AgentConfig(
            allowed_capabilities=("inspect",),
            max_turns=1,
        ),
        tool_registry=registry,
    )

    assert result.stop_reason == "max_turns"
    assert result.final_answer == ""
    assert [event["type"] for event in timeline_events(result.events)] == [
        "model",
        "tool_call",
        "tool_result",
    ]


def test_zeta_agent_continues_after_bash(monkeypatch) -> None:
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

    async def fake_chat_completion_messages(
        *args: object, **kwargs: object
    ) -> dict[str, Any]:
        nonlocal requests
        requests += 1
        return next(responses)

    monkeypatch.setattr(zeta_model_endpoint, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api, "chat_completion_messages", fake_chat_completion_messages
    )

    result = run_agent_turn(
        "test",
        [],
        zeta_agent.AgentConfig(
            allowed_capabilities=("bash",),
            max_turns=3,
        ),
    )

    assert requests == 2
    assert result.final_answer == "done"
    tool_result = next(
        event
        for event in timeline_events(result.events)
        if event.get("type") == "tool_result"
    )
    assert "direct-bash" in tool_result["result"]["content"][0]["text"]


def test_zeta_agent_turn_stops_after_default_max_turns(monkeypatch) -> None:
    requests = 0

    async def fake_chat_completion_messages(*args: object, **kwargs: object) -> dict:
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

    monkeypatch.setattr(zeta_model_endpoint, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api, "chat_completion_messages", fake_chat_completion_messages
    )
    monkeypatch.setattr(
        zeta_capability_executors,
        "invoke_capability",
        lambda name, params, **kwargs: {"ok": True},
    )

    result = run_agent_turn(
        "test",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("ls",)),
    )

    assert requests == zeta_loop_types.DEFAULT_MAX_TURNS
    assert result.final_answer == ""


def test_zeta_agent_turn_aborts_before_model_when_cancelled(monkeypatch) -> None:
    cancellation = threading.Event()
    cancellation.set()
    events: list[DraftEvent] = []

    def fail_chat_completion_messages(*args: object, **kwargs: object) -> dict:
        raise AssertionError("cancelled turn must not request the model")

    monkeypatch.setattr(zeta_model_endpoint, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api, "chat_completion_messages", fail_chat_completion_messages
    )

    with pytest.raises(zeta_loop_cancellation.AgentRunAborted) as raised:
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
    monkeypatch.setattr(zeta_model_endpoint, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api,
        "chat_completion_messages",
        _as_async(lambda *args, **kwargs: next(responses)),
    )
    monkeypatch.setattr(
        zeta_capability_executors,
        "invoke_capability",
        lambda name, params, **kwargs: read_tool_payload(target),
    )

    with pytest.raises(zeta_loop_cancellation.AgentRunAborted) as raised:
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

    async def fake_chat_completion_messages(*args: object, **kwargs: object) -> dict:
        del args, kwargs
        return next(responses)

    monkeypatch.setattr(zeta_model_endpoint, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api, "chat_completion_messages", fake_chat_completion_messages
    )
    monkeypatch.setattr(zeta_capability_executors, "invoke_capability", crash_invoke)

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


def test_zeta_agent_turn_rejects_tool_call_that_violates_input_schema(
    monkeypatch,
) -> None:
    ran_with: list[dict[str, Any]] = []

    async def fake_invoke(
        name: str,
        params: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        del name, kwargs
        ran_with.append(params)
        return {"ok": True}

    monkeypatch.setattr(zeta_model_endpoint, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api,
        "chat_completion_messages",
        _as_async(
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
            }
        ),
    )
    monkeypatch.setattr(zeta_capability_executors, "invoke_capability", fake_invoke)

    result = run_agent_turn(
        "inspect",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("read",), max_turns=1),
    )

    assert ran_with == []
    tool_result = next(
        event
        for event in timeline_events(result.events)
        if event.get("type") == "tool_result"
    )
    assert tool_result["result"]["ok"] is False
    assert tool_result["result"]["error"]["code"] == "invalid-tool-args"
    assert tool_result["status"] == "failed"


def test_zeta_agent_turn_rejects_disallowed_tool_before_running(monkeypatch) -> None:
    ran = False

    def fail_invoke(name: str, params: dict[str, Any]) -> dict[str, Any]:
        nonlocal ran
        ran = True
        return {"ok": True}

    monkeypatch.setattr(zeta_model_endpoint, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api,
        "chat_completion_messages",
        _as_async(
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
            }
        ),
    )
    monkeypatch.setattr(zeta_capability_executors, "invoke_capability", fail_invoke)

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

    async def fake_chat_completion_messages(
        *args: object,
        **kwargs: object,
    ) -> dict[str, Any]:
        nonlocal requests
        requests += 1
        return next(responses)

    monkeypatch.setattr(zeta_model_endpoint, "model_endpoint_open", lambda: True)
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
            max_turns=3,
        ),
    )

    assert requests == 2
    assert result.final_answer == "done"
    assert target.read_text(encoding="utf-8") == "new\n"


def test_zeta_agent_codex_api_skips_endpoint_probe(monkeypatch) -> None:
    def fail_probe(url: str | None = None) -> bool:
        raise AssertionError("codex profiles must not probe a local endpoint")

    monkeypatch.setattr(zeta_model_endpoint, "model_endpoint_open", fail_probe)

    config = zeta_agent.AgentConfig(model_api="codex-responses")

    assert zeta_loop_gateway.agent_model_endpoint_open(config) is True


def test_zeta_agent_turn_passes_api_to_the_model(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        request: object = None,
        **kwargs: object,
    ) -> dict[str, Any]:
        captured.update(kwargs)
        captured["request"] = request
        return {"content": "done"}

    monkeypatch.setattr(zeta_model_endpoint, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_models_api, "chat_completion_messages", fake_chat_completion_messages
    )

    run_agent_turn(
        "answer",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("read",), max_turns=1),
    )

    assert captured["request"].api is None
