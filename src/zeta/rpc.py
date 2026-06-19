"""Newline-delimited JSON-RPC transport for the Zeta runtime."""

import asyncio
import json
import os
import select
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TextIO, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from zeta.capabilities.base import (
    EFFECT_KINDS,
    READ_ONLY_EFFECT_KINDS,
    Capability,
    CapabilityId,
    CapabilityPolicy,
    CapabilityResult,
    CapabilitySpec,
    EffectKind,
    ExecutionMode,
    error_result,
)
from zeta.capabilities.registry import CapabilityRegistry
from zeta.capabilities.registry import registry as _runtime_tool_registry
from zeta.dispatch import AsyncEventDispatcher
from zeta.events import (
    DraftEvent,
    EventSink,
    boundary_event_draft,
    event_view,
)
from zeta.session import (
    Session,
    SessionRequestError,
    default_session,
    empty_session_trace_result,
    session_event_dispatcher,
    session_run_id,
    session_turn_requested_draft,
)
from zeta.store.events import EventReader, Filter

RpcSessionRunner = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
ToolCallStatus = Literal["requested", "responded", "failed", "cancelled", "timed_out"]
RpcRunStatus = Literal["running", "cancelling", "completed", "cancelled", "failed"]
RpcCancelMode = Literal["cooperative", "task"]
READ_TIMEOUT = object()
RPC_RUN_ID_PARAM = "_zeta_run_id"
RPC_CANCELLATION_EVENT_PARAM = "_zeta_cancellation_event"


class RpcCancellation(Protocol):
    def is_set(self) -> bool: ...

    def set(self) -> Any: ...


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
    cancellation_event: asyncio.Event
    task: asyncio.Task[None] | None = None
    status: RpcRunStatus = "running"
    cancel_mode: RpcCancelMode = "cooperative"


@dataclass(frozen=True)
class EventSubscription:
    id: str
    after_seq: int | None = None
    session_id: str | None = None
    run_id: str | None = None


@dataclass(frozen=True)
class EventListParams:
    after_seq: int | None = None
    limit: int | None = None
    session_id: str | None = None
    run_id: str | None = None

    @classmethod
    def from_mapping(cls, params: dict[str, Any]) -> "EventListParams":
        return cls(
            after_seq=event_cursor(params.get("after")),
            limit=positive_limit(params.get("limit")),
            session_id=optional_string(params.get("session_id")),
            run_id=optional_string(params.get("run_id")),
        )


@dataclass(frozen=True)
class EventSubscribeParams:
    after_seq: int | None = None
    session_id: str | None = None
    run_id: str | None = None

    @classmethod
    def from_mapping(cls, params: dict[str, Any]) -> "EventSubscribeParams":
        return cls(
            after_seq=event_cursor(params.get("after")),
            session_id=optional_string(params.get("session_id")),
            run_id=optional_string(params.get("run_id")),
        )

    def subscription(self) -> EventSubscription:
        return EventSubscription(
            id=f"sub_{uuid.uuid4().hex}",
            after_seq=self.after_seq,
            session_id=self.session_id,
            run_id=self.run_id,
        )


@dataclass(frozen=True)
class SessionCancelParams:
    run_id: str

    @classmethod
    def from_mapping(cls, params: dict[str, Any]) -> "SessionCancelParams":
        run_id = optional_string(params.get("run_id"))
        if run_id is None:
            raise RpcError(
                -32602,
                "invalid_run_id",
                "Invalid params",
                {"message": "run_id must be a non-empty string"},
            )
        return cls(run_id)


def rpc_session_runner_cancel_mode(runner: RpcSessionRunner) -> RpcCancelMode:
    target = getattr(runner, "func", runner)
    mode = getattr(target, "__rpc_cancel_mode__", None)
    if mode in ("cooperative", "task"):
        return cast(RpcCancelMode, mode)
    return "task"


@dataclass(frozen=True)
class RpcClientCapabilityExecutor:
    call_client_tool: Callable[..., dict[str, Any]]
    name: str
    timeout_seconds: float | None = None

    def invoke(
        self,
        capability: CapabilitySpec,
        params: dict[str, Any],
        *,
        mode: ExecutionMode,
    ) -> CapabilityResult:
        del capability, mode
        return CapabilityResult.from_mapping(
            self.call_client_tool(
                str(uuid.uuid4()),
                self.name,
                params,
                timeout_sec=self.timeout_seconds,
            )
        )


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


