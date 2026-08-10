"""Runtime-side wire-v0 supervisor for source plugins.

`SubprocessSource` spawns a child from a command spec, performs the
handshake, monitors heartbeats, restarts dead children with capped
exponential backoff, and yields the validated events. Malformed child
output is contained: junk lines earn strikes and eventually a respawn,
never an exception out of the supervisor.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from typing import Any

from zeta.wire.envelopes import PROTOCOL_VERSION, envelope
from zeta.wire.framing import FrameReader, FrameViolation, encode_frame

logger = logging.getLogger(__name__)

DEFAULT_HANDSHAKE_TIMEOUT_SECS = 10.0
DEFAULT_GRACE_SECS = 5.0
DEFAULT_HEARTBEAT_MISS_LIMIT = 3
DEFAULT_BACKOFF_INITIAL_SECS = 0.5
DEFAULT_BACKOFF_CAP_SECS = 30.0
DEFAULT_PROTOCOL_STRIKE_LIMIT = 3
HEALTHY_UPTIME_SECS = 30.0


@dataclass(frozen=True)
class SourceCommand:
    """How to spawn one source plugin child."""

    argv: tuple[str, ...]
    cwd: str | None = None


@dataclass(frozen=True)
class WireEvent:
    """One validated event received from a source plugin."""

    id: str
    type: str
    schema: str
    payload: dict[str, Any]
    caused_by: str | None
    session_id: str | None
    ts: str


class SubprocessSource:
    """Supervise one source plugin child and yield its events."""

    def __init__(
        self,
        command: SourceCommand,
        *,
        runtime_id: str,
        handshake_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT_SECS,
        grace_seconds: float = DEFAULT_GRACE_SECS,
        heartbeat_miss_limit: int = DEFAULT_HEARTBEAT_MISS_LIMIT,
        backoff_initial: float = DEFAULT_BACKOFF_INITIAL_SECS,
        backoff_cap: float = DEFAULT_BACKOFF_CAP_SECS,
        protocol_strike_limit: int = DEFAULT_PROTOCOL_STRIKE_LIMIT,
        max_restarts: int | None = None,
    ) -> None:
        self.command = command
        self.runtime_id = runtime_id
        self.handshake_timeout = handshake_timeout
        self.grace_seconds = grace_seconds
        self.heartbeat_miss_limit = heartbeat_miss_limit
        self.backoff_initial = backoff_initial
        self.backoff_cap = backoff_cap
        self.protocol_strike_limit = protocol_strike_limit
        self.max_restarts = max_restarts
        self.hello: dict[str, Any] | None = None
        self.restarts = 0
        self._process: asyncio.subprocess.Process | None = None
        self._closing = False
        self._message_ids = (f"m-host-{count}" for count in itertools.count(1))
        self._unacked: set[str] = set()

    async def __aenter__(self) -> SubprocessSource:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def events(self) -> AsyncGenerator[WireEvent, None]:
        """Yield events across child restarts until `aclose` is called."""
        backoff = self.backoff_initial
        while not self._closing:
            started = asyncio.get_running_loop().time()
            try:
                async for event in self._run_one_child():
                    yield event
            except _ChildFailed as failure:
                logger.warning("source child failed: %s", failure)
            if self._closing:
                return
            await self._kill_current_child()
            uptime = asyncio.get_running_loop().time() - started
            if uptime >= HEALTHY_UPTIME_SECS:
                backoff = self.backoff_initial
            self.restarts += 1
            if self.max_restarts is not None and self.restarts > self.max_restarts:
                logger.error("source child exceeded max restarts; giving up")
                return
            logger.info("respawning source child in %.1fs", backoff)
            await _interruptible_sleep(backoff, lambda: self._closing)
            backoff = min(self.backoff_cap, backoff * 2)

    async def ack(self, event_id: str) -> None:
        """Acknowledge one event as durably accepted."""
        self._unacked.discard(event_id)
        process = self._process
        if process is None or process.stdin is None or process.stdin.is_closing():
            return
        process.stdin.write(
            encode_frame(envelope("ack", next(self._message_ids), event_id=event_id))
        )
        with contextlib.suppress(ConnectionError):
            await process.stdin.drain()

    async def aclose(self) -> None:
        """Stop supervising: shutdown message, grace, SIGTERM, SIGKILL."""
        self._closing = True
        process = self._process
        if process is None or process.returncode is not None:
            self._process = None
            return
        if process.stdin is not None and not process.stdin.is_closing():
            with contextlib.suppress(ConnectionError):
                process.stdin.write(
                    encode_frame(
                        envelope(
                            "shutdown",
                            next(self._message_ids),
                            reason="runtime stopping",
                        )
                    )
                )
                await process.stdin.drain()
        if await _wait_or_none(process, self.grace_seconds) is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            if await _wait_or_none(process, self.grace_seconds) is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        self._process = None

    async def _run_one_child(self) -> AsyncIterator[WireEvent]:
        process = await asyncio.create_subprocess_exec(
            *self.command.argv,
            cwd=self.command.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=None,
        )
        self._process = process
        self._unacked = set()
        assert process.stdout is not None and process.stdin is not None
        frame_reader = FrameReader(process.stdout)
        hello = await self._handshake(process, frame_reader)
        self.hello = hello
        heartbeat_secs = float(hello.get("heartbeat_secs", 10))
        ack_window = int(hello.get("ack_window", 64))
        read_timeout = heartbeat_secs * self.heartbeat_miss_limit
        strikes = 0
        while not self._closing:
            try:
                frame = await asyncio.wait_for(
                    frame_reader.read_frame(), timeout=read_timeout
                )
            except TimeoutError:
                raise _ChildFailed(
                    f"no heartbeat for {read_timeout:.0f}s "
                    f"({self.heartbeat_miss_limit} missed intervals)"
                ) from None
            if frame is None:
                if self._closing:
                    return
                raise _ChildFailed("child closed stdout")
            if isinstance(frame, FrameViolation):
                strikes += 1
                logger.warning(
                    "protocol violation from source child (%s): %s",
                    frame.rule,
                    frame.preview(),
                )
                await self._send_error(
                    process, "protocol", f"invalid frame: {frame.rule}"
                )
                if strikes >= self.protocol_strike_limit:
                    raise _ChildFailed(f"{strikes} protocol violations; killing child")
                continue
            kind = frame["kind"]
            if kind == "heartbeat":
                continue
            if kind == "error":
                logger.warning(
                    "source child reported %s (retryable=%s): %s",
                    frame.get("code"),
                    frame.get("retryable"),
                    frame.get("message"),
                )
                continue
            if kind == "event":
                if "payload" not in frame:
                    await self._send_error(
                        process,
                        "unsupported",
                        "this runtime stores payloads inline; "
                        "payload_hash events are not accepted in v0",
                    )
                    continue
                self._unacked.add(frame["id"])
                if len(self._unacked) > ack_window:
                    raise _ChildFailed(
                        f"ack window exceeded: {len(self._unacked)} unacked "
                        f"events for a window of {ack_window}"
                    )
                yield WireEvent(
                    id=frame["id"],
                    type=frame["type"],
                    schema=frame["schema"],
                    payload=frame["payload"],
                    caused_by=frame.get("caused_by"),
                    session_id=frame.get("session_id"),
                    ts=frame["ts"],
                )
                continue
            await self._send_error(
                process, "protocol", f"unexpected kind {kind!r} after handshake"
            )
            raise _ChildFailed(f"child sent unexpected kind {kind!r}")

    async def _handshake(
        self,
        process: asyncio.subprocess.Process,
        frame_reader: FrameReader,
    ) -> dict[str, Any]:
        try:
            frame = await asyncio.wait_for(
                frame_reader.read_frame(), timeout=self.handshake_timeout
            )
        except TimeoutError:
            raise _ChildFailed(
                f"no hello within {self.handshake_timeout:.0f}s"
            ) from None
        if frame is None:
            raise _ChildFailed("child exited before hello")
        if isinstance(frame, FrameViolation):
            raise _ChildFailed(
                f"first frame was not a valid envelope ({frame.rule}): "
                f"{frame.preview()}"
            )
        if frame["kind"] != "hello":
            raise _ChildFailed(f"first frame was {frame['kind']!r}, not hello")
        if frame["role"] != "source":
            await self._send_error(
                process,
                "protocol",
                f"role {frame['role']!r} is not available in this runtime",
            )
            raise _ChildFailed(f"unsupported role {frame['role']!r}")
        if PROTOCOL_VERSION not in frame["protocol_versions"]:
            await self._send_error(
                process,
                "unsupported_version",
                f"runtime speaks protocol {PROTOCOL_VERSION} only",
            )
            raise _ChildFailed(
                f"no common protocol version in {frame['protocol_versions']!r}"
            )
        assert process.stdin is not None
        process.stdin.write(
            encode_frame(
                envelope(
                    "hello_ack",
                    next(self._message_ids),
                    protocol_version=PROTOCOL_VERSION,
                    runtime=self.runtime_id,
                )
            )
        )
        await process.stdin.drain()
        return frame

    async def _send_error(
        self,
        process: asyncio.subprocess.Process,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        if process.stdin is None or process.stdin.is_closing():
            return
        with contextlib.suppress(ConnectionError):
            process.stdin.write(
                encode_frame(
                    envelope(
                        "error",
                        next(self._message_ids),
                        code=code,
                        message=message,
                        retryable=retryable,
                    )
                )
            )
            await process.stdin.drain()

    async def _kill_current_child(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        if await _wait_or_none(process, self.grace_seconds) is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()


class _ChildFailed(RuntimeError):
    """One child incarnation ended abnormally; the supervisor respawns."""


async def _wait_or_none(
    process: asyncio.subprocess.Process, timeout: float
) -> int | None:
    with contextlib.suppress(TimeoutError):
        return await asyncio.wait_for(process.wait(), timeout=timeout)
    return None


async def _interruptible_sleep(seconds: float, should_stop) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not should_stop() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
