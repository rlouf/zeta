"""Zeta route adapters for the JSON-RPC boundary."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from zeta.capabilities.execution import error_result
from zeta.capabilities.registry import RegisteredCapability
from zeta.capabilities.types import Capability, CapabilityId
from zeta.orchestration.dispatch import EventDispatcher, ReservedRuntimeEventError
from zeta.orchestration.session_turn_agent import SESSION_TURN_AGENT_ID
from zeta.records.events import DraftEvent, Event
from zeta.records.stores import EventReader, EventStoreProtocol, Filter
from zeta.rpc.jsonrpc import JsonRpcConnection, JsonRpcRouter, RpcError
from zeta.run.context import RuntimeContext
from zeta.run.runs import RunStatus
from zeta.run.thread_run import (
    SessionRequestError,
    session_run_id,
    session_turn_requested_draft,
)

ToolCallStatus = Literal["requested", "responded", "failed", "cancelled", "timed_out"]
RPC_REQUESTED = "rpc.requested"
RPC_RESPONDED = "rpc.responded"
RPC_FAILED = "rpc.failed"


@dataclass
class RunState:
    """RPC-visible session run state used for cancellation and status responses."""

    run_id: str
    cancellation_event: asyncio.Event
    task: asyncio.Task[None] | None = None
    status: RunStatus = "running"


@dataclass(frozen=True)
class CapabilityRegistration:
    """Client `tools.register` payload shaped to construct a Zeta capability."""

    name: str
    provider: str = "rpc"
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: int | float | None = None


@dataclass(frozen=True)
class ToolResponse:
    """Client `tools.respond` payload used to resolve a pending tool call."""

    call_id: str
    status: ToolCallStatus
    result: dict[str, Any]


@dataclass
class RpcClient:
    """Per-RPC peer context shared by route adapters for stdio runtime calls."""

    connection: JsonRpcConnection | None
    session: RuntimeContext
    dispatcher: EventDispatcher
    pending_runs: dict[str, RunState]
    pending_tool_calls: dict[str, asyncio.Future[dict[str, Any]]]

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        if self.connection is not None:
            await self.connection.notify(method, params)

    async def call_tool(
        self,
        name: str,
        params: dict[str, Any],
        *,
        timeout_seconds: int | float | None,
    ) -> dict[str, Any]:
        call_id = str(uuid.uuid4())
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self.pending_tool_calls[call_id] = future
        notification_params: dict[str, Any] = {
            "call_id": call_id,
            "name": name,
            "arguments": params,
            "status": "requested",
        }
        if timeout_seconds is not None:
            notification_params["timeout_seconds"] = timeout_seconds
        await self.notify("tools.call", notification_params)
        try:
            if timeout_seconds is None:
                return await future
            return await asyncio.wait_for(future, timeout=timeout_seconds)
        except TimeoutError:
            return error_result(
                "client-tool-timeout",
                f"client tool {name} timed out after {timeout_seconds:g}s",
            )
        finally:
            if self.pending_tool_calls.get(call_id) is future:
                self.pending_tool_calls.pop(call_id, None)


def invalid_params(code: str, message: str, **extra: Any) -> RpcError:
    """Build a stable JSON-RPC invalid-params error for route validation failures."""

    return RpcError(-32602, code, "Invalid params", {"message": message, **extra})


def rpc_requested_draft(
    method: str,
    params: dict[str, Any],
    *,
    request_id: str | None = None,
    source: str = "zeta",
    session_id: str | None = None,
    run_id: str | None = None,
) -> DraftEvent:
    request_id = request_id or f"req_{uuid.uuid4().hex}"
    return DraftEvent(
        RPC_REQUESTED,
        source,
        {"request_id": request_id, "method": method, "params": params},
        idempotency_key=f"{RPC_REQUESTED}:{request_id}",
        session_id=session_id,
        run_id=run_id,
    )


def rpc_responded_draft(request: Event, result: Any) -> DraftEvent:
    request_id = rpc_request_id(request)
    return DraftEvent(
        RPC_RESPONDED,
        "zeta",
        {"request_id": request_id, "result": result},
        idempotency_key=f"{RPC_RESPONDED}:{request.id}",
        caused_by=request.id,
        session_id=request.session_id,
        run_id=request.run_id,
    )


def rpc_failed_draft(request: Event, error: dict[str, Any]) -> DraftEvent:
    request_id = rpc_request_id(request)
    return DraftEvent(
        RPC_FAILED,
        "zeta",
        {"request_id": request_id, "error": error},
        idempotency_key=f"{RPC_FAILED}:{request.id}",
        caused_by=request.id,
        session_id=request.session_id,
        run_id=request.run_id,
    )


def rpc_request_id(request: Event) -> str:
    request_id = request.payload.get("request_id")
    return request_id if isinstance(request_id, str) and request_id else request.id


async def run_eventlog_rpc_once(
    router: JsonRpcRouter,
    *,
    after_cursor: int | None = None,
) -> Event | None:
    store = router.client.session.event_sink
    if not isinstance(store, EventStoreProtocol):
        raise RpcError(
            -32000,
            "events_unavailable",
            "Server error",
            {"message": "event-log RPC requires a full event store"},
        )
    for request in store.list_events(
        Filter(event_type=RPC_REQUESTED, after_cursor=after_cursor)
    ):
        if rpc_request_has_terminal_response(store, request):
            continue
        response = await router.response_for_message(rpc_message_from_event(request))
        if response is None:
            draft = rpc_responded_draft(request, None)
        elif "error" in response:
            draft = rpc_failed_draft(request, response["error"])
        else:
            draft = rpc_responded_draft(request, response.get("result"))
        return store.accept(draft).event
    return None


def rpc_request_has_terminal_response(
    store: EventStoreProtocol,
    request: Event,
) -> bool:
    return any(
        child.event_type in {RPC_RESPONDED, RPC_FAILED}
        for child in store.children(request.id)
    )


def rpc_message_from_event(request: Event) -> dict[str, Any]:
    payload = request.payload
    method = payload.get("method")
    params = payload.get("params")
    if params is None:
        params = {}
    return {
        "jsonrpc": "2.0",
        "id": rpc_request_id(request),
        "method": method,
        "params": params,
    }


def event_to_wire(event: Event) -> dict[str, Any]:
    """Convert a durable Zeta event to the RPC event object sent on the wire."""

    return {
        "id": event.id,
        "event_type": event.event_type,
        "source": event.source,
        "payload": dict(event.payload),
        "idempotency_key": event.idempotency_key,
        "caused_by": event.caused_by,
        "session_id": event.session_id,
        "run_id": event.run_id,
        "timestamp_ms": event.timestamp_ms,
        "cursor": event.cursor,
    }


def capability_to_wire(
    capability: RegisteredCapability,
    *,
    timeout_seconds: int | float | None = None,
) -> dict[str, Any]:
    """Convert a registered capability to the RPC tool declaration response."""

    return {
        "id": capability.declaration.id.canonical(),
        "provider": capability.declaration.id.provider,
        "name": capability.declaration.id.name,
        "description": capability.declaration.description,
        "input_schema": capability.declaration.input_schema,
        "timeout_seconds": timeout_seconds,
    }


async def initialize(_params: dict[str, Any], _client: RpcClient) -> dict[str, Any]:
    """Return static protocol identity for the JSON-RPC `initialize` route."""

    return {"server": "zeta", "protocol": "0.1"}


async def events_publish(
    params: dict[str, Any],
    client: RpcClient,
) -> dict[str, Any]:
    """Publish a client-authored draft event through the runtime dispatcher."""

    try:
        draft = DraftEvent(**params)
    except TypeError as exc:
        raise invalid_params(
            "invalid_params",
            f"DraftEvent parameters are invalid: {exc}",
        ) from exc

    if not draft.event_type:
        raise invalid_params("invalid_event_type", "event_type must be non-empty")
    if not isinstance(draft.payload, dict):
        raise invalid_params("invalid_payload", "payload must be an object")

    try:
        outcome = await client.dispatcher.publish_event(draft)
    except ReservedRuntimeEventError as exc:
        raise invalid_params(
            "reserved_runtime_event",
            "events.publish cannot accept runtime lifecycle events",
            event_type=exc.event_type,
        ) from exc

    if outcome.inserted and client.connection is not None:
        asyncio.create_task(route_event(client, outcome.event))

    return {
        "inserted": outcome.inserted,
        "event": event_to_wire(outcome.event),
        "lifecycle_events": [],
    }


async def route_event(client: RpcClient, event: Event) -> None:
    """Let RPC ingress return before agent routing work runs."""

    try:
        route_outcome = await client.dispatcher.route(event)
        await client.dispatcher.run_queue_items(route_outcome.queue_items)
    except asyncio.CancelledError:
        raise
    except Exception:
        return


async def events_list(params: dict[str, Any], client: RpcClient) -> dict[str, Any]:
    """List durable events using the event store's constructor-shaped filter."""

    try:
        filter = Filter(**params)
    except TypeError as exc:
        raise invalid_params(
            "invalid_params",
            f"Filter parameters are invalid: {exc}",
        ) from exc

    if filter.after_cursor is not None and (
        isinstance(filter.after_cursor, bool)
        or not isinstance(filter.after_cursor, int)
        or filter.after_cursor < 0
    ):
        raise invalid_params(
            "invalid_cursor",
            "after_cursor must be a non-negative integer",
        )
    if filter.limit is not None and (
        isinstance(filter.limit, bool)
        or not isinstance(filter.limit, int)
        or filter.limit <= 0
    ):
        raise invalid_params("invalid_limit", "limit must be a positive integer")
    if not isinstance(client.session.event_sink, EventReader):
        raise RpcError(
            -32000,
            "events_unavailable",
            "Server error",
            {"message": "events.list is not configured"},
        )

    events = client.session.event_sink.list_events(filter)

    return {
        "events": [event_to_wire(event) for event in events],
        "next_cursor": events[-1].cursor if events else filter.after_cursor,
    }


