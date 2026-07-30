"""Run event-driven Zeta work from a durable queue."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from functools import cached_property
from pathlib import Path
from typing import Any

from connectors import (
    EventConnectorRegistry,
)
from zeta.agents.resources import load_connector_registry
from zeta.agents.spec import executor_config
from zeta.capabilities.execution import (
    ToolExecutor,
    ToolExecutorProviderRegistry,
    load_tool_executor_provider_registry,
)
from zeta.capabilities.registry import CapabilityRegistry
from zeta.events import Event
from zeta.models.profiles import ModelSelection, active_model_selection
from zeta.records.stores.event_store import Filter
from zeta.records.stores.sqlite import (
    event_store_path,
    resolve_state_dir,
    zeta_sqlite_path,
)
from zeta.run.config import AgentConfig
from zeta.run.outcomes import AgentRunResult
from zeta.run.runtime import AgentRunRequest, run_agent
from zeta.run.runtime_context import RuntimeContext
from zeta.substrate import SqliteObjectStore

from zetad.agents import (
    AgentDefinition,
    AgentInvocation,
    ExecutableAgent,
    compile_agent_definitions,
    config_for_spec,
)
from zetad.connector_bridge import (
    handle_push_ingress_request,
    project_egress_executors,
    run_ingress_forever,
)
from zetad.dispatch import QueueingDispatcher
from zetad.ingress import run_push_ingress_forever
from zetad.project import (
    ProjectSnapshot,
    ProjectSnapshotUnavailable,
    load_project_snapshot,
    load_recorded_project_snapshot,
    record_project_snapshot,
)
from zetad.retry import RetryPolicy
from zetad.scheduling import request_due_schedules
from zetad.session_turn import session_turn_agent
from zetad.store import RuntimeEventStore

logger = logging.getLogger(__name__)

LOCAL_WORKER_NAME = "local-runtime"
QUEUE_LEASE_MS = 60_000
ATTEMPT_HEARTBEAT_INTERVAL_SECONDS = 15.0


ToolExecutorCacheKey = tuple[str, str, str]


@dataclass
class WorkerServices:
    """Project-local resources consumed by the queue worker."""

    project_root: Path
    state_dir: Path
    events: RuntimeEventStore
    tool_registry: CapabilityRegistry = field(default_factory=CapabilityRegistry)
    registry: EventConnectorRegistry | None = None
    model_selection: ModelSelection | None = None
    worker_name: str = LOCAL_WORKER_NAME
    max_concurrent: int = 1
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    tool_executors: ToolExecutorProviderRegistry = field(
        default_factory=load_tool_executor_provider_registry
    )
    executor_cache: dict[ToolExecutorCacheKey, ToolExecutor] = field(
        default_factory=dict,
        init=False,
        repr=False,
        compare=False,
    )
    executor_locks: dict[ToolExecutorCacheKey, asyncio.Lock] = field(
        default_factory=dict,
        init=False,
        repr=False,
        compare=False,
    )
    executor_loop: asyncio.AbstractEventLoop | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    shutdown_task: asyncio.Task[None] | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    @cached_property
    def project_snapshot(self) -> ProjectSnapshot:
        return load_project_snapshot(
            self.project_root / "agents",
            registry=self.registry,
            tool_registry=self.tool_registry,
            model_selection=self.model_selection,
            tool_executors=self.tool_executors,
        )

    async def aclose(self) -> None:
        loop = asyncio.get_running_loop()
        task = self.shutdown_task
        if task is not None and task.done():
            task.result()
            return
        if task is not None:
            if task.get_loop() is not loop:
                raise RuntimeError(
                    "tool executors must close on their worker event loop"
                )
            await asyncio.shield(task)
            return
        if self.executor_loop is not None and self.executor_loop is not loop:
            raise RuntimeError("tool executors must close on their worker event loop")
        self.executor_loop = loop
        task = loop.create_task(self._shutdown())
        self.shutdown_task = task
        await asyncio.shield(task)

    async def _shutdown(self) -> None:
        errors: list[BaseException] = []
        try:
            for lock in tuple(self.executor_locks.values()):
                async with lock:
                    pass
            executors = tuple(
                {id(item): item for item in self.executor_cache.values()}.values()
            )
            self.executor_cache.clear()
            self.executor_locks.clear()
            results = await asyncio.gather(
                *(executor.aclose() for executor in executors),
                return_exceptions=True,
            )
            errors.extend(
                result for result in results if isinstance(result, BaseException)
            )
        finally:
            try:
                self.events.close()
            except Exception as error:
                errors.append(error)
        if errors:
            raise BaseExceptionGroup("tool executor shutdown failed", errors)

    async def tool_executor_for(self, agent: AgentDefinition) -> ToolExecutor:
        if self.shutdown_task is not None:
            raise RuntimeError("worker services are closed")
        config = executor_config(agent.tool_executor.config)
        key = (
            agent.tool_executor.provider,
            agent.agent_id,
            json.dumps(
                config,
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ),
        )
        loop = asyncio.get_running_loop()
        if self.executor_loop is None:
            self.executor_loop = loop
        elif self.executor_loop is not loop:
            raise RuntimeError("tool executors must stay on one worker event loop")
        executor = self.executor_cache.get(key)
        if executor is not None:
            return executor
        lock = self.executor_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if self.shutdown_task is not None:
                raise RuntimeError("worker services are closed")
            executor = self.executor_cache.get(key)
            if executor is not None:
                return executor
            provider_id = agent.tool_executor.provider
            provider = self.tool_executors.resolve(provider_id)
            if provider is None:
                raise RuntimeError(
                    f"tool executor provider {provider_id!r} is not available"
                )
            executor = await provider.setup(
                agent.agent_id,
                self.tool_registry,
                config,
            )
            self.executor_cache[key] = executor
            if self.shutdown_task is not None:
                raise RuntimeError("worker services are closed")
            return executor


def build_worker_services(
    *,
    project_root: Path,
    state_dir: Path | None = None,
    tool_registry: CapabilityRegistry | None = None,
    registry: EventConnectorRegistry | None = None,
    connector_names: Iterable[str] | None = None,
    tool_executors: ToolExecutorProviderRegistry | None = None,
) -> WorkerServices:
    resolved_project_root = project_root.expanduser().resolve()
    resolved_state_dir = resolve_state_dir(
        state_dir,
        start=resolved_project_root,
    )
    resolved_registry = registry or load_connector_registry(
        resolved_project_root / "agents",
        connector_names=connector_names,
    )
    return WorkerServices(
        project_root=resolved_project_root,
        state_dir=resolved_state_dir,
        events=RuntimeEventStore.open(event_store_path(resolved_state_dir)),
        tool_registry=tool_registry or CapabilityRegistry(),
        registry=resolved_registry,
        model_selection=active_model_selection(
            session_dir=resolved_state_dir / "sessions" / "default"
        ),
        tool_executors=tool_executors or load_tool_executor_provider_registry(),
    )


async def run_once(runtime: WorkerServices) -> str:
    rpc_request = pending_rpc_request(runtime)
    if rpc_request is not None:
        await run_eventlog_rpc_request(runtime, rpc_request)
        return f"rpc {rpc_request.id}"
    record_project_snapshot(runtime.events, runtime.project_snapshot)
    publish_due_schedules(runtime)
    executors = project_executors(runtime)
    return await run_available_queue_item(
        runtime.events,
        executors=executors,
        worker_name=runtime.worker_name,
        heartbeat_interval_seconds=ATTEMPT_HEARTBEAT_INTERVAL_SECONDS,
        lease_ms=QUEUE_LEASE_MS,
        retry_policy=runtime.retry_policy,
    )


async def run_until_idle(runtime: WorkerServices) -> str:
    processed = 0
    while await run_once(runtime) != "queue empty":
        processed += 1
    return f"processed {processed}"


def publish_due_schedules(runtime: WorkerServices) -> list[Event]:
    return request_due_schedules(runtime.events, runtime.project_snapshot.project.specs)


def project_executors(runtime: WorkerServices) -> tuple[ExecutableAgent, ...]:
    current = runtime.project_snapshot
    snapshots: list[ProjectSnapshot] = []
    historical_generations = sorted(
        {
            generation
            for item in runtime.events.list_queue_items()
            if item["status"]
            not in {"completed", "cancelled", "dead_lettered", "unhandled"}
            if isinstance(
                generation := item.get("project_generation"),
                str,
            )
            and generation != current.generation_id
        }
    )
    for generation in historical_generations:
        try:
            snapshots.append(
                load_recorded_project_snapshot(
                    runtime.events,
                    generation,
                    registry=runtime.registry,
                    tool_executors=runtime.tool_executors,
                )
            )
        except ProjectSnapshotUnavailable:
            logger.exception("project snapshot %s is unavailable", generation)
    snapshots.append(current)
    return tuple(
        executor
        for snapshot in snapshots
        for executor in compile_snapshot_executors(runtime, snapshot)
    )


def compile_snapshot_executors(
    runtime: WorkerServices,
    snapshot: ProjectSnapshot,
) -> tuple[ExecutableAgent, ...]:
    project = snapshot.project
    agent_loop = RuntimeAgentLoop(runtime)
    execution_manifests = {
        spec.slug: snapshot.execution_manifest(spec) for spec in project.specs
    }
    return tuple(
        [
            *(
                agent
                for spec in project.specs
                for agent in compile_agent_definitions(
                    spec,
                    config=config_with_model_selection(
                        config_for_spec(spec, None),
                        runtime.model_selection,
                    ),
                    event_registry=project.events,
                    agent_loop=agent_loop.run,
                    project_generation=snapshot.generation_id,
                    execution_manifest=execution_manifests[spec.slug],
                )
            ),
            *project_egress_executors(
                project,
                project_generation=snapshot.generation_id,
                execution_manifests=execution_manifests,
            ),
        ]
    )


class RuntimeAgentLoop:
    """Run an agent's model loop inside the local runtime harness."""

    def __init__(self, runtime: WorkerServices) -> None:
        self.runtime = runtime

    async def run(
        self,
        invocation: AgentInvocation,
        objective: str,
        timeline: list[dict[str, Any]],
        context: str,
        config: AgentConfig,
        session_id: str,
        run_id: str,
    ) -> AgentRunResult:
        trace_store = SqliteObjectStore(
            zeta_sqlite_path(self.runtime.state_dir),
            session_id=session_id,
        )
        runtime_context = RuntimeContext(
            session_id=session_id,
            event_sink=self.runtime.events,
            trace_store=trace_store,
            tool_registry=self.runtime.tool_registry,
            state_dir=self.runtime.state_dir,
            session_dir=self.runtime.state_dir / "sessions" / session_id,
        )
        started = time.perf_counter()
        try:
            tool_executor = await self.runtime.tool_executor_for(invocation.agent)
            return await run_agent(
                AgentRunRequest(
                    objective=objective,
                    workflow="agent",
                    runtime="zeta-agent",
                    tools=tuple(config.allowed_capabilities or ()),
                    context=context,
                    config=replace(
                        config_with_model_selection(
                            config,
                            self.runtime.model_selection,
                        ),
                        effect_scope=invocation.queue_item_id,
                    ),
                ),
                run_id=run_id,
                caused_by=invocation.triggering_event.id,
                publish_event=lambda _event: None,
                runtime_context=runtime_context,
                cancellation_event=None,
                tool_executor=tool_executor,
            )
        finally:
            self.runtime.events.observe_runtime_metric(
                "runtime.agent_execution_ms",
                (time.perf_counter() - started) * 1000,
                agent=invocation.agent.agent_id,
            )
            trace_started = time.perf_counter()
            try:
                trace_store.close()
            finally:
                self.runtime.events.observe_runtime_metric(
                    "sqlite.trace_close_ms",
                    (time.perf_counter() - trace_started) * 1000,
                    agent=invocation.agent.agent_id,
                )


