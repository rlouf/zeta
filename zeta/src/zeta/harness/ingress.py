"""Push-ingress HTTP transport for connector requests."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from urllib.parse import parse_qsl, urlsplit

from connectors import InboundRequest, InboundResponse

PushIngressRequestHandler = Callable[[str, InboundRequest], Awaitable[InboundResponse]]


async def run_push_ingress_forever(
    handler: PushIngressRequestHandler,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    route_prefix: str = "/connectors",
    stop_event: asyncio.Event | None = None,
) -> None:
    server = await asyncio.start_server(
        lambda reader, writer: handle_push_ingress_connection(
            handler,
            reader,
            writer,
            route_prefix=route_prefix,
        ),
        host,
        port,
    )
    async with server:
        if stop_event is None:
            await server.serve_forever()
            return
        await stop_event.wait()
        server.close()
        await server.wait_closed()


async def handle_push_ingress_connection(
    handler: PushIngressRequestHandler,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    route_prefix: str,
) -> None:
    try:
        request = await read_inbound_request(reader)
        connector_id = connector_id_from_path(request.path, route_prefix)
        if connector_id is None:
            response = InboundResponse(status_code=404, body=b"unknown route")
        else:
            response = await handler(connector_id, request)
        writer.write(http_response_bytes(response))
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def read_inbound_request(reader: asyncio.StreamReader) -> InboundRequest:
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.decode("iso-8859-1").split("\r\n")
    request_line = lines[0]
    try:
        method, raw_target, _version = request_line.split(" ", 2)
    except ValueError:
        return InboundRequest("GET", "/", {}, {}, b"")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    content_length = int(headers.get("content-length", "0") or "0")
    body = await reader.readexactly(content_length) if content_length else b""
    parsed = urlsplit(raw_target)
    return InboundRequest(
        method=method.upper(),
        path=parsed.path,
        headers=headers,
        query=dict(parse_qsl(parsed.query, keep_blank_values=True)),
        body=body,
    )


def connector_id_from_path(path: str, route_prefix: str) -> str | None:
    prefix = route_prefix.rstrip("/") or ""
    expected = f"{prefix}/"
    if not path.startswith(expected):
        return None
    connector_id = path[len(expected) :].split("/", 1)[0]
    return connector_id or None


def http_response_bytes(response: InboundResponse) -> bytes:
    reason = {
        200: "OK",
        202: "Accepted",
        400: "Bad Request",
        401: "Unauthorized",
        404: "Not Found",
        405: "Method Not Allowed",
    }.get(response.status_code, "OK")
    headers = {
        "Content-Length": str(len(response.body)),
        "Connection": "close",
        **dict(response.headers),
    }
    header_bytes = b"".join(
        f"{key}: {value}\r\n".encode("iso-8859-1") for key, value in headers.items()
    )
    return (
        f"HTTP/1.1 {response.status_code} {reason}\r\n".encode("iso-8859-1")
        + header_bytes
        + b"\r\n"
        + response.body
    )