async def run_rpc_session(
    params: dict[str, Any],
    *,
    publish_event: Callable[[dict[str, Any]], None],
    runtime_context: Session | None = None,
) -> dict[str, Any]:
    runtime_context = runtime_context or default_session()
    run_id = rpc_run_id_param(params) or session_run_id()
    cancellation_event = rpc_cancellation_event_param(params)
    try:
        draft = session_turn_requested_draft(
            params,
            run_id=run_id,
            runtime_context=runtime_context,
        )
        dispatcher = session_event_dispatcher(
            runtime_context,
            publish_event=publish_event,
            cancellation_event=cancellation_event,
        )
    except SessionRequestError as exc:
        raise rpc_error_from_session_request(exc) from exc
    outcome = await dispatcher.dispatch(draft)
    if outcome.agent_results:
        return outcome.agent_results[0]
    return {
        "run_id": run_id,
        "outcome": "duplicate" if not outcome.inserted else "unhandled",
        "final_text": "",
        "trace": empty_session_trace_result(),
    }


def rpc_run_id() -> str:
    return session_run_id()


def rpc_error_from_session_request(exc: SessionRequestError) -> RpcError:
    return RpcError(-32602, exc.code, "Invalid params", exc.data)


def rpc_run_id_param(params: dict[str, Any]) -> str | None:
    value = params.get(RPC_RUN_ID_PARAM)
    return value if isinstance(value, str) and value else None


def rpc_cancellation_event_param(params: dict[str, Any]) -> RpcCancellation | None:
    value = params.get(RPC_CANCELLATION_EVENT_PARAM)
    if hasattr(value, "is_set") and hasattr(value, "set"):
        return cast(RpcCancellation, value)
    return None


def rpc_event_with_run_id(event: dict[str, Any], run_id: str) -> dict[str, Any]:
    scoped = dict(event)
    scoped["run_id"] = run_id
    scoped["turn_id"] = run_id
    return scoped


def client_tool_timeout_sec(item: dict[str, Any]) -> float | None:
    value = item.get("timeout_sec")
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float) and value > 0:
        return float(value)
    return None


def client_tool_provider(item: dict[str, Any], name: str) -> str:
    provider = item.get("provider")
    if provider is None:
        return "rpc"
    if provider == "rpc":
        return "rpc"
    raise RpcError(
        -32602,
        "invalid_tool_provider",
        "Invalid params",
        {
            "message": "client tools must use the rpc provider namespace",
            "tool": name,
        },
    )


def client_tool_schema(item: dict[str, Any], name: str) -> dict[str, Any]:
    schema = item.get("schema")
    if not isinstance(schema, dict):
        raise RpcError(
            -32602,
            "missing_tool_schema",
            "Invalid params",
            {
                "message": f"tool {name!r} must declare a JSON schema",
                "tool": name,
            },
        )
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
    return schema


def validate_client_tool_trust(item: dict[str, Any], name: str) -> None:
    trust = item.get("trust")
    if trust is None or trust == "client":
        return
    raise RpcError(
        -32602,
        "invalid_tool_trust",
        "Invalid params",
        {
            "message": "client tools are always registered with client trust",
            "tool": name,
        },
    )


def client_tool_aliases(item: dict[str, Any], name: str) -> tuple[str, ...]:
    aliases = item.get("aliases")
    if not isinstance(aliases, list):
        return (name,)
    normalized = tuple(alias for alias in aliases if isinstance(alias, str) and alias)
    return normalized or (name,)


def client_tool_effects(item: dict[str, Any]) -> tuple[EffectKind, ...]:
    raw_effects = item.get("effects")
    if not isinstance(raw_effects, list):
        return ()
    return tuple(
        cast(EffectKind, effect)
        for effect in raw_effects
        if isinstance(effect, str) and effect in EFFECT_KINDS
    )


