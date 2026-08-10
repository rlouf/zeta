"""ndjson framing over asyncio streams (spec §2).

Readers never raise on peer garbage: every line comes back as either a
validated envelope or a `FrameViolation` the caller decides about, so
one misbehaving peer cannot take its supervisor down.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from zeta.wire.envelopes import EnvelopeError, canonical_json, validate_envelope

MAX_FRAME_BYTES = 8 * 1024 * 1024
_READ_CHUNK = 64 * 1024


@dataclass(frozen=True)
class FrameViolation:
    """One stream line that was not a valid envelope."""

    rule: str
    detail: str
    line: bytes

    def preview(self, limit: int = 200) -> str:
        text = self.line[:limit].decode("utf-8", errors="replace")
        return text + ("…" if len(self.line) > limit else "")


def encode_frame(envelope: dict[str, Any]) -> bytes:
    return canonical_json(envelope).encode() + b"\n"


def decode_frame(line: bytes) -> dict[str, Any] | FrameViolation:
    stripped = line.rstrip(b"\r\n")
    if not stripped:
        return FrameViolation("empty_line", "empty line", line)
    try:
        parsed = json.loads(stripped)
    except (ValueError, UnicodeDecodeError) as exc:
        return FrameViolation("bad_json", str(exc), line)
    try:
        return validate_envelope(parsed)
    except EnvelopeError as exc:
        return FrameViolation(exc.rule, str(exc), line)


class FrameReader:
    """Buffered line reader with its own bounded buffer.

    Owning the buffer (instead of `StreamReader.readuntil`) keeps
    byte order intact when an overlong line has to be discarded
    mid-stream.
    """

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
        """Read one line; None at end-of-stream. Never raises on junk."""
        while True:
            newline = self._buffer.find(b"\n")
            if newline != -1:
                line = self._buffer[: newline + 1]
                self._buffer = self._buffer[newline + 1 :]
                if len(line) > self._max_frame_bytes:
                    return self._violation_for_overlong(line)
                return decode_frame(line)
            if self._eof:
                if not self._buffer:
                    return None
                line, self._buffer = self._buffer, b""
                return decode_frame(line)
            if len(self._buffer) > self._max_frame_bytes:
                return await self._discard_until_newline()
            chunk = await self._reader.read(_READ_CHUNK)
            if not chunk:
                self._eof = True
            else:
                self._buffer += chunk

    def _violation_for_overlong(self, line: bytes) -> FrameViolation:
        return FrameViolation(
            "frame_too_long",
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
        return FrameViolation(
            "frame_too_long",
            f"line exceeded the {self._max_frame_bytes}-byte frame limit",
            head,
        )
