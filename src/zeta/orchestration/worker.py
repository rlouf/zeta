"""Run event-driven Zeta work from a durable queue."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator

from zeta.agents.manifest import PluginResolver, selected_plugin_event
from zeta.agents.resources import (
    AgentProject,
    load_agent_project,
    validate_agent_project,
)
from zeta.agents.spec import EgressBinding, IngressBinding
from zeta.capabilities.registry import CapabilityRegistry
from zeta.events import DraftEvent, Event
from zeta.orchestration.agents import (
    AgentDefinition,
    AgentInvocation,
    EventPattern,
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
    plugin_resolver: PluginResolver | None = None
    worker_name: str = LOCAL_WORKER_NAME
    max_concurrent: int = 1

    def close(self) -> None:
        self.events.close()


def build_worker_services(
    *,
    project_root: Path,
    state_dir: Path | None = None,
    tool_registry: CapabilityRegistry | None = None,
    plugin_resolver: PluginResolver | None = None,
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
        plugin_resolver=plugin_resolver,
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
    project = load_agent_project(
        runtime.project_root / "agents",
        plugin_resolver=runtime.plugin_resolver,
    )
    validate_agent_project(project)
    return tuple(
        [
            *(
                agent
                for spec in project.specs
                for agent in compile_agent_definitions(
                    spec,
                    event_registry=project.events,
                    run_turn=project_agent_run_turn(runtime),
                )
            ),
            *project_egress_executors(project),
        ]
    )


def project_egress_executors(
    project: AgentProject,
) -> tuple[ExecutableAgent, ...]:
    executors: list[ExecutableAgent] = []
    for spec in project.specs:
        for index, binding in enumerate(spec.egress):
            plugin = project.plugins.get(binding.sink)
            if plugin is None:
                continue
            event_type = selected_plugin_event(
                binding.accepts,
                plugin.egress,
                "egress sink",
                binding.sink,
                "accept",
            )
            handler = plugin.egress_handlers.get(event_type)
            if handler is None:
                continue
            agent_id = f"egress:{spec.slug}:{index}:{binding.sink}:{event_type}"
            executors.append(
                ExecutableAgent(
                    AgentDefinition(
                        agent_id,
                        (EventPattern(event_type),),
                        dispatch_mode="one_shot",
                    ),
                    run=egress_runner(binding, handler),
                )
            )
    return tuple(executors)


def egress_runner(binding: EgressBinding, handler):
    async def run(invocation: AgentInvocation) -> dict[str, Any]:
        event = invocation.triggering_event
        idempotency_key = egress_idempotency_key(binding, event)
        await invocation.publish(
            DraftEvent(
                "runtime.egress.started",
                f"egress:{binding.sink}",
                {
                    "sink": binding.sink,
                    "event_id": event.id,
                    "event_type": event.event_type,
                    "idempotency_key": idempotency_key,
                },
                idempotency_key=f"runtime.egress.started:{idempotency_key}",
            )
        )
        try:
            result = handler(event, binding, idempotency_key)
            if inspect.isawaitable(result):
                result = await result
            result_payload = dict(result or {})
        except Exception as exc:
            await invocation.publish(
                DraftEvent(
                    "runtime.egress.failed",
                    f"egress:{binding.sink}",
                    {
                        "sink": binding.sink,
                        "event_id": event.id,
                        "event_type": event.event_type,
                        "idempotency_key": idempotency_key,
                        "error": str(exc),
                    },
                    idempotency_key=f"runtime.egress.failed:{idempotency_key}",
                )
            )
            logger.exception("egress sink %r failed", binding.sink)
            return {
                "egress": {
                    "sink": binding.sink,
                    "event_id": event.id,
                    "failed": True,
                    "error": str(exc),
                }
            }
        await invocation.publish(
            DraftEvent(
                "runtime.egress.completed",
                f"egress:{binding.sink}",
                {
                    "sink": binding.sink,
                    "event_id": event.id,
                    "event_type": event.event_type,
                    "idempotency_key": idempotency_key,
                    "result": result_payload,
                },
                idempotency_key=f"runtime.egress.completed:{idempotency_key}",
            )
        )
        return {
            "egress": {
                "sink": binding.sink,
                "event_id": event.id,
                "result": result_payload,
            }
        }

    return run


async def run_ingress_once(runtime: WorkerServices) -> int:
    project = load_agent_project(
        runtime.project_root / "agents",
        plugin_resolver=runtime.plugin_resolver,
    )
    validate_agent_project(project)
    inserted = 0
    for spec in project.specs:
        for binding in spec.ingress:
            plugin = project.plugins.get(binding.source)
            if plugin is None:
                continue
            event_type = selected_plugin_event(
                binding.produces,
                plugin.ingress,
                "ingress source",
                binding.source,
                "produce",
            )
            poller = plugin.ingress_pollers.get(event_type)
            if poller is None:
                continue
            drafts = poller(binding)
            if inspect.isawaitable(drafts):
                drafts = await drafts
            for draft in cast(Iterable[DraftEvent], drafts):
                if draft.event_type != event_type:
                    raise RuntimeError(
                        f"ingress source {binding.source!r} produced {draft.event_type!r}, "
                        f"expected {event_type!r}"
                    )
                validate_event_payload(project.events, draft)
                outcome = runtime.events.accept(
                    DraftEvent(
                        draft.event_type,
                        draft.source,
                        draft.payload,
                        idempotency_key=ingress_idempotency_key(binding, draft),
                        caused_by=draft.caused_by,
                        session_id=draft.session_id,
                        run_id=draft.run_id,
                        turn_id=draft.turn_id,
                    )
                )
                if outcome.inserted:
                    inserted += 1
    return inserted


async def run_ingress_forever(
    runtime: WorkerServices,
    *,
    poll_interval_seconds: float = 1.0,
    stop_event: asyncio.Event | None = None,
) -> None:
    while stop_event is None or not stop_event.is_set():
        try:
            await run_ingress_once(runtime)
        except Exception:
            logger.exception("ingress polling failed")
        await asyncio.sleep(poll_interval_seconds)


def validate_event_payload(events, draft: DraftEvent) -> None:
    schema = events.schema(draft.event_type)
    if schema is not None:
        Draft202012Validator(schema).validate(dict(draft.payload))


def ingress_idempotency_key(binding: IngressBinding, draft: DraftEvent) -> str:
    if binding.idempotency_key is None:
        raise RuntimeError(
            f"ingress source {binding.source!r} requires idempotency_key"
        )
    return render_template(binding.idempotency_key, draft)


def egress_idempotency_key(binding: EgressBinding, event: Event) -> str:
    if binding.idempotency_key is None:
        return f"{binding.sink}:{event.id}"
    return render_template(binding.idempotency_key, event)


def render_template(template: str, event: DraftEvent | Event) -> str:
    return template.format(event=event, **dict(event.payload))


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
        if is_runtime_event(event) or event.event_type.startswith(
            ("zeta.", "rpc.", "scheduler.tick.")
        ):
            continue
        if events.event_has_queue_item(event.id):
            continue
        events.ensure_pending_queue_item(event)
        queued += 1
    return queued


def is_runtime_event(event: Event) -> bool:
    return event.event_type.startswith(
        ("runtime.queue_item.", "runtime.attempt.", "runtime.egress.")
    )


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
    ingress_task = start_ingress_task(
        runtime,
        poll_interval_seconds=poll_interval_seconds,
        stop_event=stop_event,
    )
    try:
        await run_worker_loop(
            runtime,
            running,
            poll_interval_seconds=poll_interval_seconds,
            stop_event=stop_event,
        )
    finally:
        await stop_ingress_task(ingress_task)
        await log_worker_results(running)


def start_ingress_task(
    runtime: WorkerServices,
    *,
    poll_interval_seconds: float,
    stop_event: asyncio.Event | None,
) -> asyncio.Task[None] | None:
    if runtime.plugin_resolver is None:
        return None
    return asyncio.create_task(
        run_ingress_forever(
            runtime,
            poll_interval_seconds=poll_interval_seconds,
            stop_event=stop_event,
        )
    )


async def run_worker_loop(
    runtime: WorkerServices,
    running: set[asyncio.Task[str]],
    *,
    poll_interval_seconds: float,
    stop_event: asyncio.Event | None,
) -> None:
    should_refill = True
    while stop_event is None or not stop_event.is_set():
        if should_refill:
            refill_worker_tasks(runtime, running)
        if not running:
            await asyncio.sleep(poll_interval_seconds)
            should_refill = True
            continue
        done, running_tasks = await asyncio.wait(
            running,
            return_when=asyncio.FIRST_COMPLETED,
        )
        running.clear()
        running.update(running_tasks)
        done.update(reap_finished_tasks(running))
        saw_empty_queue = task_results_saw_empty_queue(done)
        should_refill = not saw_empty_queue
        if saw_empty_queue and not running:
            await asyncio.sleep(poll_interval_seconds)
            should_refill = True


def refill_worker_tasks(
    runtime: WorkerServices,
    running: set[asyncio.Task[str]],
) -> None:
    while len(running) < runtime.max_concurrent:
        running.add(asyncio.create_task(run_once(runtime)))


def reap_finished_tasks(running: set[asyncio.Task[str]]) -> set[asyncio.Task[str]]:
    finished = {task for task in running if task.done()}
    running.difference_update(finished)
    return finished


def task_results_saw_empty_queue(tasks: set[asyncio.Task[str]]) -> bool:
    return any(_run_once_task_result(task) == "queue empty" for task in tasks)


async def stop_ingress_task(task: asyncio.Task[None] | None) -> None:
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def log_worker_results(running: set[asyncio.Task[str]]) -> None:
    if not running:
        return
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