def client_tool_supports_direct(
    item: dict[str, Any],
    effects: tuple[EffectKind, ...],
) -> bool:
    if "supports_direct" in item:
        return item.get("supports_direct") is True
    return bool(effects) and all(effect in READ_ONLY_EFFECT_KINDS for effect in effects)


def client_capability_declaration(capability: Capability) -> dict[str, Any]:
    declaration = capability.spec.metadata()
    declaration["supports_staging"] = capability.policy.supports_staging
    declaration["supports_direct"] = capability.policy.supports_direct
    declaration["trust"] = capability.policy.trust
    if capability.policy.timeout_seconds is not None:
        declaration["timeout_sec"] = capability.policy.timeout_seconds
    return declaration


def client_tool_capability(
    name: str,
    item: dict[str, Any],
    *,
    call_client_tool: Callable[..., dict[str, Any]],
) -> Capability:
    provider = client_tool_provider(item, name)
    schema = client_tool_schema(item, name)
    validate_client_tool_trust(item, name)
    effects = client_tool_effects(item)
    timeout_seconds = client_tool_timeout_sec(item)
    return Capability(
        CapabilitySpec(
            CapabilityId(provider, name),
            str(item.get("description") or ""),
            schema,
            interactive=item.get("interactive") is not False,
            effects=effects,
            aliases=client_tool_aliases(item, name),
        ),
        CapabilityPolicy(
            supports_staging=item.get("supports_staging") is True,
            supports_direct=client_tool_supports_direct(item, effects),
            trust="client",
            timeout_seconds=timeout_seconds,
        ),
        RpcClientCapabilityExecutor(
            call_client_tool,
            name,
            timeout_seconds=timeout_seconds,
        ),
    )


def rpc_publish_event_draft(params: dict[str, Any]) -> DraftEvent:
    event = params.get("event")
    if isinstance(event, dict):
        session_id = optional_string(params.get("session_id")) or optional_string(
            event.get("session")
        )
        return boundary_event_draft(
            {"cwd": os.getcwd(), **event},
            session_id=session_id or "",
        )
    event_type = optional_string(params.get("type"))
    if event_type is None:
        raise RpcError(
            -32602,
            "missing_event_type",
            "Invalid params",
            {"message": "events.publish requires type"},
        )
    payload = params.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise RpcError(
            -32602,
            "invalid_event_payload",
            "Invalid params",
            {"message": "events.publish payload must be an object"},
        )
    return DraftEvent(
        event_type,
        optional_string(params.get("source")) or "zeta",
        dict(payload),
        idempotency_key=optional_string(params.get("idempotency_key")),
        caused_by=optional_string(params.get("caused_by")),
        session_id=optional_string(params.get("session_id")),
        turn_id=optional_string(params.get("turn_id")),
    )


def normalized_client_tool_response(
    raw_result: Any,
) -> tuple[dict[str, Any], ToolCallStatus]:
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


def event_cursor(value: Any) -> int | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RpcError(
            -32602,
            "invalid_cursor",
            "Invalid params",
            {"message": "after must be an event cursor string"},
        )
    try:
        return int(value)
    except ValueError:
        raise RpcError(
            -32602,
            "invalid_cursor",
            "Invalid params",
            {"message": "after must be an event cursor string"},
        ) from None


def positive_limit(value: Any) -> int | None:
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
    if (
        subscription.session_id is not None
        and event.get("session") != subscription.session_id
    ):
        return False
    if subscription.run_id is not None and event.get("run_id") != subscription.run_id:
        return False
    if subscription.after_seq is None:
        return True
    try:
        cursor_seq = int(str(event.get("cursor") or ""))
    except ValueError:
        return False
    return cursor_seq > subscription.after_seq


