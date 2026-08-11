"""Stdio wiring for the Zeta IPC runtime."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any, TextIO

from zeta._version import __version__
from zeta.events import Event
from zeta.harness.dispatch import QueueingDispatcher
from zeta.harness.session_turn import session_turn_agent
from zeta.harness.worker import build_worker_services
from zeta.ipc.connection import JsonRpcConnection
from zeta.ipc.framing import MAX_FRAME_BYTES
from zeta.ipc.routes import IpcClient, build_ipc_router
from zeta.journal.wire import event_to_wire
from zeta.loop.runtime_context import default_session


def run_stdio(input: TextIO, output: TextIO) -> None:
    """Run the Zeta IPC server over standard input and output."""

    asyncio.run(run_stdio_async(input, output))


async def run_stdio_async(input: TextIO, output: TextIO) -> None:
    reader, writer = await stdio_streams(input, output)
    connection = JsonRpcConnection(
        reader,
        writer,
        runtime_name="zeta",
        runtime_version=__version__,
    )
    session = default_session()
    session.event_sink.close()
    runtime = build_worker_services(
        project_root=Path.cwd(),
        state_dir=session.state_dir,
        tool_registry=session.tool_registry,
    )
    event_store = runtime.events
    session = replace(
        session,
        event_sink=event_store,
    )
    background_tasks: set[asyncio.Task[Any]] = set()

    def retain_background_task(awaitable: Any) -> None:
        task = asyncio.create_task(awaitable)
        background_tasks.add(task)
        task.add_done_callback(discard_background_task)

    def discard_background_task(task: asyncio.Task[Any]) -> None:
        background_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    def notify_event(event: Event) -> None:
        retain_background_task(
            connection.notify("event", {"event": event_to_wire(event)})
        )

    dispatcher = QueueingDispatcher(
        event_store,
        event_store,
        executors=[
            session_turn_agent(
                session,
                publish_event=notify_event,
            )
        ],
        publish_event=notify_event,
        worker_name="stdio",
    )
    client = IpcClient(
        connection=connection,
        session=session,
        dispatcher=dispatcher,
        background_tasks=background_tasks,
        project_snapshot=runtime.project_snapshot,
    )
    router = build_ipc_router(client)
    try:
        await connection.serve(router)
    finally:
        await runtime.aclose()


async def stdio_streams(
    input: TextIO,
    output: TextIO,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_FRAME_BYTES)
    reader_protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: reader_protocol, input)
    write_transport, write_protocol = await loop.connect_write_pipe(
        lambda: asyncio.streams.FlowControlMixin(loop=loop),
        output,
    )
    writer = asyncio.StreamWriter(write_transport, write_protocol, None, loop)
    return reader, writer
