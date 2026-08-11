"""Peer-side IPC client for executable sources and providers."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import itertools
import logging
import sys
from collections.abc import AsyncIterable, AsyncIterator, Callable
from dataclasses import dataclass, field
from importlib.metadata import EntryPoint
from typing import Any, BinaryIO, cast

from zeta.ipc.framing import FrameReader, FrameViolation, encode_frame
from zeta.ipc.messages import (
    INTERNAL_ERROR,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PROTOCOL_VERSION,
    SERVER_ERROR,
    MessageError,
    compact_json_bytes,
    error_response,
    message_kind,
    request,
    success_response,
    validate_initialize_result,
    validate_publish_result,
)

logger = logging.getLogger(__name__)

DEFAULT_HEARTBEAT_SECONDS = 10.0
DEFAULT_MAX_IN_FLIGHT = 64
INITIALIZE_TIMEOUT_SECONDS = 30.0


def _run_entry_point(argv: list[str]) -> None:
    """Load connector code only after execution enters its child process."""
    if len(argv) < 3 or not all(argv[:3]):
        raise SystemExit(
            "usage: python -m zeta.ipc.client GROUP NAME VALUE [PLUGIN_ARG ...]"
        )
    group, name, value, *plugin_argv = argv
    target = EntryPoint(name=name, value=value, group=group).load()
    if not callable(target):
        raise SystemExit(f"entry point {name} is not callable: {value}")
    target(plugin_argv)


@dataclass(frozen=True)
class EventType:
    """Declare one event type and the schema used for its payload."""

    type: str
    schema: str


@dataclass(frozen=True)
class SourceEvent:
    """Hold one event until the runtime confirms durable acceptance."""

    type: str
    payload: dict[str, Any]
    idempotency_key: str | None = None
    caused_by: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    turn_id: str | None = None
    on_ack: Callable[[], Any] | None = None


class ProviderError(RuntimeError):
    """Describe a provider failure with stable retry behavior."""

    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


ProviderHandler = Callable[[dict[str, Any], str], Any]
Source = (
    AsyncIterable[SourceEvent]
    | Callable[[dict[str, Any]], "AsyncIterable[SourceEvent] | None"]
    | None
)


@dataclass
class _PeerSession:
    writer: asyncio.StreamWriter
    max_in_flight: int
    pending_publishes: dict[int, SourceEvent] = field(default_factory=dict)
    incoming_requests: set[str | int] = field(default_factory=set)
    publish_slots: asyncio.Condition = field(default_factory=asyncio.Condition)
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    provider_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    stop: asyncio.Event = field(default_factory=asyncio.Event)

    async def send(self, message: dict[str, Any]) -> None:
        async with self.write_lock:
            self.writer.write(encode_frame(message))
            await self.writer.drain()


def run_peer(
    events: Source,
    *,
    name: str,
    peer_version: str,
    event_types: list[EventType] | None = None,
    methods: dict[str, ProviderHandler] | None = None,
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    max_in_flight: int = DEFAULT_MAX_IN_FLIGHT,
) -> None:
    """Run one executable peer until its source ends or shutdown arrives."""
    protocol_stdout = sys.stdout.buffer
    sys.stdout = sys.stderr
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    asyncio.run(
        _run_peer(
            events,
            protocol_stdout,
            name=name,
            peer_version=peer_version,
            event_types=event_types,
            methods=methods,
            heartbeat_seconds=heartbeat_seconds,
            max_in_flight=max_in_flight,
        )
    )


async def _run_peer(
    events: Source,
    protocol_stdout: BinaryIO,
    *,
    name: str,
    peer_version: str,
    event_types: list[EventType] | None,
    methods: dict[str, ProviderHandler] | None,
    heartbeat_seconds: float,
    max_in_flight: int,
) -> None:
    methods = methods or {}
    roles = []
    if event_types is not None:
        roles.append("source")
    if methods:
        roles.append("provider")
    if not roles:
        raise ValueError("an IPC peer must request at least one role")

    reader, writer = await _stdio_streams(protocol_stdout)
    session = _PeerSession(writer=writer, max_in_flight=max_in_flight)
    initialize_params: dict[str, Any] = {
        "protocol_versions": [PROTOCOL_VERSION],
        "peer": {"name": name, "version": peer_version},
        "roles": roles,
        "heartbeat_seconds": heartbeat_seconds,
        "max_in_flight": max_in_flight,
    }
    if event_types is not None:
        initialize_params["event_types"] = [
            {"type": declaration.type, "schema": declaration.schema}
            for declaration in event_types
        ]
    if methods:
        initialize_params["methods"] = [{"name": method} for method in sorted(methods)]
    await session.send(request("peer-initialize", "initialize", initialize_params))

    frame_reader = FrameReader(reader)
    initialized = await asyncio.wait_for(
        frame_reader.read_frame(), timeout=INITIALIZE_TIMEOUT_SECONDS
    )
    if initialized is None or isinstance(initialized, FrameViolation):
        raise SystemExit(f"initialize failed: {initialized!r}")
    if initialized.get("id") != "peer-initialize":
        raise SystemExit("initialize failed: response id did not match")
    if message_kind(initialized) == "error":
        raise SystemExit(f"initialize failed: {initialized['error']['message']}")
    if message_kind(initialized) != "success":
        raise SystemExit("initialize failed: expected a response")
    result = validate_initialize_result(initialized["result"], roles)
    session.max_in_flight = result["max_in_flight"]
    resolved_events = _resolve_source(events, result["config"])
    schemas = {
        declaration.type: declaration.schema for declaration in event_types or []
    }
    message_ids = itertools.count(1)
    tasks = [
        asyncio.create_task(_read_runtime(frame_reader, session, methods)),
    ]
    if resolved_events is not None:
        tasks.append(
            asyncio.create_task(
                _publish_events(resolved_events, session, schemas, message_ids)
            )
        )
    try:
        await session.stop.wait()
    finally:
        for task in tasks:
            task.cancel()
        for task in session.provider_tasks:
            task.cancel()
        await asyncio.gather(*tasks, *session.provider_tasks, return_exceptions=True)
        writer.close()
        with contextlib.suppress(ConnectionError, NotImplementedError):
            await writer.wait_closed()


def _resolve_source(
    events: Source,
    config: dict[str, Any],
) -> AsyncIterable[SourceEvent] | None:
    if events is None:
        return None
    if isinstance(events, AsyncIterable):
        return cast(AsyncIterable[SourceEvent], events)
    factory = cast(
        Callable[[dict[str, Any]], AsyncIterable[SourceEvent] | None], events
    )
    return factory(config)


async def _publish_events(
    events: AsyncIterable[SourceEvent],
    session: _PeerSession,
    schemas: dict[str, str],
    message_ids: Any,
) -> None:
    try:
        iterator: AsyncIterator[SourceEvent] = aiter(events)
        async for event in iterator:
            await _publish_event(event, session, schemas, message_ids)
        async with session.publish_slots:
            await session.publish_slots.wait_for(
                lambda: not session.pending_publishes or session.stop.is_set()
            )
    except Exception:
        logger.exception("source iterator failed")
    finally:
        session.stop.set()


async def _publish_event(
    event: SourceEvent,
    session: _PeerSession,
    schemas: dict[str, str],
    message_ids: Any,
) -> None:
    if event.type not in schemas:
        raise ValueError(f"event type {event.type!r} was not declared")
    payload_size = len(compact_json_bytes(event.payload))
    if payload_size > 64 * 1024:
        raise ValueError(
            f"payload for {event.type!r} is {payload_size} bytes; "
            "IPC sources may only inline payloads up to 64 KiB"
        )
    async with session.publish_slots:
        await session.publish_slots.wait_for(
            lambda: (
                len(session.pending_publishes) < session.max_in_flight
                or session.stop.is_set()
            )
        )
        if session.stop.is_set():
            return
        message_id = next(message_ids)
        session.pending_publishes[message_id] = event
    await session.send(
        request(
            message_id,
            "events.publish",
            {
                "type": event.type,
                "payload": event.payload,
                "idempotency_key": event.idempotency_key,
                "caused_by": event.caused_by,
                "session_id": event.session_id,
                "run_id": event.run_id,
                "turn_id": event.turn_id,
            },
        )
    )


async def _read_runtime(
    frame_reader: FrameReader,
    session: _PeerSession,
    methods: dict[str, ProviderHandler],
) -> None:
    while not session.stop.is_set():
        frame = await frame_reader.read_frame()
        if frame is None:
            session.stop.set()
            return
        if isinstance(frame, FrameViolation):
            logger.warning("ignoring invalid runtime frame: %s", frame.preview())
            continue
        kind = message_kind(frame)
        if kind in {"success", "error"}:
            await _resolve_publish(frame, session)
            continue
        if kind == "notification":
            logger.warning("ignoring runtime notification %r", frame["method"])
            continue
        method = frame["method"]
        if method == "ping":
            await session.send(success_response(frame["id"], {}))
        elif method == "shutdown":
            await session.send(success_response(frame["id"], {}))
            session.stop.set()
            async with session.publish_slots:
                session.publish_slots.notify_all()
            return
        elif method in methods:
            await _start_provider_request(frame, session, methods[method])
        else:
            await session.send(
                error_response(
                    frame["id"],
                    METHOD_NOT_FOUND,
                    "Method not found",
                    {"code": "method_not_found"},
                )
            )


async def _resolve_publish(frame: dict[str, Any], session: _PeerSession) -> None:
    message_id = frame["id"]
    async with session.publish_slots:
        event = session.pending_publishes.pop(message_id, None)
        session.publish_slots.notify_all()
    if event is None:
        logger.warning("dropping a response for unknown request id %r", message_id)
        return
    if message_kind(frame) == "error":
        logger.error("runtime rejected %r: %s", event.type, frame["error"]["message"])
        return
    try:
        validate_publish_result(frame["result"])
    except MessageError as exc:
        logger.error(
            "runtime returned an invalid publish result for %r: %s", event.type, exc
        )
        return
    await _run_ack_callback(event)


async def _start_provider_request(
    frame: dict[str, Any],
    session: _PeerSession,
    handler: ProviderHandler,
) -> None:
    message_id = frame["id"]
    if message_id in session.incoming_requests:
        await session.send(
            error_response(message_id, INVALID_REQUEST, "request id is already pending")
        )
        return
    if len(session.incoming_requests) >= session.max_in_flight:
        await session.send(
            error_response(message_id, SERVER_ERROR, "in-flight request limit is full")
        )
        return
    session.incoming_requests.add(message_id)
    task = asyncio.create_task(_serve_provider_request(frame, session, handler))
    session.provider_tasks.add(task)
    task.add_done_callback(session.provider_tasks.discard)


async def _serve_provider_request(
    frame: dict[str, Any],
    session: _PeerSession,
    handler: ProviderHandler,
) -> None:
    try:
        params = frame["params"]
        outcome = handler(params["input"], params["effect_key"])
        if inspect.isawaitable(outcome):
            outcome = await outcome
        result = dict(outcome or {})
        await session.send(success_response(frame["id"], result))
    except ProviderError as exc:
        await session.send(
            error_response(
                frame["id"],
                SERVER_ERROR,
                str(exc),
                {"code": exc.code, "retryable": exc.retryable},
            )
        )
    except Exception as exc:
        logger.exception("provider method %r failed", frame["method"])
        await session.send(error_response(frame["id"], INTERNAL_ERROR, str(exc)))
    finally:
        session.incoming_requests.discard(frame["id"])


async def _run_ack_callback(event: SourceEvent) -> None:
    if event.on_ack is None:
        return
    try:
        outcome = event.on_ack()
        if inspect.isawaitable(outcome):
            await outcome
    except Exception:
        logger.exception("on_ack callback failed for %r", event.type)


async def _stdio_streams(
    protocol_stdout: BinaryIO,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
    )
    transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, protocol_stdout
    )
    writer = asyncio.StreamWriter(transport, protocol, None, loop)
    return reader, writer


if __name__ == "__main__":
    _run_entry_point(sys.argv[1:])