async def session_run(params: dict[str, Any], client: RpcClient) -> dict[str, Any]:
    """Start a session run by publishing the requested-turn event and routing it."""

    run_id = session_run_id()
    try:
        draft = session_turn_requested_draft(
            params,
            run_id=run_id,
            runtime_context=client.session,
        )
    except SessionRequestError as exc:
        raise invalid_params(
            exc.code,
            exc.message,
            **{key: value for key, value in exc.data.items() if key != "message"},
        ) from exc

    cancellation_event = asyncio.Event()
    state = RunState(run_id=run_id, cancellation_event=cancellation_event)

    client.pending_runs[run_id] = state
    outcome = await client.dispatcher.publish_event(draft)
    state.task = asyncio.create_task(route_run(client, state, outcome.event))

    return {
        "run_id": run_id,
        "session_id": client.session.session_id,
        "status": "started",
        "event": event_to_wire(outcome.event),
    }


async def route_run(client: RpcClient, state: RunState, event: Event) -> None:
    """Route the requested-turn event in the background after `session.run` returns."""

    try:
        route_outcome = await client.dispatcher.route(event)
        lifecycle_events = [
            *route_outcome.lifecycle_events,
            *await client.dispatcher.run_queue_items(route_outcome.queue_items),
        ]
    except asyncio.CancelledError:
        state.status = "cancelled"
        raise
    except Exception:
        state.status = "failed"
        return
    state.status = run_status_from_lifecycle(state, lifecycle_events)


