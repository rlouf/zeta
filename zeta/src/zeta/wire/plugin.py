"""Child-side wire-v0 SDK for `source` plugins.

A plugin author supplies an async iterable of `SourceEvent`s and calls
`run_source`; the SDK owns the handshake, event ids, the ack window,
heartbeats, and shutdown. This module (with `envelopes` and `framing`)
is the part that later extracts into a standalone SDK: it imports
nothing from Zeta outside `zeta.wire` and `zeta.addresses`.

stdout belongs to the protocol. `run_source` grabs the real stdout
stream for frames, then points `sys.stdout` and the root logger at
stderr, so a stray `print` or log call cannot corrupt the channel.
"""

from __future__ import annotations

import asyncio
import inspect
import itertools
import logging
import sys
from collections.abc import AsyncIterable, AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, BinaryIO, cast

from zeta.wire.envelopes import (
    MAX_INLINE_PAYLOAD_BYTES,
    PROTOCOL_VERSION,
    canonical_json,
    envelope,
    mint_event_id,
)
from zeta.wire.framing import FrameReader, FrameViolation, encode_frame

logger = logging.getLogger(__name__)

DEFAULT_HEARTBEAT_SECS = 10.0
DEFAULT_ACK_WINDOW = 64
HANDSHAKE_TIMEOUT_SECS = 30.0


@dataclass(frozen=True)
class EventType:
    """One event type a source may emit, with its schema reference."""

    type: str
    schema: str


@dataclass(frozen=True)
class SourceEvent:
    """One event a source hands to the SDK for delivery.

    `on_ack` runs when the runtime acknowledges the event as durably
    journaled. A source holding an upstream cursor advances it there
    and only there (spec §7), so a crash before the ack redelivers.
    """

    type: str
    payload: dict[str, Any]
    caused_by: str | None = None
    session_id: str | None = None
    on_ack: Callable[[], Any] | None = None


class OperationError(RuntimeError):
    """A served operation failed in a way the runtime should see."""

    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


OperationHandler = Callable[[dict[str, Any], str], Any]
Source = (
    AsyncIterable[SourceEvent]
    | Callable[[dict[str, Any]], AsyncIterable[SourceEvent]]
    | None
)


@dataclass
class _Session:
    writer: asyncio.StreamWriter
    ack_window: int
    unacked: dict[str, SourceEvent] = field(default_factory=dict)
    acked: asyncio.Condition = field(default_factory=asyncio.Condition)
    stop: asyncio.Event = field(default_factory=asyncio.Event)

    def send(self, message: dict[str, Any]) -> None:
        self.writer.write(encode_frame(message))


def run_source(
    events: Source,
    *,
    name: str,
    plugin_version: str,
    event_types: list[EventType],
    operations: dict[str, OperationHandler] | None = None,
    capabilities: dict[str, Any] | None = None,
    heartbeat_secs: float = DEFAULT_HEARTBEAT_SECS,
    ack_window: int = DEFAULT_ACK_WINDOW,
) -> None:
    """Speak wire-v0 as a source plugin until shutdown or end of events.

    `events` may be an async iterable, a callable that receives the
    `hello_ack.config` object and returns one, or None for a pure
    operations plugin. `operations` maps operation names to async
    handlers `(payload, effect_key) -> result dict`.
    """
    proto_stdout = sys.stdout.buffer
    sys.stdout = sys.stderr
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    asyncio.run(
        _run_source(
            events,
            proto_stdout,
            name=name,
            plugin_version=plugin_version,
            event_types=event_types,
            operations=operations or {},
            capabilities=capabilities,
            heartbeat_secs=heartbeat_secs,
            ack_window=ack_window,
        )
    )


async def _run_source(
    events: Source,
    proto_stdout: BinaryIO,
    *,
    name: str,
    plugin_version: str,
    event_types: list[EventType],
    operations: dict[str, OperationHandler],
    capabilities: dict[str, Any] | None,
    heartbeat_secs: float,
    ack_window: int,
) -> None:
    reader, writer = await _stdio_streams(proto_stdout)
    message_ids = (f"m-{name}-{count}" for count in itertools.count(1))
    session = _Session(writer=writer, ack_window=ack_window)
    session.send(
        envelope(
            "hello",
            next(message_ids),
            name=name,
            plugin_version=plugin_version,
            role="source",
            operations=[{"name": operation} for operation in sorted(operations)],
            protocol_versions=[PROTOCOL_VERSION],
            event_types=[
                {"type": entry.type, "schema": entry.schema} for entry in event_types
            ],
            capabilities=dict(capabilities or {"effects_are_proposals": False}),
            heartbeat_secs=heartbeat_secs,
            ack_window=ack_window,
        )
    )
    await writer.drain()
    schemas = {entry.type: entry.schema for entry in event_types}
    frame_reader = FrameReader(reader)
    ack = await asyncio.wait_for(
        frame_reader.read_frame(), timeout=HANDSHAKE_TIMEOUT_SECS
    )
    if ack is None or isinstance(ack, FrameViolation) or ack.get("kind") != "hello_ack":
        raise SystemExit(f"handshake failed: expected hello_ack, got {ack!r}")
    if ack.get("protocol_version") != PROTOCOL_VERSION:
        raise SystemExit(
            f"handshake failed: unsupported protocol {ack.get('protocol_version')!r}"
        )
    config = ack.get("config")
    resolved_events = _resolve_source(
        events, config if isinstance(config, dict) else {}
    )

    tasks = [
        asyncio.create_task(
            _read_parent(frame_reader, session, operations, message_ids)
        ),
        asyncio.create_task(_heartbeat(session, message_ids, heartbeat_secs)),
    ]
    if resolved_events is not None:
        tasks.append(asyncio.create_task(_emit(resolved_events, session, schemas)))
    await session.stop.wait()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    writer.close()


