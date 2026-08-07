"""Serve event-log JSON-RPC requests from the worker loop.

The harness owns the worker loop but must not know a transport exists. It
offers a hook instead, and this module fills it. Whoever composes the worker
decides whether event-log RPC is served at all.
"""

from __future__ import annotations

from zeta.events import Event
from zeta.harness.dispatch import QueueingDispatcher
from zeta.harness.session_turn import session_turn_agent
from zeta.harness.worker import (
    ATTEMPT_HEARTBEAT_INTERVAL_SECONDS,
    QUEUE_LEASE_MS,
    WorkerServices,
    project_executors,
)
from zeta.journal.sqlite import zeta_sqlite_path
from zeta.journal.store import Filter
from zeta.loop.runtime_context import RuntimeContext
from zeta.rpc.routes import (
    RPC_REQUESTED,
    RpcClient,
    RunState,
    build_rpc_router,
    rpc_request_has_terminal_response,
    run_eventlog_rpc_once,
)
from zeta.substrate import SqliteObjectStore


def pending_rpc_request(runtime: WorkerServices) -> Event | None:
    for event in runtime.events.list_events(Filter(event_type=RPC_REQUESTED)):
        if not rpc_request_has_terminal_response(runtime.events, event):
            return event
    return None


async def run_eventlog_rpc_request(
    runtime: WorkerServices,
    request: Event,
) -> Event | None:
    project_snapshot = runtime.project_snapshot
    session_id = request.session_id or "default"
    trace_store = SqliteObjectStore(
        zeta_sqlite_path(runtime.state_dir),
        session_id=session_id,
    )
    content_store = SqliteObjectStore(zeta_sqlite_path(runtime.state_dir))
    session = RuntimeContext(
        session_id=session_id,
        event_sink=runtime.events,
        trace_store=trace_store,
        tool_registry=runtime.tool_registry,
        state_dir=runtime.state_dir,
        session_dir=runtime.state_dir / "sessions" / session_id,
        content_store=content_store,
    )
    pending_runs: dict[str, RunState] = {}

    dispatcher = QueueingDispatcher(
        runtime.events,
        runtime.events,
        executors=(
            session_turn_agent(
                session,
                publish_event=lambda _event: None,
            ),
            *project_executors(runtime),
        ),
        worker_name=runtime.worker_name,
        heartbeat_interval_seconds=ATTEMPT_HEARTBEAT_INTERVAL_SECONDS,
        lease_ms=QUEUE_LEASE_MS,
        retry_policy=runtime.retry_policy,
    )
    client = RpcClient(
        connection=None,
        session=session,
        dispatcher=dispatcher,
        pending_runs=pending_runs,
        pending_tool_calls={},
        project_snapshot=project_snapshot,
    )
    router = build_rpc_router(client)
    try:
        return await run_eventlog_rpc_once(router)
    finally:
        content_store.close()
        trace_store.close()


async def eventlog_rpc_step(runtime: WorkerServices) -> str | None:
    """Serve one pending event-log RPC request, if any is waiting.

    Returns a message when a request was serviced, so the worker loop can
    report it, and None when there was nothing to do.
    """
    request = pending_rpc_request(runtime)
    if request is None:
        return None
    await run_eventlog_rpc_request(runtime, request)
    return f"rpc {request.id}"
