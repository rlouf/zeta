"""Newline-delimited JSON-RPC transport for the Zeta runtime."""

from __future__ import annotations

import json
import select
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, TextIO, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .agent import (
    AgentConfig,
    AgentTurnAborted,
    AgentTurnResult,
    registered_tools,
    run_agent_turn,
)
from .context import ZetaContext, default_context, load_project_context
from .events import Event, EventCursor, EventReader, Filter
from .timeline import current_timeline, record_event, timeline_event_from_durable_event
from .tools.base import EFFECT_KINDS, EffectKind, ToolImpl, ToolSpec, error_result
from .tools.registry import ExecutionMode, ToolRegistry
from .tools.registry import registry as _runtime_tool_registry

RpcSessionRunner = Callable[[dict[str, Any]], dict[str, Any]]
ToolCallStatus = Literal["requested", "responded", "failed", "cancelled", "timed_out"]
RpcRunStatus = Literal["running", "cancelling", "completed", "cancelled", "failed"]
READ_TIMEOUT = object()
RPC_RUN_ID_PARAM = "_zeta_run_id"
RPC_CANCELLATION_EVENT_PARAM = "_zeta_cancellation_event"


@dataclass
class ClientToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    status: ToolCallStatus = "requested"
    timeout_sec: float | None = None


@dataclass
class RpcRunState:
    run_id: str
    request_id: Any
    cancellation_event: threading.Event
    status: RpcRunStatus = "running"


@dataclass(frozen=True)
class EventSubscription:
    id: str
    after: EventCursor | None = None
    session_id: str | None = None
    run_id: str | None = None


@dataclass
class RpcError(RuntimeError):
    jsonrpc_code: int
    zeta_code: str
    summary: str
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.summary)

    def error_data(self) -> dict[str, Any]:
        return {"code": self.zeta_code, **self.data}


def run_rpc_session(
    params: dict[str, Any],
    *,
    publish_event: Callable[[dict[str, Any]], None],
    runtime_context: ZetaContext | None = None,
) -> dict[str, Any]:
    runtime_context = runtime_context or default_context()
    run_id = rpc_run_id_param(params) or rpc_run_id()
    cancellation_event = rpc_cancellation_event_param(params)
    objective = rpc_objective(params)
    workflow = rpc_workflow(params)
    enabled_tools = registered_tools(
        rpc_allowed_tools(params),
        tool_registry=runtime_context.tool_registry,
    )
    execution_mode: ExecutionMode = "direct" if workflow == "do" else "stage"
    prior_timeline = current_timeline(runtime_context=runtime_context)
    user_event = record_event(
        {
            "type": "user_message",
            "content": objective,
            "workflow": workflow,
            "runtime": "zeta-rpc",
            "available_tools": list(enabled_tools),
            "run_id": run_id,
            "turn_id": run_id,
        },
        runtime_context=runtime_context,
    )
    publish_event(rpc_event_with_cursor(runtime_context, user_event, run_id))

    def sink(event: dict[str, Any]) -> None:
        persisted = record_event(
            rpc_event_with_run_id(event, run_id),
            runtime_context=runtime_context,
        )
        publish_event(rpc_event_with_cursor(runtime_context, persisted, run_id))

    try:
        result = run_agent_turn(
            objective,
            prior_timeline,
            rpc_agent_config(
                params,
                enabled_tools=enabled_tools,
                execution_mode=execution_mode,
                session_id=runtime_context.session_id,
            ),
            context=rpc_context(params),
            event_sink=sink,
            trace_store=runtime_context.trace_store,
            tool_registry=runtime_context.tool_registry,
            caused_by=str(user_event.get("id") or ""),
            cancellation_event=cancellation_event,
        )
    except AgentTurnAborted as exc:
        return rpc_session_result(
            "aborted",
            "",
            run_id=run_id,
            runtime_context=runtime_context,
            agent_result=exc.result,
        )
    return rpc_session_result(
        rpc_outcome(result.staged_effect, result.final_text),
        result.final_text,
        run_id=run_id,
        runtime_context=runtime_context,
        agent_result=result,
    )