def config_with_model_selection(
    config: AgentConfig,
    selection: ModelSelection | None,
) -> AgentConfig:
    if config.model_name is not None or config.model_url is not None:
        return config
    if selection is None:
        return config
    return replace(
        config,
        model_profile=selection.profile,
        model_name=selection.model,
        model_url=selection.url,
        thinking=selection.thinking,
        model_api=selection.api,
    )


async def run_available_queue_item(
    events: RuntimeEventStore,
    executors: tuple[ExecutableAgent, ...],
    *,
    worker_name: str,
    skipped_queue_items: set[str] | None = None,
    lease_ms: int = QUEUE_LEASE_MS,
    heartbeat_interval_seconds: float = ATTEMPT_HEARTBEAT_INTERVAL_SECONDS,
    retry_policy: RetryPolicy | None = None,
) -> str:
    dispatcher = QueueingDispatcher(
        events.journal,
        events,
        executors=executors,
        worker_name=worker_name,
        lease_ms=lease_ms,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        retry_policy=retry_policy,
    )
    outcome = await dispatcher.run_next(skipped_queue_items=skipped_queue_items)
    if outcome is None:
        return "queue empty"
    queue_item_id, lifecycle_events = outcome
    return run_once_message(queue_item_id, lifecycle_events)


def pending_rpc_request(runtime: WorkerServices) -> Event | None:
    from zetad.rpc.routes import RPC_REQUESTED, rpc_request_has_terminal_response

    for event in runtime.events.list_events(Filter(event_type=RPC_REQUESTED)):
        if not rpc_request_has_terminal_response(runtime.events, event):
            return event
    return None


