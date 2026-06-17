"""Newline-delimited JSON-RPC transport for the Zeta runtime."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TextIO, cast

from .agent import AgentConfig, AgentTurnAborted, registered_tools, run_agent_turn
from .context import ZetaContext, default_context, load_project_context
from .events import Event, EventCursor, EventReader, Filter
from .timeline import current_timeline, record_event, timeline_event_from_durable_event
from .tools.base import EFFECT_KINDS, EffectKind, ToolImpl, ToolSpec
from .tools.registry import ExecutionMode, ToolRegistry
from .tools.registry import registry as _runtime_tool_registry

RpcSessionRunner = Callable[[dict[str, Any]], dict[str, Any]]


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
    run_id = rpc_run_id()
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
        )
    except AgentTurnAborted:
        return rpc_session_result(
            "aborted",
            "",
            run_id=run_id,
            runtime_context=runtime_context,
        )
    return rpc_session_result(
        rpc_outcome(result.staged_effect, result.final_text),
        result.final_text,
        run_id=run_id,
        runtime_context=runtime_context,
    )


def rpc_run_id() -> str:
    return f"run_{uuid.uuid4().hex}"


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
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "run_id": run_id,
        "outcome": outcome,
        "final_text": final_text,
    }
    cursor = final_event_cursor(runtime_context, run_id)
    if cursor is not None:
        result["final_event_cursor"] = cursor
    return result


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
        self.client_tools: set[str] = set()

    def serve(self) -> None:
        while (message := self.read_message()) is not None:
            self.handle_message(message)

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

    def handle_message(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        method = str(message.get("method") or "")
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        try:
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
            if name not in self.client_tools:
                self.register_client_tool(name, item)
            registered.append(name)
        return registered

    def register_client_tool(self, name: str, item: dict[str, Any]) -> None:
        existing = self.tool_registry.get(name)
        if existing is not None and name not in self.client_tools:
            raise RpcError(
                -32602,
                "duplicate_tool",
                "Invalid params",
                {
                    "message": f"tool {name!r} is already registered",
                    "tool": name,
                },
            )
        self.client_tools.add(name)
        if existing is not None:
            return
        schema = item.get("schema")
        schema = schema if isinstance(schema, dict) else {"type": "object"}
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
        )

        def run(params: dict[str, Any]) -> dict[str, Any]:
            return self.call_client_tool(str(uuid.uuid4()), name, params)

        self.tool_registry.register(name, ToolImpl(spec, run, run))

    def record_tool_response(self, params: dict[str, Any]) -> None:
        call_id = str(params.get("id") or "")
        if not call_id:
            return
        result = params.get("result")
        if not isinstance(result, dict):
            result = {"ok": False, "error": {"code": "invalid-result"}}
        self.tool_responses[call_id] = cast(dict[str, Any], result)

    def call_client_tool(
        self,
        call_id: str,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        self.write_notification(
            "tools.call",
            {"id": call_id, "name": name, "arguments": arguments},
        )
        while call_id not in self.tool_responses:
            message = self.read_message()
            if message is None:
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
        self.output.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.output.flush()
