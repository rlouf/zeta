"""Bounded UTF-8 NDJSON framing for IPC messages."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from zeta.ipc.messages import (
    INVALID_REQUEST,
    PARSE_ERROR,
    MessageError,
    compact_json_bytes,
    validate_message,
)

MAX_FRAME_BYTES = 8 * 1024 * 1024
_READ_CHUNK = 64 * 1024


@dataclass(frozen=True)
class FrameViolation:
    """One bounded description of an invalid input line."""

    rule: str
    code: int
    detail: str
    line: bytes

    def preview(self, limit: int = 200) -> str:
        text = self.line[:limit].decode("utf-8", errors="replace")
        return text + ("…" if len(self.line) > limit else "")


def encode_frame(message: dict[str, Any]) -> bytes:
    """Encode one validated compact message and its terminating newline."""
    validate_message(message)
    return compact_json_bytes(message) + b"\n"


def decode_frame(line: bytes) -> dict[str, Any] | FrameViolation:
    """Decode one line without raising on peer-controlled input."""
    content = line[:-1] if line.endswith(b"\n") else line
    content = content.rstrip(b"\r")
    if not content:
        return FrameViolation("empty_line", PARSE_ERROR, "empty line", line)
    try:
        text = content.decode("utf-8")
        parsed = json.loads(text, parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        return FrameViolation("parse_error", PARSE_ERROR, str(exc), line)
    try:
        return validate_message(parsed)
    except MessageError as exc:
        rule = "invalid_request" if exc.code == INVALID_REQUEST else "invalid_params"
        return FrameViolation(rule, exc.code, str(exc), line)


class FrameReader:
    """Read bounded lines while retaining bytes after one bad frame."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        *,
        max_frame_bytes: int = MAX_FRAME_BYTES,
    ) -> None:
        self._reader = reader
        self._buffer = b""
        self._eof = False
        self._max_frame_bytes = max_frame_bytes

    async def read_frame(self) -> dict[str, Any] | FrameViolation | None:
        """Read one message or violation, accepting a complete final EOF object."""
        while True:
            newline = self._buffer.find(b"\n")
            if newline != -1:
                line = self._buffer[: newline + 1]
                self._buffer = self._buffer[newline + 1 :]
                if newline > self._max_frame_bytes:
                    return self._overlong(line)
                return decode_frame(line)
            if self._eof:
                if not self._buffer:
                    return None
                line, self._buffer = self._buffer, b""
                if len(line) > self._max_frame_bytes:
                    return self._overlong(line)
                return decode_frame(line)
            if len(self._buffer) > self._max_frame_bytes:
                return await self._discard_until_newline()
            chunk = await self._reader.read(_READ_CHUNK)
            if chunk:
                self._buffer += chunk
            else:
                self._eof = True

    def _overlong(self, line: bytes) -> FrameViolation:
        return FrameViolation(
            "frame_too_long",
            PARSE_ERROR,
            f"line exceeded the {self._max_frame_bytes}-byte frame limit",
            line[:1024],
        )

    async def _discard_until_newline(self) -> FrameViolation:
        head = self._buffer[:1024]
        self._buffer = b""
        while not self._eof:
            chunk = await self._reader.read(_READ_CHUNK)
            if not chunk:
                self._eof = True
                break
            newline = chunk.find(b"\n")
            if newline != -1:
                self._buffer = chunk[newline + 1 :]
                break
        return self._overlong(head)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"{value} is not valid JSON")
