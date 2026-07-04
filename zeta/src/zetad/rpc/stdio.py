"""Stdio wiring for the Zeta JSON-RPC runtime."""

from __future__ import annotations

import asyncio
from typing import Any, TextIO

from zeta.records.events import Event
from zeta.run.context import default_session
from zeta.run.runtime import CancellationToken

from zetad.dispatch import EventDispatcher
from zetad.rpc.jsonrpc import (
    MAX_JSONRPC_LINE_BYTES,
    JsonRpcConnection,
)
from zetad.rpc.routes import (
    RpcClient,
    RunState,
    build_rpc_router,
    event_to_wire,
)
from zetad.session_turn import session_turn_agent


def run_stdio(input: TextIO, output: TextIO) -> None:
    """Run the Zeta JSON-RPC server over stdio with explicit route wiring."""

    asyncio.run(run_stdio_async(input, output))


async def run_stdio_async(input: TextIO, output: TextIO) -> None:
    reader, writer = await stdio_streams(input, output)
    connection = JsonRpcConnection(reader, writer)
    session = default_session()
    pending_runs: dict[str, RunState] = {}
    pending_tool_calls: dict[str, asyncio.Future[dict[str, Any]]] = {}
    background_tasks: set[asyncio.Task[Any]] = set()

    def retain_background_task(awaitable: Any) -> None:
        task = asyncio.create_task(awaitable)
        background_tasks.add(task)
        task.add_done_callback(discard_background_task)

    def discard_background_task(task: asyncio.Task[Any]) -> None:
        background_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    def cancellation_event_for_run(run_id: str) -> CancellationToken | None:
        state = pending_runs.get(run_id)
        return state.cancellation_event if state is not None else None

    def notify_event(event: Event) -> None:
        retain_background_task(
            connection.notify("events.notify", {"event": event_to_wire(event)})
        )

    dispatcher = EventDispatcher(
        session.event_sink,
        executors=[
            session_turn_agent(
                session,
                publish_event=notify_event,
                cancellation_event_for_run=cancellation_event_for_run,
            )
        ],
        publish_event=notify_event,
    )
    client = RpcClient(
        connection=connection,
        session=session,
        dispatcher=dispatcher,
        pending_runs=pending_runs,
        pending_tool_calls=pending_tool_calls,
    )
    router = build_rpc_router(client)
    await connection.serve(router)


async def stdio_streams(
    input: TextIO,
    output: TextIO,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_JSONRPC_LINE_BYTES)
    reader_protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: reader_protocol, input)
    write_transport, write_protocol = await loop.connect_write_pipe(
        lambda: asyncio.streams.FlowControlMixin(loop=loop),
        output,
    )
    writer = asyncio.StreamWriter(write_transport, write_protocol, None, loop)
    return reader, writer