class JsonRpcProtocol:
    def __init__(
        self,
        input: TextIO,
        output: TextIO,
        *,
        session_runner: RpcSessionRunner | None = None,
        tool_registry: CapabilityRegistry | None = None,
        event_reader: EventReader | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        self.input = input
        self.output = output
        self.session_runner = session_runner
        self.tool_registry = tool_registry or _runtime_tool_registry
        self.event_reader = event_reader
        self.event_sink = event_sink
        if self.event_sink is None and hasattr(event_reader, "accept"):
            self.event_sink = cast(EventSink, event_reader)
        self.event_dispatcher: AsyncEventDispatcher | None = None
        self.tool_responses: dict[str, dict[str, Any]] = {}
        self.tool_calls: dict[str, ClientToolCall] = {}
        self.client_tools: set[str] = set()
        self.runs: dict[str, RpcRunState] = {}
        self.event_subscriptions: dict[str, EventSubscription] = {}

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

    def read_message_before(
        self, deadline: float | None
    ) -> dict[str, Any] | object | None:
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

    def sync_method_handlers(
        self,
    ) -> dict[str, Callable[[dict[str, Any]], dict[str, Any] | None]]:
        return {
            "initialize": self.initialize,
            "tools.register": self.register_tools_rpc,
            "tools.respond": self.respond_tools_rpc,
            "events.list": self.list_events,
            "events.subscribe": self.subscribe_events,
            "events.publish": self.events_publish_unavailable,
            "session.run": self.session_run_unavailable,
        }

    def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        del params
        return {"server": "zeta", "protocol": "0.1"}

    def register_tools_rpc(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"registered": self.register_client_tools(params.get("tools"))}

    def respond_tools_rpc(self, params: dict[str, Any]) -> None:
        self.record_tool_response(params)
        return None

    def events_publish_unavailable(self, params: dict[str, Any]) -> None:
        del params
        raise RpcError(
            -32000,
            "events_unavailable",
            "Server error",
            {"message": "events.publish requires an active async server"},
        )

    def session_run_unavailable(self, params: dict[str, Any]) -> None:
        del params
        raise RpcError(
            -32000,
            "session_run_unavailable",
            "Server error",
            {"message": "session.run requires an active async server"},
        )

    def dispatch_sync(
        self, method: str, params: dict[str, Any]
    ) -> dict[str, Any] | None:
        handler = self.sync_method_handlers().get(method)
        if handler is not None:
            return handler(params)
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
        request = EventListParams.from_mapping(params)
        events = self.event_reader.list_events(
            Filter(
                session_id=request.session_id,
                turn_id=request.run_id,
                after_seq=request.after_seq,
                limit=request.limit,
            )
        )
        next_cursor = (
            str(events[-1].seq)
            if events
            else (str(request.after_seq) if request.after_seq is not None else None)
        )
        return {
            "events": [event_view(event) for event in events],
            "next_cursor": next_cursor,
        }

    def subscribe_events(self, params: dict[str, Any]) -> dict[str, Any]:
        request = EventSubscribeParams.from_mapping(params)
        subscription = request.subscription()
        self.event_subscriptions[subscription.id] = subscription
        return {
            "subscribed": True,
            "subscription_id": subscription.id,
            "next_cursor": str(request.after_seq)
            if request.after_seq is not None
            else None,
        }

    def register_client_tools(self, tools: Any) -> list[dict[str, Any]]:
        if not isinstance(tools, list):
            return []
        registered = []
        for item in tools:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if not name:
                continue
            registered.append(self.register_client_tool(name, item))
        return registered

    def register_client_tool(self, name: str, item: dict[str, Any]) -> dict[str, Any]:
        capability = client_tool_capability(
            name,
            item,
            call_client_tool=self.call_client_tool,
        )
        capability_id = capability.spec.id.canonical()
        existing = self.tool_registry.get(capability_id)
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
        try:
            self.tool_registry.register(capability)
        except ValueError as exc:
            raise RpcError(
                -32602,
                "invalid_tool_capability",
                "Invalid params",
                {
                    "message": str(exc),
                    "tool": name,
                },
            ) from exc
        self.client_tools.add(name)
        return client_capability_declaration(capability)

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
            rpc_message = cast(dict[str, Any], message)
            if str(rpc_message.get("method") or "") == "tools.respond":
                response_params = rpc_message.get("params")
                if isinstance(response_params, dict):
                    self.record_tool_response(response_params)
                continue
            self.handle_nested_message(rpc_message)
        return self.tool_responses.pop(call_id)

    def handle_nested_message(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        method = str(message.get("method") or "")
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        try:
            result = self.dispatch_sync(method, params)
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
        self.output.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.output.flush()


class JsonRpcServer(JsonRpcProtocol):
    """Async JSON-RPC transport variant for daemon-style session scheduling."""

    def __init__(
        self,
        input: TextIO,
        output: TextIO,
        *,
        session_runner: RpcSessionRunner | None = None,
        tool_registry: CapabilityRegistry | None = None,
        event_reader: EventReader | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        super().__init__(
            input,
            output,
            session_runner=session_runner,
            tool_registry=tool_registry,
            event_reader=event_reader,
            event_sink=event_sink,
        )
        self.session_runner = session_runner
        self._task_group: asyncio.TaskGroup | None = None

    async def serve(self) -> None:
        async with asyncio.TaskGroup() as task_group:
            self._task_group = task_group
            try:
                while (
                    message := await asyncio.to_thread(super().read_message)
                ) is not None:
                    await self.handle_message(message)
            finally:
                self._task_group = None

    async def handle_message(self, message: dict[str, Any]) -> None:
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
            result = await self.dispatch(method, params)
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

    async def dispatch(
        self,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any] | None:
        handler = self.async_method_handlers().get(method)
        if handler is not None:
            return await handler(params)
        return super().dispatch_sync(method, params)

    def async_method_handlers(
        self,
    ) -> dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]]:
        return {
            "session.cancel": self.cancel_session_async,
            "events.publish": self.publish_runtime_event_async,
        }

    async def cancel_session_async(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.cancel_session(params)

    async def publish_runtime_event_async(
        self,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        dispatcher = self.event_dispatcher
        if dispatcher is None:
            if self.event_sink is None:
                raise RpcError(
                    -32000,
                    "events_unavailable",
                    "Server error",
                    {"message": "events.publish is not configured"},
                )
            dispatcher = AsyncEventDispatcher(
                self.event_sink,
                publish_event=lambda event: self.publish_event(event_view(event)),
            )
        outcome = await dispatcher.dispatch(rpc_publish_event_draft(params))
        return {
            "inserted": outcome.inserted,
            "event": event_view(outcome.event),
            "work_events": [event_view(event) for event in outcome.work_events],
            "agent_results": outcome.agent_results,
        }

    def start_session_run(self, request_id: Any, params: dict[str, Any]) -> None:
        if self.session_runner is None:
            raise RpcError(
                -32000,
                "session_run_unavailable",
                "Server error",
                {"message": "session.run is not configured"},
            )
        if self._task_group is None:
            raise RpcError(
                -32000,
                "session_run_unavailable",
                "Server error",
                {"message": "session.run requires an active async server"},
            )
        run_id = rpc_run_id()
        cancellation_event = asyncio.Event()
        state = RpcRunState(
            run_id=run_id,
            request_id=request_id,
            cancellation_event=cancellation_event,
            cancel_mode=rpc_session_runner_cancel_mode(self.session_runner),
        )
        self.runs[run_id] = state
        worker_params = {
            **params,
            RPC_RUN_ID_PARAM: run_id,
            RPC_CANCELLATION_EVENT_PARAM: cancellation_event,
        }
        state.task = self._task_group.create_task(
            self.complete_session_run(state, worker_params)
        )

    async def complete_session_run(
        self,
        state: RpcRunState,
        params: dict[str, Any],
    ) -> None:
        try:
            assert self.session_runner is not None
            result = await self.call_session_runner(params)
        except asyncio.CancelledError:
            state.status = "cancelled"
            self.write_response(
                state.request_id,
                {"run_id": state.run_id, "outcome": "aborted", "final_text": ""},
            )
            return
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
            if state.cancellation_event.is_set() and result.get("outcome") == "aborted"
            else "completed"
        )
        self.write_response(state.request_id, result)

    async def call_session_runner(self, params: dict[str, Any]) -> dict[str, Any]:
        assert self.session_runner is not None
        return await self.session_runner(params)

    def cancel_session(self, params: dict[str, Any]) -> dict[str, Any]:
        request = SessionCancelParams.from_mapping(params)
        run_id = request.run_id
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
        if (
            state.cancel_mode == "task"
            and state.task is not None
            and not state.task.done()
        ):
            state.task.cancel()
        return {"cancelled": True, "run_id": run_id}
