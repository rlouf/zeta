"""Runtime-side process supervision for executable IPC peers."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import os
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

from zeta.ipc.framing import FrameReader, FrameViolation, encode_frame
from zeta.ipc.messages import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PROTOCOL_VERSION,
    SERVER_ERROR,
    RequestId,
    error_response,
    message_kind,
    request,
    success_response,
)

logger = logging.getLogger(__name__)

DEFAULT_INITIALIZE_TIMEOUT_SECONDS = 10.0
DEFAULT_GRACE_SECONDS = 5.0
DEFAULT_HEARTBEAT_MISS_LIMIT = 3
DEFAULT_BACKOFF_INITIAL_SECONDS = 0.5
DEFAULT_BACKOFF_CAP_SECONDS = 30.0
DEFAULT_PROTOCOL_STRIKE_LIMIT = 3
HEALTHY_UPTIME_SECONDS = 30.0


@dataclass(frozen=True)
class PeerCommand:
    """Describe how the supervisor starts one executable peer."""

    argv: tuple[str, ...]
    cwd: str | None = None
    env: Mapping[str, str] | None = None


class ProviderCallError(RuntimeError):
    """Report a failed direct provider request to its runtime caller."""

    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class PublishRequest:
    """Hold a source request until its event is durably accepted or rejected."""

    request_id: RequestId
    type: str
    payload: dict[str, Any]
    idempotency_key: str | None
    caused_by: str | None
    session_id: str | None
    run_id: str | None
    turn_id: str | None


class SubprocessPeer:
    """Supervise one executable source or provider across process restarts."""

    def __init__(
        self,
        command: PeerCommand,
        *,
        runtime_name: str,
        runtime_version: str,
        config: dict[str, Any] | None = None,
        handshake_timeout: float = DEFAULT_INITIALIZE_TIMEOUT_SECONDS,
        grace_seconds: float = DEFAULT_GRACE_SECONDS,
        heartbeat_miss_limit: int = DEFAULT_HEARTBEAT_MISS_LIMIT,
        backoff_initial: float = DEFAULT_BACKOFF_INITIAL_SECONDS,
        backoff_cap: float = DEFAULT_BACKOFF_CAP_SECONDS,
        protocol_strike_limit: int = DEFAULT_PROTOCOL_STRIKE_LIMIT,
        max_restarts: int | None = None,
    ) -> None:
        self.command = command
        self.runtime_name = runtime_name
        self.runtime_version = runtime_version
        self.config = config
        self.handshake_timeout = handshake_timeout
        self.grace_seconds = grace_seconds
        self.heartbeat_miss_limit = heartbeat_miss_limit
        self.backoff_initial = backoff_initial
        self.backoff_cap = backoff_cap
        self.protocol_strike_limit = protocol_strike_limit
        self.max_restarts = max_restarts
        self.initialization: dict[str, Any] | None = None
        self.restarts = 0
        self._process: asyncio.subprocess.Process | None = None
        self._closing = False
        self._message_ids = (f"runtime-{count}" for count in itertools.count(1))
        self._write_lock = asyncio.Lock()
        self._pending_publications: dict[RequestId, PublishRequest] = {}
        self._pending_outgoing: dict[
            RequestId, tuple[str, asyncio.Future[dict[str, Any]] | None]
        ] = {}

    async def __aenter__(self) -> SubprocessPeer:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def publications(self) -> AsyncGenerator[PublishRequest, None]:
        """Yield publish requests across child restarts until closed."""
        backoff = self.backoff_initial
        while not self._closing:
            started = asyncio.get_running_loop().time()
            try:
                async for publication in self._run_one_peer():
                    yield publication
            except _PeerFailed as failure:
                logger.warning("IPC peer failed: %s", failure)
            self._fail_pending_calls("connector peer died")
            self._pending_publications.clear()
            if self._closing:
                return
            await self._kill_current_peer()
            uptime = asyncio.get_running_loop().time() - started
            if uptime >= HEALTHY_UPTIME_SECONDS:
                backoff = self.backoff_initial
            self.restarts += 1
            if self.max_restarts is not None and self.restarts > self.max_restarts:
                logger.error("IPC peer exceeded max restarts; giving up")
                return
            logger.info("respawning IPC peer in %.1fs", backoff)
            await _interruptible_sleep(backoff, lambda: self._closing)
            backoff = min(self.backoff_cap, backoff * 2)

    async def call(
        self,
        method: str,
        input_value: dict[str, Any],
        effect_key: str,
        *,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Call one method declared by the initialized provider peer."""
        process = self._process
        initialization = self.initialization or {}
        declared = {entry.get("name") for entry in initialization.get("methods", [])}
        if process is None or process.stdin is None or process.stdin.is_closing():
            raise ProviderCallError(
                "peer_unavailable", "connector peer is not running", retryable=True
            )
        if method not in declared:
            raise ProviderCallError(
                "method_not_found",
                f"method {method!r} was not declared by the peer",
                retryable=False,
            )
        maximum = int(initialization.get("max_in_flight", 64))
        if len(self._pending_outgoing) >= maximum:
            raise ProviderCallError(
                "peer_busy", "the peer request window is full", retryable=True
            )
        message_id = next(self._message_ids)
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending_outgoing[message_id] = (method, future)
        try:
            await self._send(
                process,
                request(
                    message_id,
                    method,
                    {"input": input_value, "effect_key": effect_key},
                ),
            )
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            raise ProviderCallError(
                "provider_timeout", f"call {method!r} timed out", retryable=True
            ) from None
        except ConnectionError as exc:
            raise ProviderCallError(
                "peer_unavailable", str(exc), retryable=True
            ) from exc
        finally:
            self._pending_outgoing.pop(message_id, None)

    async def complete_publish(
        self, publication: PublishRequest, result: dict[str, Any]
    ) -> None:
        """Respond only after the publication reached durable storage."""
        pending = self._pending_publications.get(publication.request_id)
        if pending != publication:
            raise ValueError("publish request is not pending")
        process = self._process
        if process is None:
            raise ConnectionError("connector peer is not running")
        await self._send(process, success_response(publication.request_id, result))
        self._pending_publications.pop(publication.request_id, None)

    async def fail_publish(
        self,
        publication: PublishRequest,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        """Reject one pending publication with a stable application error."""
        pending = self._pending_publications.get(publication.request_id)
        if pending != publication:
            raise ValueError("publish request is not pending")
        process = self._process
        if process is None:
            raise ConnectionError("connector peer is not running")
        await self._send(
            process,
            error_response(
                publication.request_id,
                SERVER_ERROR,
                message,
                {"code": code, "retryable": retryable},
            ),
        )
        self._pending_publications.pop(publication.request_id, None)

    async def aclose(self) -> None:
        """Request shutdown, then escalate through terminate and kill."""
        self._closing = True
        process = self._process
        if process is None or process.returncode is not None:
            self._process = None
            return
        if process.stdin is not None and not process.stdin.is_closing():
            with contextlib.suppress(ConnectionError):
                await self._send(
                    process,
                    request(
                        next(self._message_ids),
                        "shutdown",
                        {"reason": "runtime stopping"},
                    ),
                )
        if await _wait_or_none(process, self.grace_seconds) is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            if await _wait_or_none(process, self.grace_seconds) is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        self._process = None
        self._fail_pending_calls("connector peer stopped")

    async def _run_one_peer(self) -> AsyncIterator[PublishRequest]:
        environment = None
        if self.command.env is not None:
            environment = {**os.environ, **dict(self.command.env)}
        process = await asyncio.create_subprocess_exec(
            *self.command.argv,
            cwd=self.command.cwd,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=None,
        )
        self._process = process
        self.initialization = None
        self._pending_publications = {}
        assert process.stdout is not None and process.stdin is not None
        frame_reader = FrameReader(process.stdout)
        initialization = await self._initialize(process, frame_reader)
        self.initialization = initialization
        heartbeat_seconds = float(initialization.get("heartbeat_seconds", 10))
        strikes = 0
        missed_intervals = 0
        while not self._closing:
            try:
                frame = await asyncio.wait_for(
                    frame_reader.read_frame(), timeout=heartbeat_seconds
                )
            except TimeoutError:
                missed_intervals += 1
                await self._send_ping(process)
                if missed_intervals >= self.heartbeat_miss_limit:
                    raise _PeerFailed(
                        f"peer missed {missed_intervals} heartbeat intervals"
                    ) from None
                continue
            if frame is None:
                if self._closing:
                    return
                raise _PeerFailed("peer closed stdout")
            if isinstance(frame, FrameViolation):
                strikes += 1
                logger.warning(
                    "protocol violation from IPC peer (%s): %s",
                    frame.rule,
                    frame.preview(),
                )
                await self._send(
                    process,
                    error_response(None, frame.code, frame.detail),
                )
                if strikes >= self.protocol_strike_limit:
                    raise _PeerFailed(f"{strikes} protocol violations")
                continue
            missed_intervals = 0
            publication = await self._dispatch_message(process, frame)
            if publication is not None:
                yield publication

    async def _dispatch_message(
        self,
        process: asyncio.subprocess.Process,
        message: dict[str, Any],
    ) -> PublishRequest | None:
        kind = message_kind(message)
        if kind in {"success", "error"}:
            self._resolve_outgoing(message)
            return None
        if kind == "notification":
            raise _PeerFailed(f"unexpected notification {message['method']!r}")
        method = message["method"]
        if method == "ping":
            await self._send(process, success_response(message["id"], {}))
            return None
        if method != "events.publish":
            code = INVALID_REQUEST if method == "initialize" else METHOD_NOT_FOUND
            await self._send(
                process,
                error_response(
                    message["id"],
                    code,
                    "connection is already initialized"
                    if method == "initialize"
                    else "Method not found",
                ),
            )
            return None
        initialization = self.initialization or {}
        if "source" not in initialization.get("roles", []):
            await self._send(
                process,
                error_response(message["id"], METHOD_NOT_FOUND, "Method not found"),
            )
            return None
        declared = {
            entry.get("type") for entry in initialization.get("event_types", [])
        }
        if message["params"]["type"] not in declared:
            await self._send(
                process,
                error_response(message["id"], METHOD_NOT_FOUND, "Method not found"),
            )
            return None
        message_id = message["id"]
        if message_id in self._pending_publications:
            await self._send(
                process,
                error_response(
                    message_id, INVALID_REQUEST, "request id is already pending"
                ),
            )
            return None
        maximum = int(initialization.get("max_in_flight", 64))
        if len(self._pending_publications) >= maximum:
            raise _PeerFailed("peer exceeded the negotiated in-flight request limit")
        params = message["params"]
        publication = PublishRequest(
            request_id=message_id,
            type=params["type"],
            payload=params["payload"],
            idempotency_key=params.get("idempotency_key"),
            caused_by=params.get("caused_by"),
            session_id=params.get("session_id"),
            run_id=params.get("run_id"),
            turn_id=params.get("turn_id"),
        )
        self._pending_publications[message_id] = publication
        return publication

    async def _initialize(
        self,
        process: asyncio.subprocess.Process,
        frame_reader: FrameReader,
    ) -> dict[str, Any]:
        try:
            message = await asyncio.wait_for(
                frame_reader.read_frame(), timeout=self.handshake_timeout
            )
        except TimeoutError:
            raise _PeerFailed(
                f"no initialize request within {self.handshake_timeout:.0f}s"
            ) from None
        if message is None:
            raise _PeerFailed("peer exited before initialization")
        if isinstance(message, FrameViolation):
            raise _PeerFailed(
                f"first frame was invalid ({message.rule}): {message.preview()}"
            )
        if message_kind(message) != "request" or message["method"] != "initialize":
            raise _PeerFailed("initialize must be the first peer request")
        params = message["params"]
        roles = params["roles"]
        if any(role not in {"source", "provider"} for role in roles):
            await self._reject_initialization(
                process,
                error_response(
                    message["id"],
                    INVALID_PARAMS,
                    "requested role is unavailable",
                    {"code": "unsupported_role", "retryable": False},
                ),
            )
            raise _PeerFailed(f"unsupported roles {roles!r}")
        if PROTOCOL_VERSION not in params["protocol_versions"]:
            await self._reject_initialization(
                process,
                error_response(
                    message["id"],
                    SERVER_ERROR,
                    "unsupported protocol version",
                    {"code": "unsupported_version", "retryable": False},
                ),
            )
            raise _PeerFailed("no common protocol version")
        heartbeat_seconds = params.get("heartbeat_seconds", 10)
        max_in_flight = params.get("max_in_flight", 64)
        result = {
            "protocol_version": PROTOCOL_VERSION,
            "runtime": {
                "name": self.runtime_name,
                "version": self.runtime_version,
            },
            "roles": roles,
            "config": dict(self.config or {}),
            "heartbeat_seconds": heartbeat_seconds,
            "max_in_flight": max_in_flight,
        }
        await self._send(process, success_response(message["id"], result))
        return {**params, **result}

    async def _reject_initialization(
        self,
        process: asyncio.subprocess.Process,
        response: dict[str, Any],
    ) -> None:
        await self._send(process, response)
        await _wait_or_none(process, self.grace_seconds)

    async def _send_ping(self, process: asyncio.subprocess.Process) -> None:
        maximum = int((self.initialization or {}).get("max_in_flight", 64))
        if len(self._pending_outgoing) >= maximum:
            return
        message_id = next(self._message_ids)
        self._pending_outgoing[message_id] = ("ping", None)
        await self._send(process, request(message_id, "ping", {}))

    async def _send(
        self, process: asyncio.subprocess.Process, message: dict[str, Any]
    ) -> None:
        if process.stdin is None or process.stdin.is_closing():
            raise ConnectionError("connector peer stdin is closed")
        async with self._write_lock:
            process.stdin.write(encode_frame(message))
            await process.stdin.drain()

    def _resolve_outgoing(self, message: dict[str, Any]) -> None:
        pending = self._pending_outgoing.pop(message["id"], None)
        if pending is None:
            logger.warning("dropping response for unknown request id %r", message["id"])
            return
        method, future = pending
        if future is None or future.done():
            return
        if message_kind(message) == "success":
            result = message["result"]
            if isinstance(result, dict):
                future.set_result(result)
            else:
                future.set_exception(
                    ProviderCallError(
                        "invalid_result",
                        f"provider method {method!r} returned a non-object",
                        retryable=False,
                    )
                )
            return
        error = message["error"]
        data = error.get("data")
        stable_code = "provider_error"
        retryable = False
        if isinstance(data, dict):
            if isinstance(data.get("code"), str):
                stable_code = data["code"]
            if isinstance(data.get("retryable"), bool):
                retryable = data["retryable"]
        future.set_exception(
            ProviderCallError(stable_code, error["message"], retryable=retryable)
        )

    def _fail_pending_calls(self, reason: str) -> None:
        pending, self._pending_outgoing = self._pending_outgoing, {}
        for _method, future in pending.values():
            if future is not None and not future.done():
                future.set_exception(
                    ProviderCallError("peer_unavailable", reason, retryable=True)
                )

    async def _kill_current_peer(self) -> None:
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


class _PeerFailed(RuntimeError):
    """Signal that one child incarnation must be replaced."""


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
