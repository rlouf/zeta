"""Private JSON-RPC helpers for the Zeta provider host."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, TextIO

JSONRPC_VERSION = "2.0"
PROTOCOL_VERSION = 0


class ProtocolError(ValueError):
    """A host protocol message has an invalid shape."""


def read_message(stream: TextIO) -> Mapping[str, Any] | None:
    """Read one JSON-RPC message, or return None at end of input."""

    line = stream.readline()
    if line == "":
        return None
    try:
        value = json.loads(line)
    except json.JSONDecodeError as error:
        raise ProtocolError(f"The line is not valid JSON: {error.msg}") from error
    if not isinstance(value, dict):
        raise ProtocolError("A JSON-RPC message must be an object")
    if value.get("jsonrpc") != JSONRPC_VERSION:
        raise ProtocolError('A JSON-RPC message must carry jsonrpc equal to "2.0"')
    return value


def write_message(stream: TextIO, message: Mapping[str, Any]) -> None:
    """Write one compact JSON-RPC message."""

    stream.write(json.dumps(message, separators=(",", ":"), sort_keys=True))
    stream.write("\n")
    stream.flush()


def request(
    identifier: str | int, method: str, params: Mapping[str, Any]
) -> dict[str, Any]:
    """Create one JSON-RPC request."""

    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": identifier,
        "method": method,
        "params": dict(params),
    }


def success(identifier: str | int, result: Mapping[str, Any]) -> dict[str, Any]:
    """Create one JSON-RPC success response."""

    return {"jsonrpc": JSONRPC_VERSION, "id": identifier, "result": dict(result)}


def error(
    identifier: str | int | None,
    code: int,
    message: str,
    *,
    stable_code: str | None = None,
    retryable: bool | None = None,
) -> dict[str, Any]:
    """Create one JSON-RPC error response."""

    body: dict[str, Any] = {"code": code, "message": message}
    data: dict[str, Any] = {}
    if stable_code is not None:
        data["code"] = stable_code
    if retryable is not None:
        data["retryable"] = retryable
    if data:
        body["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": identifier, "error": body}
