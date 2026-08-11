"""Shared helpers for IPC protocol tests."""

from __future__ import annotations

import asyncio
import sys
import textwrap
from pathlib import Path

from zeta.ipc.framing import FrameReader, FrameViolation, encode_frame
from zeta.ipc.messages import success_response

VECTORS_DIR = Path(__file__).resolve().parents[2] / "spec" / "vectors"
RUNTIME = {"name": "zeta-test", "version": "0"}


def write_child(tmp_path: Path, body: str, name: str = "child.py") -> Path:
    child_path = tmp_path / name
    child_path.write_text(textwrap.dedent(body), encoding="utf-8")
    return child_path


def sdk_child_peer(
    *,
    events_body: str,
    name: str = "fs-inbox",
    peer_version: str = "0.1.0",
    heartbeat_seconds: float = 10,
    max_in_flight: int = 64,
) -> str:
    """Build a child that publishes from the supplied generator body."""
    return f"""
        import asyncio

        from zeta.ipc.client import EventType, SourceEvent, run_peer


        async def events():
{textwrap.indent(textwrap.dedent(events_body), "            ")}

        run_peer(
            events(),
            name={name!r},
            peer_version={peer_version!r},
            event_types=[EventType("file.created", "file.created@1")],
            heartbeat_seconds={heartbeat_seconds!r},
            max_in_flight={max_in_flight!r},
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


async def read_message(reader: FrameReader, timeout: float = 5.0) -> dict:
    frame = await asyncio.wait_for(reader.read_frame(), timeout=timeout)
    assert frame is not None, "stream ended while a message was expected"
    assert not isinstance(frame, FrameViolation), frame
    return frame


async def send(process: asyncio.subprocess.Process, message: dict) -> None:
    assert process.stdin is not None
    process.stdin.write(encode_frame(message))
    await process.stdin.drain()


async def complete_initialize(
    process: asyncio.subprocess.Process,
    reader: FrameReader,
    *,
    config: dict | None = None,
) -> dict:
    initialize = await read_message(reader)
    assert initialize["method"] == "initialize"
    params = initialize["params"]
    await send(
        process,
        success_response(
            initialize["id"],
            {
                "protocol_version": 0,
                "runtime": RUNTIME,
                "roles": params["roles"],
                "config": config or {},
                "heartbeat_seconds": params.get("heartbeat_seconds", 10),
                "max_in_flight": params.get("max_in_flight", 64),
            },
        ),
    )
    return initialize


async def finished(process: asyncio.subprocess.Process, timeout: float = 5.0) -> int:
    return await asyncio.wait_for(process.wait(), timeout=timeout)