def run_status_from_lifecycle(
    state: RunState,
    lifecycle_events: list[Event],
) -> RunStatus:
    """Map runtime lifecycle events to the RPC status exposed by `session.cancel`."""

    for event in reversed(lifecycle_events):
        if (
            event.event_type == "runtime.queue_item.cancelled"
            and event.payload.get("target_agent") == SESSION_TURN_AGENT_ID
        ):
            return "cancelled"
        if (
            event.event_type == "runtime.queue_item.failed"
            and event.payload.get("target_agent") == SESSION_TURN_AGENT_ID
        ):
            return "failed"
        if (
            event.event_type == "runtime.queue_item.completed"
            and event.payload.get("target_agent") == SESSION_TURN_AGENT_ID
        ):
            return (
                "cancelled"
                if state.cancellation_event.is_set()
                and isinstance(event.payload.get("result"), dict)
                and event.payload["result"].get("outcome") == "aborted"
                else "completed"
            )
    return "cancelled" if state.cancellation_event.is_set() else "completed"


async def session_cancel(params: dict[str, Any], client: RpcClient) -> dict[str, Any]:
    """Request cancellation for an RPC-started session run by run id."""

    run_id = params.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise invalid_params("invalid_run_id", "run_id must be non-empty")

    state = client.pending_runs.get(run_id)
    if state is None:
        return {"cancelled": False, "run_id": run_id, "status": "unknown"}
    if state.status not in {"running", "cancelling"}:
        return {"cancelled": False, "run_id": run_id, "status": state.status}
    state.status = "cancelling"
    state.cancellation_event.set()

    return {"cancelled": True, "run_id": run_id, "status": state.status}