async def run_eventlog_rpc_request(
    runtime: WorkerServices,
    request: Event,
) -> Event | None:
    from zetad.rpc.routes import (
        RpcClient,
        RunState,
        build_rpc_router,
        run_eventlog_rpc_once,
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

    dispatcher = QueueingDispatcher(
        runtime.events,
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
        retry_policy=runtime.retry_policy,
    )
    client = RpcClient(
        connection=None,
        session=session,
        dispatcher=dispatcher,
        pending_runs=pending_runs,
        pending_tool_calls={},
    )
    router = build_rpc_router(client)
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


async def run_forever(
    runtime: WorkerServices,
    *,
    poll_interval_seconds: float = 1.0,
    push_host: str = "127.0.0.1",
    push_port: int = 8080,
    push_route_prefix: str = "/connectors",
    stop_event: asyncio.Event | None = None,
) -> None:
    running: set[asyncio.Task[str]] = set()
    ingress_task = start_ingress_task(
        runtime,
        poll_interval_seconds=poll_interval_seconds,
        stop_event=stop_event,
    )
    push_ingress_task = start_push_ingress_task(
        runtime,
        host=push_host,
        port=push_port,
        route_prefix=push_route_prefix,
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
        await stop_ingress_task(push_ingress_task)
        await log_worker_results(running)


def start_ingress_task(
    runtime: WorkerServices,
    *,
    poll_interval_seconds: float,
    stop_event: asyncio.Event | None,
) -> asyncio.Task[None] | None:
    if runtime.registry is None or not runtime.registry.has_ingress_connectors():
        return None
    return asyncio.create_task(
        run_ingress_forever(
            runtime,
            poll_interval_seconds=poll_interval_seconds,
            stop_event=stop_event,
        )
    )


def start_push_ingress_task(
    runtime: WorkerServices,
    *,
    host: str,
    port: int,
    route_prefix: str,
    stop_event: asyncio.Event | None,
) -> asyncio.Task[None] | None:
    if runtime.registry is None or not runtime.registry.push_ingress_connectors():
        return None
    return asyncio.create_task(
        run_push_ingress_forever(
            lambda connector_id, request: handle_push_ingress_request(
                runtime,
                connector_id,
                request,
            ),
            host=host,
            port=port,
            route_prefix=route_prefix,
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
            await sleep_with_runtime_metrics(runtime, poll_interval_seconds)
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
            await sleep_with_runtime_metrics(runtime, poll_interval_seconds)
            should_refill = True


async def sleep_with_runtime_metrics(
    runtime: WorkerServices,
    interval_seconds: float,
) -> None:
    expected_at = time.monotonic() + interval_seconds
    await asyncio.sleep(interval_seconds)
    runtime.events.observe_runtime_metric(
        "runtime.event_loop_delay_ms",
        max(0.0, (time.monotonic() - expected_at) * 1000),
    )


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
