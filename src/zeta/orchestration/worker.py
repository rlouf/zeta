"""Run event-driven Zeta work from a durable queue."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zeta.agents.resources import load_agent_project, validate_agent_project
from zeta.capabilities.registry import CapabilityRegistry
from zeta.events import Event
from zeta.orchestration.agents import (
    AgentInvocation,
    ExecutableAgent,
    agent_session_id,
    compile_agent_definitions,
)
from zeta.orchestration.dispatch import EventDispatcher
from zeta.orchestration.session_turn_agent import session_turn_agent
from zeta.records.stores import (
    Filter,
    QueueClaim,
    SqliteEventStore,
    SqliteObjectStore,
    event_store_path,
    zeta_sqlite_path,
)
from zeta.run.config import AgentConfig
from zeta.run.context import RuntimeContext
from zeta.run.runtime import AgentRunRequest, run_agent

logger = logging.getLogger(__name__)

LOCAL_WORKER_NAME = "local-runtime"
QUEUE_LEASE_MS = 60_000
ATTEMPT_HEARTBEAT_INTERVAL_SECONDS = 15.0


@dataclass(frozen=True)
class WorkerServices:
    """Project-local resources consumed by the queue worker."""

    project_root: Path
    state_dir: Path
    events: SqliteEventStore
    tool_registry: CapabilityRegistry = field(default_factory=CapabilityRegistry)
    worker_name: str = LOCAL_WORKER_NAME
    max_concurrent: int = 1

    def close(self) -> None:
        self.events.close()


def build_worker_services(
    *,
    project_root: Path,
    state_dir: Path | None = None,
    tool_registry: CapabilityRegistry | None = None,
) -> WorkerServices:
    resolved_project_root = project_root.expanduser().resolve()
    resolved_state_dir = (
        state_dir.expanduser().resolve()
        if state_dir is not None
        else resolved_project_root / ".zeta"
    )
    return WorkerServices(
        project_root=resolved_project_root,
        state_dir=resolved_state_dir,
        events=SqliteEventStore(event_store_path(resolved_state_dir)),
        tool_registry=tool_registry or CapabilityRegistry(),
    )


async def run_once(runtime: WorkerServices) -> str:
    rpc_request = pending_rpc_request(runtime)
    if rpc_request is not None:
        await run_eventlog_rpc_request(runtime, rpc_request)
        return f"rpc {rpc_request.id}"
    enqueue_pending_events(runtime.events)
    executors = project_executors(runtime)
    return await run_available_queue_item(
        runtime.events,
        executors=executors,
        worker_name=runtime.worker_name,
        heartbeat_interval_seconds=ATTEMPT_HEARTBEAT_INTERVAL_SECONDS,
        lease_ms=QUEUE_LEASE_MS,
    )


def project_executors(runtime: WorkerServices) -> tuple[ExecutableAgent, ...]:
    project = load_agent_project(runtime.project_root / "agents")
    validate_agent_project(project)
    return tuple(
        agent
        for spec in project.specs
        for agent in compile_agent_definitions(
            spec,
            event_registry=project.events,
            run_turn=project_agent_run_turn(runtime),
        )
    )


def project_agent_run_turn(runtime: WorkerServices):
    async def run_turn(
        objective: str,
        timeline: list[dict[str, object]],
        config: AgentConfig,
        **kwargs: Any,
    ) -> Any:
        del timeline
        invocation = kwargs.get("agent_invocation")
        if not isinstance(invocation, AgentInvocation):
            raise RuntimeError("authored agent run requires an invocation")
        session_id = agent_session_id(invocation.agent, invocation.triggering_event)
        trace_store = SqliteObjectStore(
            zeta_sqlite_path(runtime.state_dir),
            session_id=session_id,
        )
        runtime_context = RuntimeContext(
            session_id=session_id,
            event_sink=runtime.events,
            trace_store=trace_store,
            tool_registry=runtime.tool_registry,
            state_dir=runtime.state_dir,
            session_dir=runtime.state_dir / "sessions" / session_id,
        )
        run_id = invocation.run_id or (
            f"run_{invocation.attempt_id}"
            if invocation.attempt_id is not None
            else f"run_{invocation.triggering_event.id}"
        )
        try:
            return await run_agent(
                AgentRunRequest(
                    objective=objective,
                    workflow="agent",
                    runtime="zeta-agent",
                    tools=tuple(config.allowed_capabilities or ()),
                    context=kwargs.get("context", ""),
                    config=config,
                ),
                run_id=run_id,
                caused_by=kwargs.get("caused_by") or invocation.triggering_event.id,
                publish_event=lambda _event: None,
                runtime_context=runtime_context,
                cancellation_event=None,
            )
        finally:
            trace_store.close()

    return run_turn


async def run_available_queue_item(
    events: SqliteEventStore,
    executors: tuple[ExecutableAgent, ...],
    *,
    worker_name: str,
    skipped_queue_items: set[str] | None = None,
    lease_ms: int = QUEUE_LEASE_MS,
    heartbeat_interval_seconds: float = ATTEMPT_HEARTBEAT_INTERVAL_SECONDS,
) -> str:
    dispatcher = EventDispatcher(
        events,
        executors=executors,
        worker_name=worker_name,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        lease_ms=lease_ms,
    )
    skipped = skipped_queue_items or set()
    while True:
        claimed = claim_available_queue_item(
            events,
            worker_name=worker_name,
            skipped_queue_items=skipped,
            lease_ms=lease_ms,
        )
        if claimed is None:
            return "queue empty"
        lock_keys = queue_item_lock_keys(events, executors, claimed.queue_item_id)
        lock_owner = queue_item_lock_owner(claimed)
        now_ms = runtime_time_ms()
        if not events.acquire_locks(
            lock_keys,
            lock_owner,
            lease_ms=lease_ms,
            now_ms=now_ms,
        ):
            events.release_queue_claim(
                claimed.queue_item_id,
                worker_name,
                claim_token=claimed.token,
                now_ms=now_ms,
            )
            skipped.add(claimed.queue_item_id)
            continue
        dispatcher.claim_token = claimed.token
        try:
            lifecycle_events = await dispatcher.run_queue_item(claimed.queue_item_id)
            return run_once_message(claimed.queue_item_id, lifecycle_events)
        finally:
            events.release_locks(lock_keys, lock_owner)


def enqueue_pending_events(events: SqliteEventStore) -> int:
    queued = 0
    for event in events.list_events(Filter()):
        if is_runtime_event(event) or event.event_type.startswith(("zeta.", "rpc.")):
            continue
        if events.event_has_queue_item(event.id):
            continue
        events.ensure_pending_queue_item(event)
        queued += 1
    return queued


def is_runtime_event(event: Event) -> bool:
    return event.event_type.startswith(("runtime.queue_item.", "runtime.attempt."))


def pending_rpc_request(runtime: WorkerServices) -> Event | None:
    from zeta.rpc.routes import RPC_REQUESTED, rpc_request_has_terminal_response

    for event in runtime.events.list_events(Filter(event_type=RPC_REQUESTED)):
        if not rpc_request_has_terminal_response(runtime.events, event):
            return event
    return None


async def run_eventlog_rpc_request(
    runtime: WorkerServices,
    request: Event,
) -> Event | None:
    from zeta.rpc.jsonrpc import JsonRpcRouter
    from zeta.rpc.routes import (
        RpcClient,
        RunState,
        events_list,
        events_publish,
        initialize,
        run_eventlog_rpc_once,
        session_cancel,
        session_run,
        tools_register,
        tools_respond,
    )

    session_id = request.session_id or "default"
    trace_store = SqliteObjectStore(
        zeta_sqlite_path(runtime.state_dir),
        session_id=session_id,
    )
    session = RuntimeContext(
        session_id=session_id,
        event_sink=runtime.events,
        trace_store=trace_store,
        tool_registry=runtime.tool_registry,
        state_dir=runtime.state_dir,
        session_dir=runtime.state_dir / "sessions" / session_id,
    )
    pending_runs: dict[str, RunState] = {}

    def cancellation_event_for_run(run_id: str) -> asyncio.Event | None:
        state = pending_runs.get(run_id)
        return state.cancellation_event if state is not None else None

    dispatcher = EventDispatcher(
        runtime.events,
        executors=(
            session_turn_agent(
                session,
                publish_event=lambda _event: None,
                cancellation_event_for_run=cancellation_event_for_run,
            ),
            *project_executors(runtime),
        ),
        worker_name=runtime.worker_name,
        heartbeat_interval_seconds=ATTEMPT_HEARTBEAT_INTERVAL_SECONDS,
        lease_ms=QUEUE_LEASE_MS,
    )
    client = RpcClient(
        connection=None,
        session=session,
        dispatcher=dispatcher,
        pending_runs=pending_runs,
        pending_tool_calls={},
    )
    router = JsonRpcRouter(client)
    router.route("initialize", initialize)
    router.route("events.publish", events_publish)
    router.route("events.list", events_list)
    router.route("session.run", session_run)
    router.route("session.cancel", session_cancel)
    router.route("tools.register", tools_register)
    router.route("tools.respond", tools_respond)
    try:
        return await run_eventlog_rpc_once(router)
    finally:
        trace_store.close()


def run_once_message(queue_item_id: str, lifecycle_events: list[Event]) -> str:
    for event in lifecycle_events:
        if event.event_type == "runtime.queue_item.unhandled":
            return f"routed {event.payload['event_id']}"
        if event.event_type == "runtime.queue_item.available" and event.payload.get(
            "target_agent"
        ):
            return f"routed {event.payload['event_id']}"
    return f"ran {queue_item_id}"


def claim_available_queue_item(
    events: SqliteEventStore,
    *,
    worker_name: str,
    skipped_queue_items: set[str] | None = None,
    lease_ms: int = QUEUE_LEASE_MS,
) -> QueueClaim | None:
    now_ms = runtime_time_ms()
    events.reconcile_expired_queue_claims(now_ms=now_ms)
    events.reconcile_expired_locks(now_ms=now_ms)
    return events.claim_next_queue_item(
        worker_name,
        lease_ms=lease_ms,
        now_ms=now_ms,
        exclude_queue_item_ids=skipped_queue_items or (),
    )


def queue_item_lock_keys(
    events: SqliteEventStore,
    executors: tuple[ExecutableAgent, ...],
    queue_item_id: str,
) -> tuple[str, ...]:
    row = events.queue_item(queue_item_id)
    if row is None:
        return ()
    target_agent = str(row["target_agent"])
    if target_agent:
        return agent_lock_keys(executors, target_agent)
    event = events.get(str(row["event_id"]))
    if event is None:
        return ()
    matching_executors = [
        agent for agent in executors if agent.definition.accepts(event)
    ]
    if len(matching_executors) != 1:
        return ()
    return matching_executors[0].definition.lock_keys


def agent_lock_keys(
    executors: tuple[ExecutableAgent, ...],
    agent_id: str,
) -> tuple[str, ...]:
    for agent in executors:
        if agent.definition.agent_id == agent_id:
            return agent.definition.lock_keys
    return ()


def queue_item_lock_owner(claim: QueueClaim) -> str:
    return claim.token


def runtime_time_ms() -> int:
    return time.time_ns() // 1_000_000


async def run_forever(
    runtime: WorkerServices,
    *,
    poll_interval_seconds: float = 1.0,
    stop_event: asyncio.Event | None = None,
) -> None:
    running: set[asyncio.Task[str]] = set()
    should_refill = True
    while stop_event is None or not stop_event.is_set():
        if should_refill:
            while len(running) < runtime.max_concurrent:
                running.add(asyncio.create_task(run_once(runtime)))
        if not running:
            await asyncio.sleep(poll_interval_seconds)
            should_refill = True
            continue
        done, running = await asyncio.wait(
            running,
            return_when=asyncio.FIRST_COMPLETED,
        )
        finished = {task for task in running if task.done()}
        if finished:
            done.update(finished)
            running.difference_update(finished)
        saw_empty_queue = False
        for task in done:
            if _run_once_task_result(task) == "queue empty":
                saw_empty_queue = True
        should_refill = not saw_empty_queue
        if saw_empty_queue and not running:
            await asyncio.sleep(poll_interval_seconds)
            should_refill = True
    if running:
        results = await asyncio.gather(*running, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.error(
                    "queue worker task failed",
                    exc_info=(type(result), result, result.__traceback__),
                )


def _run_once_task_result(task: asyncio.Task[str]) -> str | None:
    try:
        return task.result()
    except Exception:
        logger.exception("queue worker task failed")
        return None
