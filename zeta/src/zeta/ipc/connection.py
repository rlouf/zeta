"""Runtime-side connection state for the unified IPC protocol."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeAlias, cast

from zeta.ipc.framing import MAX_FRAME_BYTES, FrameReader, FrameViolation, encode_frame
from zeta.ipc.messages import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PROTOCOL_VERSION,
    SERVER_ERROR,
    error_response,
    message_kind,
    notification,
    success_response,
)

RpcResult = dict[str, Any] | None
RpcHandler: TypeAlias = Callable[[dict[str, Any], Any], Awaitable[RpcResult]]

CLIENT_METHODS = frozenset(
    {
        "events.list",
        "session.start",
        "session.send",
        "session.status",
        "session.list",
        "session.cancel",
    }
)
DEFAULT_HEARTBEAT_SECONDS = 10
DEFAULT_MAX_IN_FLIGHT = 64


@dataclass
class RpcError(RuntimeError):
    """Carry protocol and stable application codes from a runtime route."""

    jsonrpc_code: int
    zeta_code: str
    summary: str
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.summary)

    def error_data(self) -> dict[str, Any]:
        return {"code": self.zeta_code, **self.data}


class JsonRpcConnection:
    """Serve one initialized client over bounded newline-delimited JSON-RPC."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        runtime_name: str = "zeta",
        runtime_version: str = "unknown",
        allow_remote_shutdown: bool = True,
        max_frame_bytes: int = MAX_FRAME_BYTES,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.runtime_name = runtime_name
        self.runtime_version = runtime_version
        self.allow_remote_shutdown = allow_remote_shutdown
        self.max_frame_bytes = max_frame_bytes
        self.write_lock = asyncio.Lock()
        self.peer_name: str | None = None
        self.roles: frozenset[str] = frozenset()
        self.max_in_flight = DEFAULT_MAX_IN_FLIGHT
        self.pending_requests: set[str | int] = set()
        self.initialized = False
        self.closing = False

    async def serve(self, router: JsonRpcRouter) -> None:
        frames = FrameReader(self.reader, max_frame_bytes=self.max_frame_bytes)
        try:
            async with asyncio.TaskGroup() as tasks:
                while not self.closing:
                    frame = await frames.read_frame()
                    if frame is None:
                        break
                    await self._handle_frame(router, tasks, frame)
        finally:
            await self.close()

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        if (
            self.closing
            or not self.initialized
            or "client" not in self.roles
            or method != "event"
        ):
            return
        await self.write_message(notification(method, params))

    async def write_message(self, message: dict[str, Any]) -> None:
        payload = encode_frame(message)
        async with self.write_lock:
            self.writer.write(payload)
            await self.writer.drain()

    async def close(self) -> None:
        self.closing = True
        self.writer.close()
        with contextlib.suppress(
            AttributeError,
            BrokenPipeError,
            ConnectionError,
            RuntimeError,
        ):
            await self.writer.wait_closed()

    async def _handle_frame(
        self,
        router: JsonRpcRouter,
        tasks: asyncio.TaskGroup,
        frame: dict[str, Any] | FrameViolation,
    ) -> None:
        if isinstance(frame, FrameViolation):
            await self.write_message(
                error_response(frame.request_id, frame.code, frame.detail)
            )
            return
        if message_kind(frame) != "request":
            if not self.initialized:
                self.closing = True
            return
        if await self._handle_protocol_request(frame) or self.closing:
            return
        await self._queue_request(router, tasks, frame)

    async def _queue_request(
        self,
        router: JsonRpcRouter,
        tasks: asyncio.TaskGroup,
        message: dict[str, Any],
    ) -> None:
        request_id = cast(str | int, message["id"])
        if request_id in self.pending_requests:
            await self.write_message(
                rpc_error_message(
                    request_id,
                    INVALID_REQUEST,
                    "Invalid Request",
                    {"code": "request_pending"},
                )
            )
            return
        if len(self.pending_requests) >= self.max_in_flight:
            await self.write_message(
                rpc_error_message(
                    request_id,
                    SERVER_ERROR,
                    "Server error",
                    {"code": "in_flight_limit"},
                )
            )
            return
        self.pending_requests.add(request_id)
        tasks.create_task(self._dispatch_request(router, message, request_id))

    async def _handle_protocol_request(self, message: dict[str, Any]) -> bool:
        method = cast(str, message["method"])
        request_id = cast(str | int, message["id"])
        if method == "initialize":
            await self._initialize(message)
            return True
        if not self.initialized:
            await self.write_message(
                rpc_error_message(
                    request_id,
                    INVALID_REQUEST,
                    "Invalid Request",
                    {"code": "not_initialized"},
                )
            )
            self.closing = True
            return True
        if method == "ping":
            await self.write_message(success_response(request_id, {}))
            return True
        if method == "shutdown":
            if not self.allow_remote_shutdown:
                await self.write_message(
                    rpc_error_message(
                        request_id,
                        METHOD_NOT_FOUND,
                        "Method not found",
                        {"code": "method_not_found", "method": method},
                    )
                )
                return True
            await self.write_message(success_response(request_id, {}))
            self.closing = True
            return True
        if method not in CLIENT_METHODS or "client" not in self.roles:
            await self.write_message(
                rpc_error_message(
                    request_id,
                    METHOD_NOT_FOUND,
                    "Method not found",
                    {"code": "method_not_found", "method": method},
                )
            )
            return True
        return False

    async def _initialize(self, message: dict[str, Any]) -> None:
        request_id = cast(str | int, message["id"])
        if self.initialized:
            await self.write_message(
                rpc_error_message(
                    request_id,
                    INVALID_REQUEST,
                    "Invalid Request",
                    {"code": "already_initialized"},
                )
            )
            return
        params = cast(dict[str, Any], message["params"])
        versions = cast(list[int], params["protocol_versions"])
        if PROTOCOL_VERSION not in versions:
            await self.write_message(
                rpc_error_message(
                    request_id,
                    SERVER_ERROR,
                    "No supported IPC protocol version",
                    {"code": "unsupported_version", "retryable": False},
                )
            )
            self.closing = True
            return
        roles = cast(list[str], params["roles"])
        if roles != ["client"]:
            await self.write_message(
                rpc_error_message(
                    request_id,
                    INVALID_PARAMS,
                    "Invalid params",
                    {"code": "unsupported_role"},
                )
            )
            self.closing = True
            return
        peer = cast(dict[str, Any], params["peer"])
        heartbeat = params.get("heartbeat_seconds", DEFAULT_HEARTBEAT_SECONDS)
        requested_limit = cast(int, params.get("max_in_flight", DEFAULT_MAX_IN_FLIGHT))
        self.peer_name = cast(str, peer["name"])
        self.roles = frozenset(roles)
        self.max_in_flight = min(requested_limit, DEFAULT_MAX_IN_FLIGHT)
        self.initialized = True
        await self.write_message(
            success_response(
                request_id,
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "runtime": {
                        "name": self.runtime_name,
                        "version": self.runtime_version,
                    },
                    "roles": roles,
                    "config": {},
                    "heartbeat_seconds": heartbeat,
                    "max_in_flight": self.max_in_flight,
                },
            )
        )

    async def _dispatch_request(
        self,
        router: JsonRpcRouter,
        message: dict[str, Any],
        request_id: str | int,
    ) -> None:
        try:
            response = await router.response_for_message(message)
            if response is not None and not self.closing:
                await self.write_message(response)
        finally:
            self.pending_requests.discard(request_id)