def rpc_run_id() -> str:
    return f"run_{uuid.uuid4().hex}"


def rpc_run_id_param(params: dict[str, Any]) -> str | None:
    value = params.get(RPC_RUN_ID_PARAM)
    return value if isinstance(value, str) and value else None


def rpc_cancellation_event_param(params: dict[str, Any]) -> threading.Event | None:
    value = params.get(RPC_CANCELLATION_EVENT_PARAM)
    if hasattr(value, "is_set") and hasattr(value, "set"):
        return cast(threading.Event, value)
    return None


def rpc_event_with_run_id(event: dict[str, Any], run_id: str) -> dict[str, Any]:
    scoped = dict(event)
    scoped["run_id"] = run_id
    scoped["turn_id"] = run_id
    return scoped


def rpc_session_result(
    outcome: str,
    final_text: str,
    *,
    run_id: str,
    runtime_context: ZetaContext,
    agent_result: AgentTurnResult | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "run_id": run_id,
        "outcome": outcome,
        "final_text": final_text,
        "trace": rpc_trace_result(agent_result),
    }
    cursor = final_event_cursor(runtime_context, run_id)
    if cursor is not None:
        result["final_event_cursor"] = cursor
    return result


def rpc_trace_result(agent_result: AgentTurnResult | None) -> dict[str, list[str]]:
    trace = empty_rpc_trace_result()
    if agent_result is None:
        return trace
    for prompt_trace in agent_result.prompt_traces:
        add_unique(trace["prompt_ids"], prompt_trace.prompt_object_id)
        add_unique(
            trace["assistant_message_ids"],
            prompt_trace.assistant_message_object_id,
        )
    for event in agent_result.events:
        event_type = str(event.get("type") or "")
        if event_type == "model":
            add_unique(trace["model_event_ids"], event.get("id"))
            add_unique_list(trace["tool_call_ids"], event.get("tool_call_object_ids"))
            continue
        if event_type == "tool_call":
            add_unique(trace["tool_event_ids"], event.get("id"))
            add_unique(trace["tool_call_ids"], event.get("tool_call_object_id"))
            continue
        if event_type == "tool_result":
            add_unique(trace["tool_event_ids"], event.get("id"))
            add_unique(trace["tool_call_ids"], event.get("tool_call_object_id"))
            add_unique(trace["tool_result_ids"], event.get("tool_result_object_id"))
    return trace


def empty_rpc_trace_result() -> dict[str, list[str]]:
    return {
        "prompt_ids": [],
        "assistant_message_ids": [],
        "model_event_ids": [],
        "tool_event_ids": [],
        "tool_call_ids": [],
        "tool_result_ids": [],
    }


def add_unique(values: list[str], value: Any) -> None:
    if isinstance(value, str) and value and value not in values:
        values.append(value)


def add_unique_list(values: list[str], raw_values: Any) -> None:
    if not isinstance(raw_values, list | tuple):
        return
    for value in raw_values:
        add_unique(values, value)


def final_event_cursor(runtime_context: ZetaContext, run_id: str) -> str | None:
    if not isinstance(runtime_context.event_sink, EventReader):
        return None
    events = runtime_context.event_sink.list_events(
        Filter(session_id=runtime_context.session_id, turn_id=run_id)
    )
    if not events:
        return None
    return events[-1].cursor().encode()


def rpc_event_with_cursor(
    runtime_context: ZetaContext,
    event: dict[str, Any],
    run_id: str,
) -> dict[str, Any]:
    durable_event = durable_event_for_rpc_event(runtime_context, event, run_id)
    if durable_event is None:
        return event
    return rpc_event_from_durable_event(durable_event)


def durable_event_for_rpc_event(
    runtime_context: ZetaContext,
    event: dict[str, Any],
    run_id: str,
) -> Event | None:
    if not isinstance(runtime_context.event_sink, EventReader):
        return None
    event_id = event.get("id")
    if not isinstance(event_id, str):
        return None
    events = runtime_context.event_sink.list_events(
        Filter(session_id=runtime_context.session_id, turn_id=run_id)
    )
    for durable_event in events:
        if durable_event.id == event_id:
            return durable_event
    return None


