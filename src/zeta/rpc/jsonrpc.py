"""Newline-delimited JSON-RPC protocol mechanics for Zeta transports."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeAlias, cast

if TYPE_CHECKING:
    from zeta.rpc.routes import RpcClient


RpcResult = dict[str, Any] | None
if TYPE_CHECKING:
    RpcHandler: TypeAlias = Callable[[dict[str, Any], RpcClient], Awaitable[RpcResult]]
else:
    RpcHandler: TypeAlias = Callable[[dict[str, Any], Any], Awaitable[RpcResult]]


@dataclass
class RpcError(RuntimeError):
    """JSON-RPC route error with both protocol and Zeta-specific failure codes."""

    jsonrpc_code: int
    zeta_code: str
    summary: str
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.summary)

    def error_data(self) -> dict[str, Any]:
        return {"code": self.zeta_code, **self.data}


class JsonRpcConnection:
    """Newline-delimited JSON-RPC stream that owns read, write, and notify rules."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.write_lock = asyncio.Lock()

    async def serve(self, router: JsonRpcRouter) -> None:
        try:
            async with asyncio.TaskGroup() as tasks:
                while line_bytes := await self.reader.readline():
                    if not line_bytes.strip():
                        continue
                    try:
                        message = json.loads(line_bytes.decode("utf-8").strip())
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        await self.write_error(
                            None,
                            -32700,
                            "Parse error",
                            {"message": str(exc)},
                        )
                        continue
                    if isinstance(message, dict):
                        tasks.create_task(
                            router.handle_message(cast(dict[str, Any], message))
                        )
                        continue
                    await self.write_error(None, -32600, "Invalid Request")
        finally:
            await self.close()

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self.write_message({"jsonrpc": "2.0", "method": method, "params": params})

    async def write_response(self, request_id: Any, result: Any) -> None:
        await self.write_message({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def write_error(
        self,
        request_id: Any,
        code: int,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        await self.write_message({"jsonrpc": "2.0", "id": request_id, "error": error})

    async def write_message(self, message: dict[str, Any]) -> None:
        payload = (json.dumps(message, separators=(",", ":")) + "\n").encode()
        async with self.write_lock:
            self.writer.write(payload)
            await self.writer.drain()

    async def close(self) -> None:
        self.writer.close()


class JsonRpcRouter:
    """Async JSON-RPC method router backed by an explicit method-to-callable map."""

    def __init__(
        self,
        client: RpcClient,
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
                return rpc_error_message(request_id, -32600, "Invalid Request")
            return None
        if params is None:
            params = {}
        if not isinstance(params, dict):
            if has_request_id:
                return rpc_error_message(request_id, -32602, "Invalid params")
            return None

        handler = self.routes.get(method)
        if handler is None:
            if has_request_id:
                return rpc_error_message(
                    request_id,
                    -32601,
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
                    -32603,
                    "Internal error",
                    {
                        "code": "internal_error",
                        "message": f"{type(exc).__name__}: {exc}",
                    },
                )
            return None

        if has_request_id:
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        return None


def rpc_error_message(
    request_id: Any,
    code: int,
    message: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}
