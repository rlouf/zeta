"""Shared helpers for wire-v0 protocol tests."""

from __future__ import annotations

import asyncio
import sys
import textwrap
from pathlib import Path

from zeta.wire.envelopes import envelope
from zeta.wire.framing import FrameReader, FrameViolation, encode_frame

VECTORS_DIR = Path(__file__).resolve().parents[2] / "spec" / "vectors"

RUNTIME_ID = "zeta-test/0"


def write_child(tmp_path: Path, body: str, name: str = "child.py") -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def sdk_child_source(
    *,
    events_body: str,
    name: str = "fs-inbox",
    plugin_version: str = "0.1.0",
    heartbeat_secs: float = 1,
    ack_window: int = 64,
) -> str:
    """A child that uses the plugin SDK; `events_body` is the generator body."""
    return f"""
        import asyncio

        from zeta.wire.plugin import EventType, SourceEvent, run_source


        async def events():
{textwrap.indent(textwrap.dedent(events_body), "            ")}

        run_source(
            events(),
            name={name!r},
            plugin_version={plugin_version!r},
            event_types=[EventType("file.created", "file.created@1")],
            heartbeat_secs={heartbeat_secs!r},
            ack_window={ack_window!r},
        )
    """


def frame_reader(process: asyncio.subprocess.Process) -> FrameReader:
    assert process.stdout is not None
    return FrameReader(process.stdout)


async def child_stderr(process: asyncio.subprocess.Process) -> str:
    assert process.stderr is not None
    return (await process.stderr.read()).decode()


async def spawn(script: Path) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def read_envelope(reader: FrameReader, timeout: float = 5.0) -> dict:
    frame = await asyncio.wait_for(reader.read_frame(), timeout=timeout)
    assert frame is not None, "stream ended while an envelope was expected"
    assert not isinstance(frame, FrameViolation), frame
    return frame


async def send(process: asyncio.subprocess.Process, message: dict) -> None:
    assert process.stdin is not None
    process.stdin.write(encode_frame(message))
    await process.stdin.drain()


async def complete_handshake(
    process: asyncio.subprocess.Process,
    reader: FrameReader,
) -> dict:
    hello = await read_envelope(reader)
    assert hello["kind"] == "hello"
    await send(
        process,
        envelope("hello_ack", "m-t-1", protocol_version=0, runtime=RUNTIME_ID),
    )
    return hello


async def finished(process: asyncio.subprocess.Process, timeout: float = 5.0) -> int:
    return await asyncio.wait_for(process.wait(), timeout=timeout)