def rpc_event_from_durable_event(event: Event) -> dict[str, Any]:
    projected = timeline_event_from_durable_event(event)
    projected["cursor"] = event.cursor().encode()
    return projected


def rpc_objective(params: dict[str, Any]) -> str:
    objective = str(params.get("objective") or "")
    if not objective:
        raise RpcError(
            -32602,
            "missing_objective",
            "Invalid params",
            {"message": "session.run requires objective"},
        )
    return objective


def rpc_workflow(params: dict[str, Any]) -> str:
    workflow = str(params.get("workflow") or "ask")
    if workflow not in {"ask", "propose", "do"}:
        raise RpcError(
            -32602,
            "invalid_workflow",
            "Invalid params",
            {
                "message": "workflow must be ask, propose, or do",
                "workflow": workflow,
            },
        )
    return workflow


def rpc_allowed_tools(params: dict[str, Any]) -> tuple[str, ...] | None:
    requested_tools = params.get("tools")
    if not isinstance(requested_tools, list):
        return None
    return tuple(str(tool) for tool in requested_tools if isinstance(tool, str))


def rpc_agent_config(
    params: dict[str, Any],
    *,
    enabled_tools: tuple[str, ...],
    execution_mode: ExecutionMode,
    session_id: str,
) -> AgentConfig:
    return AgentConfig(
        system_prompt=optional_str_param(params, "system"),
        allowed_tools=enabled_tools,
        max_turns=params.get("max_steps")
        if isinstance(params.get("max_steps"), int)
        else None,
        stop_on_staged_effect=True,
        execution_mode=execution_mode,
        model_name=optional_str_param(params, "model"),
        model_url=optional_str_param(params, "url"),
        model_session_id=session_id,
        thinking=optional_str_param(params, "thinking"),
        model_api=optional_str_param(params, "api"),
        max_wall_seconds=optional_float_param(params, "max_wall_seconds"),
    )


def rpc_context(params: dict[str, Any]) -> str:
    context = params.get("context")
    return str(context) if isinstance(context, str) else load_project_context()


def optional_str_param(params: dict[str, Any], key: str) -> str | None:
    value = params.get(key)
    return value if isinstance(value, str) else None


def optional_float_param(params: dict[str, Any], key: str) -> float | None:
    value = params.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def client_tool_timeout_sec(item: dict[str, Any]) -> float | None:
    value = item.get("timeout_sec")
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float) and value > 0:
        return float(value)
    return None


def normalized_client_tool_response(raw_result: Any) -> tuple[dict[str, Any], ToolCallStatus]:
    if not isinstance(raw_result, dict):
        return (
            error_result(
                "invalid-tool-response",
                "tool response result must be an object",
            ),
            "failed",
        )
    if not isinstance(raw_result.get("ok"), bool):
        return (
            error_result(
                "invalid-tool-response",
                "tool response result must include boolean ok",
            ),
            "failed",
        )
    return cast(dict[str, Any], raw_result), "responded"


def event_cursor_param(value: Any) -> EventCursor | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RpcError(
            -32602,
            "invalid_cursor",
            "Invalid params",
            {"message": "after must be an event cursor string"},
        )
    cursor = EventCursor.decode(value)
    if cursor is None:
        raise RpcError(
            -32602,
            "invalid_cursor",
            "Invalid params",
            {"message": "after must be an event cursor string"},
        )
    return cursor