class JsonRpcRouter:
    """Map the fixed application methods to explicit runtime callables."""

    def __init__(
        self,
        client: Any,
        routes: dict[str, RpcHandler] | None = None,
    ) -> None:
        self.client = client
        self.routes = dict(routes or {})

    def route(self, method: str, handler: RpcHandler) -> None:
        self.routes[method] = handler

    async def handle_message(self, message: dict[str, Any]) -> None:
        response = await self.response_for_message(message)
        if response is None:
            return
        connection = self.client.connection
        if connection is not None:
            await connection.write_message(response)

    async def response_for_message(
        self, message: dict[str, Any]
    ) -> dict[str, Any] | None:
        has_request_id = "id" in message
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params", {})

        if not isinstance(method, str) or not method:
            if has_request_id:
                return rpc_error_message(request_id, INVALID_REQUEST, "Invalid Request")
            return None
        if not isinstance(params, dict):
            if has_request_id:
                return rpc_error_message(request_id, INVALID_PARAMS, "Invalid params")
            return None

        handler = self.routes.get(method)
        if handler is None:
            if has_request_id:
                return rpc_error_message(
                    request_id,
                    METHOD_NOT_FOUND,
                    "Method not found",
                    {"code": "method_not_found", "method": method},
                )
            return None

        try:
            result = await handler(cast(dict[str, Any], params), self.client)
        except RpcError as exc:
            if has_request_id:
                return rpc_error_message(
                    request_id,
                    exc.jsonrpc_code,
                    exc.summary,
                    exc.error_data(),
                )
            return None
        except Exception as exc:
            if has_request_id:
                return rpc_error_message(
                    request_id,
                    INTERNAL_ERROR,
                    "Internal error",
                    {
                        "code": "internal_error",
                        "message": f"{type(exc).__name__}: {exc}",
                    },
                )
            return None

        if has_request_id:
            return success_response(cast(str | int, request_id), result)
        return None


def rpc_error_message(
    request_id: Any,
    code: int,
    message: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return error_response(cast(str | int | None, request_id), code, message, data)