async def tools_register(params: dict[str, Any], client: RpcClient) -> dict[str, Any]:
    """Register RPC client tools as Zeta capabilities for later agent calls."""

    raw_capabilities = params.get("capabilities")
    if not isinstance(raw_capabilities, list):
        raise invalid_params("invalid_capabilities", "capabilities must be a list")
    registered = []
    for item in raw_capabilities:
        if not isinstance(item, dict):
            raise invalid_params(
                "invalid_capability",
                "each capability must be an object",
            )
        try:
            registration = CapabilityRegistration(**item)
        except TypeError as exc:
            raise invalid_params(
                "invalid_params",
                f"CapabilityRegistration parameters are invalid: {exc}",
            ) from exc
        if registration.provider != "rpc":
            raise invalid_params(
                "invalid_tool_provider",
                "client capabilities must use the rpc provider",
            )
        if not registration.name:
            raise invalid_params(
                "invalid_capability_name",
                "capability name must be non-empty",
            )
        if not isinstance(registration.input_schema, dict):
            raise invalid_params(
                "invalid_input_schema",
                "input_schema must be an object",
            )
        if (
            registration.timeout_seconds is not None
            and registration.timeout_seconds <= 0
        ):
            raise invalid_params(
                "invalid_timeout_seconds",
                "timeout_seconds must be positive",
            )

        async def execute_client_tool(
            params: dict[str, Any],
            *,
            name: str = registration.name,
            timeout_seconds: int | float | None = registration.timeout_seconds,
            **_ignored: Any,
        ) -> dict[str, Any]:
            return await client.call_tool(
                name,
                params,
                timeout_seconds=timeout_seconds,
            )

        capability = RegisteredCapability(
            Capability(
                CapabilityId(registration.provider, registration.name),
                registration.description,
                registration.input_schema,
            ),
            execute_client_tool,
        )
        capability_id = capability.declaration.id.canonical()
        if client.session.tool_registry.get(capability_id) is not None:
            raise invalid_params(
                "duplicate_tool",
                f"tool {registration.name!r} is already registered",
                tool=registration.name,
            )
        try:
            client.session.tool_registry.register(capability)
        except ValueError as exc:
            raise invalid_params(
                "invalid_tool_capability",
                str(exc),
                tool=registration.name,
            ) from exc
        registered.append(
            capability_to_wire(
                capability,
                timeout_seconds=registration.timeout_seconds,
            )
        )
    return {"registered": registered}


async def tools_respond(params: dict[str, Any], client: RpcClient) -> None:
    """Resolve a pending RPC client tool call with a `tools.respond` payload."""

    try:
        response = ToolResponse(**params)
    except TypeError as exc:
        raise invalid_params(
            "invalid_params",
            f"ToolResponse parameters are invalid: {exc}",
        ) from exc

    if not response.call_id:
        raise invalid_params("invalid_call_id", "call_id must be non-empty")
    if response.status not in {"responded", "failed", "cancelled", "timed_out"}:
        raise invalid_params(
            "invalid_tool_status",
            "status must be responded, failed, cancelled, or timed_out",
        )
    if not isinstance(response.result, dict):
        raise invalid_params("invalid_result", "result must be an object")
    if not isinstance(response.result.get("ok"), bool):
        raise invalid_params("invalid_result", "result.ok must be a boolean")

    future = client.pending_tool_calls.get(response.call_id)
    if future is None or future.done():
        return None

    future.set_result(response.result)

    return None