def _resolve_source(
    events: Source,
    config: dict[str, Any],
) -> AsyncIterable[SourceEvent] | None:
    if events is None:
        return None
    if isinstance(events, AsyncIterable):
        return cast(AsyncIterable[SourceEvent], events)
    factory = cast(Callable[[dict[str, Any]], AsyncIterable[SourceEvent]], events)
    return factory(config)


async def _emit(
    events: AsyncIterable[SourceEvent],
    session: _Session,
    schemas: dict[str, str],
) -> None:
    try:
        iterator: AsyncIterator[SourceEvent] = aiter(events)
        async for event in iterator:
            await _send_event(session, event, schemas)
    except Exception:
        logger.exception("source iterator failed")
    finally:
        session.stop.set()


async def _send_event(
    session: _Session,
    event: SourceEvent,
    schemas: dict[str, str],
) -> None:
    schema = schemas.get(event.type)
    if schema is None:
        raise ValueError(
            f"event type {event.type!r} was not declared in the hello's event_types"
        )
    payload_bytes = len(canonical_json(event.payload).encode())
    if payload_bytes > MAX_INLINE_PAYLOAD_BYTES:
        raise ValueError(
            f"payload for {event.type!r} is {payload_bytes} bytes; wire-v0 "
            "sources may only inline payloads up to 64 KiB"
        )
    event_id = mint_event_id(event.type, event.payload)
    async with session.acked:
        await session.acked.wait_for(
            lambda: len(session.unacked) < session.ack_window or session.stop.is_set()
        )
        if session.stop.is_set():
            return
        session.unacked[event_id] = event
    session.send(
        envelope(
            "event",
            event_id,
            type=event.type,
            schema=schema,
            caused_by=event.caused_by,
            session_id=event.session_id,
            payload=event.payload,
        )
    )
    await session.writer.drain()


async def _read_parent(
    frame_reader: FrameReader,
    session: _Session,
    operations: dict[str, OperationHandler],
    message_ids: Any,
) -> None:
    while True:
        frame = await frame_reader.read_frame()
        if frame is None:
            session.stop.set()
            return
        if isinstance(frame, FrameViolation):
            logger.warning(
                "ignoring bad parent frame (%s): %s", frame.rule, frame.preview()
            )
            continue
        kind = frame["kind"]
        if kind == "ack":
            async with session.acked:
                acked_event = session.unacked.pop(frame["event_id"], None)
                session.acked.notify_all()
            if acked_event is not None and acked_event.on_ack is not None:
                await _run_ack_callback(acked_event)
        elif kind == "call":
            asyncio.ensure_future(_serve_call(frame, session, operations, message_ids))
        elif kind == "shutdown":
            logger.info("shutdown requested: %s", frame.get("reason", ""))
            session.stop.set()
            async with session.acked:
                session.acked.notify_all()
            return
        elif kind == "error":
            logger.warning(
                "runtime error %s (retryable=%s): %s",
                frame.get("code"),
                frame.get("retryable"),
                frame.get("message"),
            )
        else:
            logger.warning("ignoring unexpected parent kind %r", kind)


async def _run_ack_callback(event: SourceEvent) -> None:
    if event.on_ack is None:
        return
    try:
        outcome = event.on_ack()
        if inspect.isawaitable(outcome):
            await outcome
    except Exception:
        logger.exception("on_ack callback failed for %r", event.type)


async def _serve_call(
    frame: dict[str, Any],
    session: _Session,
    operations: dict[str, OperationHandler],
    message_ids: Any,
) -> None:
    handler = operations.get(frame["name"])
    if handler is None:
        await _send_call_result(
            session,
            next(message_ids),
            frame["id"],
            error=("protocol", f"operation {frame['name']!r} was not declared", False),
        )
        return
    try:
        result = await handler(frame["payload"], frame["effect_key"])
        await _send_call_result(
            session, next(message_ids), frame["id"], result=dict(result or {})
        )
    except OperationError as exc:
        await _send_call_result(
            session,
            next(message_ids),
            frame["id"],
            error=(exc.code, str(exc), exc.retryable),
        )
    except Exception as exc:
        logger.exception("operation %r failed", frame["name"])
        await _send_call_result(
            session,
            next(message_ids),
            frame["id"],
            error=("internal", str(exc), False),
        )


async def _send_call_result(
    session: _Session,
    message_id: str,
    call_id: str,
    *,
    result: dict[str, Any] | None = None,
    error: tuple[str, str, bool] | None = None,
) -> None:
    if error is None:
        message = envelope(
            "call_result", message_id, call_id=call_id, ok=True, result=result or {}
        )
    else:
        code, detail, retryable = error
        message = envelope(
            "call_result",
            message_id,
            call_id=call_id,
            ok=False,
            error={"code": code, "message": detail, "retryable": retryable},
        )
    session.send(message)
    await session.writer.drain()


async def _heartbeat(
    session: _Session,
    message_ids: Any,
    heartbeat_secs: float,
) -> None:
    while not session.stop.is_set():
        await asyncio.sleep(heartbeat_secs)
        session.send(envelope("heartbeat", next(message_ids)))
        await session.writer.drain()


async def _stdio_streams(
    proto_stdout: BinaryIO,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
    )
    transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, proto_stdout
    )
    writer = asyncio.StreamWriter(transport, protocol, None, loop)
    return reader, writer