def positive_limit_param(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RpcError(
            -32602,
            "invalid_limit",
            "Invalid params",
            {"message": "limit must be a positive integer"},
        )
    return value


def optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def rpc_outcome(staged_effect: dict[str, Any] | None, final_text: str) -> str:
    if staged_effect is not None:
        return "staged"
    if final_text:
        return "answered"
    return "failed"


def event_matches_subscription(
    event: dict[str, Any],
    subscription: EventSubscription,
) -> bool:
    if subscription.session_id is not None and event.get("session") != subscription.session_id:
        return False
    if subscription.run_id is not None and event.get("run_id") != subscription.run_id:
        return False
    if subscription.after is None:
        return True
    cursor = EventCursor.decode(str(event.get("cursor") or ""))
    return cursor is not None and cursor.seq > subscription.after.seq


class JsonRpcServer:
    def __init__(
        self,
        input: TextIO,
        output: TextIO,
        *,
        session_runner: RpcSessionRunner | None = None,
        tool_registry: ToolRegistry | None = None,
        event_reader: EventReader | None = None,
    ) -> None:
        self.input = input
        self.output = output
        self.session_runner = session_runner
        self.tool_registry = tool_registry or _runtime_tool_registry
        self.event_reader = event_reader
        self.tool_responses: dict[str, dict[str, Any]] = {}
        self.tool_calls: dict[str, ClientToolCall] = {}
        self.client_tools: set[str] = set()
        self.runs: dict[str, RpcRunState] = {}
        self.run_workers: list[threading.Thread] = []
        self.runs_lock = threading.Lock()
        self.output_lock = threading.Lock()
        self.event_subscriptions: dict[str, EventSubscription] = {}

    def serve(self) -> None:
        try:
            while (message := self.read_message()) is not None:
                self.handle_message(message)
        finally:
            self.join_session_runs()

    def read_message(self) -> dict[str, Any] | None:
        for line in self.input:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                self.write_error(None, -32700, str(exc))
                continue
            if isinstance(message, dict):
                return cast(dict[str, Any], message)
            self.write_error(None, -32600, "JSON-RPC message must be an object")
        return None

    def read_message_before(self, deadline: float | None) -> dict[str, Any] | object | None:
        if deadline is None:
            return self.read_message()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return READ_TIMEOUT
        if not self.input_ready_before(remaining):
            return READ_TIMEOUT
        message = self.read_message()
        if message is None and time.monotonic() >= deadline:
            return READ_TIMEOUT
        return message

    def input_ready_before(self, timeout_sec: float) -> bool:
        try:
            self.input.fileno()
        except (AttributeError, OSError, ValueError):
            return True
        try:
            readable, _, _ = select.select([self.input], [], [], timeout_sec)
        except (OSError, ValueError):
            return True
        return bool(readable)

    def handle_message(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        method = str(message.get("method") or "")
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        try:
            if (
                method == "session.run"
                and request_id is not None
                and self.session_runner is not None
            ):
                self.start_session_run(request_id, params)
                return
            result = self.dispatch(method, params)
        except RpcError as exc:
            if request_id is not None:
                self.write_error(
                    request_id,
                    exc.jsonrpc_code,
                    exc.summary,
                    data=exc.error_data(),
                )
            return
        except Exception as exc:
            if request_id is not None:
                self.write_error(
                    request_id,
                    -32603,
                    "Internal error",
                    data={
                        "code": "internal_error",
                        "message": f"{type(exc).__name__}: {exc}",
                    },
                )
            return
        if request_id is not None:
            self.write_response(request_id, result)

    def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if method == "initialize":
            return {"server": "zeta", "protocol": "0.1"}
        if method == "tools.register":
            return {"registered": self.register_client_tools(params.get("tools"))}
        if method == "tools.respond":
            self.record_tool_response(params)
            return None
        if method == "events.list":
            return self.list_events(params)
        if method == "events.subscribe":
            return self.subscribe_events(params)
        if method == "session.cancel":
            return self.cancel_session(params)
        if method == "session.run":
            if self.session_runner is None:
                raise RpcError(
                    -32000,
                    "session_run_unavailable",
                    "Server error",
                    {"message": "session.run is not configured"},
                )
            return self.session_runner(params)
        raise RpcError(
            -32601,
            "method_not_found",
            "Method not found",
            {"method": method},
        )

    def list_events(self, params: dict[str, Any]) -> dict[str, Any]:
        if self.event_reader is None:
            raise RpcError(
                -32000,
                "events_unavailable",
                "Server error",
                {"message": "events.list is not configured"},
            )
        after = event_cursor_param(params.get("after"))
        limit = positive_limit_param(params.get("limit"))
        session_id = optional_string(params.get("session_id"))
        run_id = optional_string(params.get("run_id"))
        events = self.event_reader.list_events(
            Filter(session_id=session_id, turn_id=run_id, after=after, limit=limit)
        )
        next_cursor = events[-1].cursor().encode() if events else (
            after.encode() if after is not None else None
        )
        return {
            "events": [rpc_event_from_durable_event(event) for event in events],
            "next_cursor": next_cursor,
        }

    def subscribe_events(self, params: dict[str, Any]) -> dict[str, Any]:
        after = event_cursor_param(params.get("after"))
        subscription = EventSubscription(
            id=f"sub_{uuid.uuid4().hex}",
            after=after,
            session_id=optional_string(params.get("session_id")),
            run_id=optional_string(params.get("run_id")),
        )
        self.event_subscriptions[subscription.id] = subscription
        return {
            "subscribed": True,
            "subscription_id": subscription.id,
            "next_cursor": after.encode() if after is not None else None,
        }

    def start_session_run(self, request_id: Any, params: dict[str, Any]) -> None:
        if self.session_runner is None:
            raise RpcError(
                -32000,
                "session_run_unavailable",
                "Server error",
                {"message": "session.run is not configured"},
            )
        run_id = rpc_run_id()
        cancellation_event = threading.Event()
        state = RpcRunState(
            run_id=run_id,
            request_id=request_id,
            cancellation_event=cancellation_event,
        )
        with self.runs_lock:
            self.runs[run_id] = state
        worker_params = {
            **params,
            RPC_RUN_ID_PARAM: run_id,
            RPC_CANCELLATION_EVENT_PARAM: cancellation_event,
        }
        worker = threading.Thread(
            target=self.complete_session_run,
            args=(state, worker_params),
        )
        self.run_workers.append(worker)
        worker.start()

    def complete_session_run(
        self,
        state: RpcRunState,
        params: dict[str, Any],
    ) -> None:
        try:
            assert self.session_runner is not None
            result = self.session_runner(params)
        except RpcError as exc:
            state.status = "failed"
            self.write_error(
                state.request_id,
                exc.jsonrpc_code,
                exc.summary,
                data=exc.error_data(),
            )
            return
        except Exception as exc:
            state.status = "failed"
            self.write_error(
                state.request_id,
                -32603,
                "Internal error",
                data={
                    "code": "internal_error",
                    "message": f"{type(exc).__name__}: {exc}",
                },
            )
            return
        state.status = (
            "cancelled"
            if state.cancellation_event.is_set()
            and result.get("outcome") == "aborted"
            else "completed"
        )
        self.write_response(state.request_id, result)

    def join_session_runs(self) -> None:
        for worker in self.run_workers:
            worker.join()

    def cancel_session(self, params: dict[str, Any]) -> dict[str, Any]:
        run_id = optional_string(params.get("run_id"))
        if run_id is None:
            raise RpcError(
                -32602,
                "invalid_run_id",
                "Invalid params",
                {"message": "run_id must be a non-empty string"},
            )
        with self.runs_lock:
            state = self.runs.get(run_id)
            if state is None:
                return {"cancelled": False, "run_id": run_id, "status": "unknown"}
            if state.status not in {"running", "cancelling"}:
                return {
                    "cancelled": False,
                    "run_id": run_id,
                    "status": state.status,
                }
            state.status = "cancelling"
            state.cancellation_event.set()
        return {"cancelled": True, "run_id": run_id}

    def register_client_tools(self, tools: Any) -> list[str]:
        if not isinstance(tools, list):
            return []
        registered = []
        for item in tools:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if not name:
                continue
            self.register_client_tool(name, item)
            registered.append(name)
        return registered

    def register_client_tool(self, name: str, item: dict[str, Any]) -> None:
        existing = self.tool_registry.get(name)
        if existing is not None:
            raise RpcError(
                -32602,
                "duplicate_tool",
                "Invalid params",
                {
                    "message": f"tool {name!r} is already registered",
                    "tool": name,
                },
            )
        schema = item.get("schema")
        schema = schema if isinstance(schema, dict) else {"type": "object"}
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise RpcError(
                -32602,
                "invalid_tool_schema",
                "Invalid params",
                {
                    "message": exc.message,
                    "tool": name,
                },
            ) from exc
        raw_effects = item.get("effects")
        effects = (
            tuple(
                cast(EffectKind, effect)
                for effect in raw_effects
                if isinstance(effect, str) and effect in EFFECT_KINDS
            )
            if isinstance(raw_effects, list)
            else ()
        )
        spec = ToolSpec(
            name,
            str(item.get("description") or ""),
            schema,
            interactive=True,
            effects=effects,
            staging_supported=item.get("staging_supported") is True,
            direct_execution_allowed=item.get("direct_execution_allowed") is True,
            timeout_sec=client_tool_timeout_sec(item),
        )

        def run(params: dict[str, Any]) -> dict[str, Any]:
            return self.call_client_tool(
                str(uuid.uuid4()),
                name,
                params,
                timeout_sec=spec.timeout_sec,
            )

        stage = run if spec.staging_supported else None
        self.tool_registry.register(name, ToolImpl(spec, run, stage))
        self.client_tools.add(name)

    def record_tool_response(self, params: dict[str, Any]) -> None:
        call_id = str(params.get("id") or "")
        if not call_id:
            return
        if params.get("cancelled") is True:
            result = error_result(
                "client-cancelled",
                f"client cancelled tool call {call_id}",
            )
            status: ToolCallStatus = "cancelled"
        else:
            result, status = normalized_client_tool_response(params.get("result"))
        call = self.tool_calls.get(call_id)
        if call is not None:
            call.status = status
        self.tool_responses[call_id] = result

    def call_client_tool(
        self,
        call_id: str,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout_sec: float | None = None,
    ) -> dict[str, Any]:
        self.tool_calls[call_id] = ClientToolCall(
            id=call_id,
            name=name,
            arguments=arguments,
            timeout_sec=timeout_sec,
        )
        params: dict[str, Any] = {
            "id": call_id,
            "name": name,
            "arguments": arguments,
            "status": "requested",
        }
        if timeout_sec is not None:
            params["timeout_sec"] = timeout_sec
        self.write_notification(
            "tools.call",
            params,
        )
        deadline = time.monotonic() + timeout_sec if timeout_sec is not None else None
        while call_id not in self.tool_responses:
            message = self.read_message_before(deadline)
            if message is READ_TIMEOUT:
                self.tool_calls[call_id].status = "timed_out"
                return error_result(
                    "client-tool-timeout",
                    f"client tool {name} timed out after {timeout_sec:g}s",
                )
            if message is None:
                self.tool_calls[call_id].status = "failed"
                return {
                    "ok": False,
                    "error": {"code": "client-disconnected", "message": name},
                }
            if str(message.get("method") or "") == "tools.respond":
                params = message.get("params")
                if isinstance(params, dict):
                    self.record_tool_response(params)
                continue
            self.handle_message(message)
        return self.tool_responses.pop(call_id)

    def publish_event(self, event: dict[str, Any]) -> None:
        if self.event_subscriptions:
            for subscription in self.event_subscriptions.values():
                if event_matches_subscription(event, subscription):
                    self.write_notification(
                        "events.publish",
                        {"subscription_id": subscription.id, "event": event},
                    )
            return
        self.write_notification("events.publish", {"event": event})

    def write_response(self, request_id: Any, result: Any) -> None:
        self.write_message({"jsonrpc": "2.0", "id": request_id, "result": result})

    def write_error(
        self,
        request_id: Any,
        code: int,
        message: str,
        *,
        data: dict[str, Any] | None = None,
    ) -> None:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        self.write_message({"jsonrpc": "2.0", "id": request_id, "error": error})

    def write_notification(self, method: str, params: dict[str, Any]) -> None:
        self.write_message({"jsonrpc": "2.0", "method": method, "params": params})

    def write_message(self, message: dict[str, Any]) -> None:
        with self.output_lock:
            self.output.write(json.dumps(message, separators=(",", ":")) + "\n")
            self.output.flush()
