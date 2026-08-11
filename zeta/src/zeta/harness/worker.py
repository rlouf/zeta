"""Run event-driven Zeta work from a durable queue."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from connectors import (
    EventConnectorRegistry,
)

from zeta import ids
from zeta.authoring.resources import load_connector_registry
from zeta.authoring.spec import executor_config
from zeta.capabilities.executors import (
    ToolExecutor,
    ToolExecutorProviderRegistry,
    load_tool_executor_provider_registry,
    tool_executor_providers_with_local,
)
from zeta.capabilities.registry import CapabilityRegistry
from zeta.events import Event
from zeta.harness.connector_bridge import (
    ConnectorCalls,
    project_egress_executors,
    run_ipc_ingress_forever,
)
from zeta.harness.dispatch import QueueingDispatcher
from zeta.harness.project import (
    ProjectSnapshot,
    ProjectSnapshotUnavailable,
    load_project_snapshot,
    load_recorded_project_snapshot,
    record_project_snapshot,
)
from zeta.harness.retry import RetryPolicy
from zeta.harness.routing import (
    AgentDefinition,
    AgentInvocation,
    ExecutableAgent,
    compile_agent_definitions,
    config_for_spec,
)
from zeta.harness.scheduling import request_due_schedules
from zeta.harness.store import RuntimeEventStore
from zeta.journal.sqlite import (
    event_store_path,
    resolve_state_dir,
    zeta_sqlite_path,
)
from zeta.loop.config import AgentConfig
from zeta.loop.outcomes import AgentRunResult
from zeta.loop.runtime import AgentRunRequest, run_agent
from zeta.loop.runtime_context import RuntimeContext
from zeta.models.profiles import ModelSelection, active_model_selection
from zeta.substrate import SqliteObjectStore

logger = logging.getLogger(__name__)

LOCAL_WORKER_NAME = "local-runtime"
QUEUE_LEASE_MS = 60_000
ATTEMPT_HEARTBEAT_INTERVAL_SECONDS = 15.0


ToolExecutorCacheKey = tuple[str, str, str, str]


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
    connector_calls: ConnectorCalls = field(default_factory=ConnectorCalls)
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

    @property
    def project_snapshot(self) -> ProjectSnapshot:
        return load_project_snapshot(
            self.project_root / "agents",
            registry=self.registry,
            tool_registry=self.tool_registry,
            model_selection=self.model_selection,
            tool_executors=self.tool_executors,
            content_store=self.events.content_store(),
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
            await self.connector_calls.aclose()
        except Exception as error:
            errors.append(error)
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

    async def tool_executor_for(
        self,
        agent: AgentDefinition,
        tool_registry: CapabilityRegistry | None = None,
    ) -> ToolExecutor:
        if self.shutdown_task is not None:
            raise RuntimeError("worker services are closed")
        config = executor_config(agent.tool_executor.config)
        key = (
            agent.tool_executor.provider,
            agent.agent_id,
            agent.project_generation or "",
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
                tool_registry or self.tool_registry,
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
        tool_executors=tool_executor_providers_with_local(tool_executors),
    )


async def run_once(runtime: WorkerServices) -> str:
    record_project_snapshot(runtime.events, runtime.project_snapshot)
    publish_due_schedules(runtime)
    runtime.events.publish_next_due_scheduled_event()
    runtime.events.timeout_next_due_wait()
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
                    tool_registry=runtime.tool_registry,
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
    agent_loop = RuntimeAgentLoop(runtime, snapshot.tool_registry)
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
                connector_calls=runtime.connector_calls,
            ),
        ]
    )


class RuntimeAgentLoop:
    """Run an agent's model loop inside the local runtime harness."""

    def __init__(
        self,
        runtime: WorkerServices,
        tool_registry: CapabilityRegistry,
    ) -> None:
        self.runtime = runtime
        self.tool_registry = tool_registry

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
        content_store = SqliteObjectStore(zeta_sqlite_path(self.runtime.state_dir))
        runtime_context = RuntimeContext(
            session_id=session_id,
            event_sink=self.runtime.events,
            trace_store=trace_store,
            tool_registry=self.tool_registry,
            state_dir=self.runtime.state_dir,
            session_dir=self.runtime.state_dir / "sessions" / session_id,
            content_store=content_store,
        )
        started = time.perf_counter()
        try:
            tool_executor = await self.runtime.tool_executor_for(
                invocation.agent,
                self.tool_registry,
            )
            queue_item_id = invocation.queue_item_id or ids.queue_item_id(
                invocation.triggering_event.id,
                invocation.agent.agent_id,
            )
            return await run_agent(
                AgentRunRequest(
                    objective=objective,
                    runtime="zeta-agent",
                    tools=tuple(config.allowed_capabilities or ()),
                    context=context,
                    config=replace(
                        config_with_model_selection(
                            config,
                            self.runtime.model_selection,
                        ),
                        effect_scope=queue_item_id,
                    ),
                    publishable_events=invocation.agent.publishable_events,
                    source_queue_item_id=queue_item_id,
                    source_agent_id=invocation.agent.agent_id,
                ),
                run_id=run_id,
                caused_by=invocation.triggering_event.id,
                publish_event=lambda _event: None,
                runtime_context=runtime_context,
                cancellation_event=invocation.cancellation_event,
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
                content_store.close()
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
        tool_profile=selection.tool_profile,
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
    stop_event: asyncio.Event | None = None,
) -> None:
    running: set[asyncio.Task[str]] = set()
    ingress_task = start_ipc_ingress_task(
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


def start_ipc_ingress_task(
    runtime: WorkerServices,
    *,
    poll_interval_seconds: float,
    stop_event: asyncio.Event | None,
) -> asyncio.Task[None] | None:
    if runtime.registry is None or not runtime.registry.has_ingress_connectors():
        return None
    return asyncio.create_task(
        run_ipc_ingress_forever(
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
