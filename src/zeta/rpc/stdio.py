"""Stdio wiring for the Zeta JSON-RPC runtime."""

from __future__ import annotations

import asyncio
from typing import Any, TextIO

from zeta.execute import session_turn_agent
from zeta.orchestration.dispatch import EventDispatcher
from zeta.records.events import Event
from zeta.rpc.jsonrpc import JsonRpcConnection, JsonRpcRouter
from zeta.rpc.routes import (
    RpcClient,
    RunState,
    event_to_wire,
    events_list,
    events_publish,
    initialize,
    session_cancel,
    session_run,
    tools_register,
    tools_respond,
)
from zeta.run.runtime import CancellationToken
from zeta.runtime.local import default_session


def run_stdio(input: TextIO, output: TextIO) -> None:
    """Run the Zeta JSON-RPC server over stdio with explicit route wiring."""

    connection = JsonRpcConnection(input, output)
    session = default_session()
    pending_runs: dict[str, RunState] = {}
    pending_tool_calls: dict[str, asyncio.Future[dict[str, Any]]] = {}

    def cancellation_event_for_run(run_id: str) -> CancellationToken | None:
        state = pending_runs.get(run_id)
        return state.cancellation_event if state is not None else None

    def notify_event(event: Event) -> None:
        connection.notify("events.notify", {"event": event_to_wire(event)})

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
    router = JsonRpcRouter(client)
    router.route("initialize", initialize)
    router.route("events.publish", events_publish)
    router.route("events.list", events_list)
    router.route("session.run", session_run)
    router.route("session.cancel", session_cancel)
    router.route("tools.register", tools_register)
    router.route("tools.respond", tools_respond)
    asyncio.run(connection.serve(router))
