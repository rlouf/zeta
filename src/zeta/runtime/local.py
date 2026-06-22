"""Local process resource construction for Zeta runtime scopes."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from agents.loader import load_specs_recursive
from agents.spec import DEFAULT_SCHEDULE_EVENT, AgentSpec, ScheduleEntry
from zeta.agents.runtime import compile_agent_definitions
from zeta.capabilities.registry import CapabilityRegistry
from zeta.execute import session_turn_agent
from zeta.orchestration.dispatch import (
    EventDispatcher,
    ExecutableAgent,
)
from zeta.records.events import DraftEvent, Event
from zeta.records.stores import (
    Filter,
    SqliteEventStore,
    SqliteStore,
    event_store_path,
    zeta_sqlite_path,
)
from zeta.run.threads import SessionScope
from zeta.runtime.config import zeta_state_dir

LOCAL_WORKER_NAME = "local-runtime"
QUEUE_LEASE_MS = 60_000
ATTEMPT_HEARTBEAT_INTERVAL_SECONDS = 15.0


@dataclass(frozen=True)
class RuntimeServices:
    """Project-local runtime resources owned by a worker loop."""

    project_root: Path
    state_dir: Path
    events: SqliteEventStore
    specs: tuple[AgentSpec, ...]
    executors: tuple[ExecutableAgent, ...]
    worker_name: str = LOCAL_WORKER_NAME
    max_concurrent: int = 1

    def close(self) -> None:
        self.events.close()


def default_session() -> SessionScope:
    """Return the default process session for pure Zeta runtime calls."""

    state_dir = zeta_state_dir()
    session_id = os.environ.get("ZETA_SESSION_ID") or "default"
    return session_for_id(
        session_id=session_id,
        state_dir=state_dir,
        session_dir=state_dir / "sessions" / session_id,
    )


def session_for_id(
    *,
    session_id: str,
    state_dir: Path,
    session_dir: Path,
    tool_registry: CapabilityRegistry | None = None,
) -> SessionScope:
    """Build the default Zeta runtime dependencies for one session scope."""

    from zeta.records.stores import (
        SqliteEventStore,
        SqliteStore,
        event_store_path,
        zeta_sqlite_path,
    )

    if tool_registry is None:
        from zeta.capabilities.registry import registry as tool_registry

    return SessionScope(
        session_id=session_id,
        event_sink=SqliteEventStore(event_store_path(state_dir)),
        trace_store=SqliteStore(zeta_sqlite_path(state_dir), session_id=session_id),
        tool_registry=tool_registry,
        state_dir=state_dir,
        session_dir=session_dir,
    )


def build_runtime(
    *,
    project_root: Path,
    state_dir: Path | None = None,
) -> RuntimeServices:
    resolved_project_root = project_root.expanduser().resolve()
    resolved_state_dir = (
        state_dir.expanduser().resolve()
        if state_dir is not None
        else resolved_project_root / ".zeta"
    )
    specs = project_specs(resolved_project_root)
    return RuntimeServices(
        project_root=resolved_project_root,
        state_dir=resolved_state_dir,
        events=SqliteEventStore(event_store_path(resolved_state_dir)),
        specs=specs,
        executors=executors_for_specs(specs),
    )


def project_specs(project_root: Path) -> tuple[AgentSpec, ...]:
    agents_dir = project_root / "agents"
    if not agents_dir.exists():
        return ()
    return tuple(load_specs_recursive(agents_dir))


def executors_for_specs(specs: tuple[AgentSpec, ...]) -> tuple[ExecutableAgent, ...]:
    return tuple(agent for spec in specs for agent in compile_agent_definitions(spec))


def is_runtime_event(event: Event) -> bool:
    return event.event_type.startswith(("runtime.queue_item.", "runtime.attempt."))


async def run_once(runtime: RuntimeServices) -> str:
    emit_due_schedules(runtime)
    rpc_request = pending_rpc_request(runtime)
    if rpc_request is not None:
        await run_eventlog_rpc_request(runtime, rpc_request)
        return f"rpc {rpc_request.id}"
    enqueue_pending_events(runtime)
    dispatcher = EventDispatcher(
        runtime.events,
        executors=runtime.executors,
        worker_name=runtime.worker_name,
        heartbeat_interval_seconds=ATTEMPT_HEARTBEAT_INTERVAL_SECONDS,
        lease_ms=QUEUE_LEASE_MS,
    )
    skipped_queue_items: set[str] = set()
    while True:
        claimed = claim_available_queue_item(runtime, skipped_queue_items)
        if claimed is None:
            return "queue empty"
        lock_keys = queue_item_lock_keys(runtime, claimed)
        lock_owner = queue_item_lock_owner(runtime, claimed)
        now_ms = runtime_time_ms()
        if not runtime.events.acquire_locks(
            lock_keys,
            lock_owner,
            lease_ms=QUEUE_LEASE_MS,
            now_ms=now_ms,
        ):
            runtime.events.release_queue_claim(
                claimed,
                runtime.worker_name,
                now_ms=now_ms,
            )
            skipped_queue_items.add(claimed)
            continue
        try:
            lifecycle_events = await dispatcher.run_queue_item(claimed)
            return run_once_message(claimed, lifecycle_events)
        finally:
            runtime.events.release_locks(lock_keys, lock_owner)


def enqueue_pending_events(runtime: RuntimeServices) -> int:
    queued = 0
    for event in runtime.events.list_events(Filter()):
        if is_runtime_event(event) or event.event_type.startswith(("zeta.", "rpc.")):
            continue
        if runtime.events.event_has_queue_item(event.id):
            continue
        runtime.events.ensure_pending_queue_item(event)
        queued += 1
    return queued


def pending_rpc_request(runtime: RuntimeServices) -> Event | None:
    from zeta.rpc.routes import RPC_REQUESTED, rpc_request_has_terminal_response

    for event in runtime.events.list_events(Filter(event_type=RPC_REQUESTED)):
        if not rpc_request_has_terminal_response(runtime.events, event):
            return event
    return None


async def run_eventlog_rpc_request(
    runtime: RuntimeServices,
    request: Event,
) -> Event | None:
    from zeta.capabilities.registry import registry as tool_registry
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
    trace_store = SqliteStore(
        zeta_sqlite_path(runtime.state_dir),
        session_id=session_id,
    )
    session = SessionScope(
        session_id=session_id,
        event_sink=runtime.events,
        trace_store=trace_store,
        tool_registry=tool_registry,
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
            *runtime.executors,
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
    runtime: RuntimeServices,
    skipped_queue_items: set[str] | None = None,
) -> str | None:
    now_ms = runtime_time_ms()
    runtime.events.reconcile_expired_queue_claims(now_ms=now_ms)
    runtime.events.reconcile_expired_locks(now_ms=now_ms)
    return runtime.events.claim_next_queue_item(
        runtime.worker_name,
        lease_ms=QUEUE_LEASE_MS,
        now_ms=now_ms,
        exclude_queue_item_ids=skipped_queue_items or (),
    )


def queue_item_lock_keys(
    runtime: RuntimeServices, queue_item_id: str
) -> tuple[str, ...]:
    row = runtime.events.queue_item(queue_item_id)
    if row is None:
        return ()
    target_agent = str(row["target_agent"])
    if target_agent:
        return agent_lock_keys(runtime, target_agent)
    event = runtime.events.get(str(row["event_id"]))
    if event is None:
        return ()
    matching_executors = [
        agent for agent in runtime.executors if agent.definition.accepts(event)
    ]
    if len(matching_executors) != 1:
        return ()
    return matching_executors[0].definition.lock_keys


def agent_lock_keys(runtime: RuntimeServices, agent_id: str) -> tuple[str, ...]:
    for agent in runtime.executors:
        if agent.definition.agent_id == agent_id:
            return agent.definition.lock_keys
    return ()


def queue_item_lock_owner(runtime: RuntimeServices, queue_item_id: str) -> str:
    return f"{runtime.worker_name}:{queue_item_id}"


def runtime_time_ms() -> int:
    return time.time_ns() // 1_000_000


def utc_now() -> datetime:
    return datetime.now(UTC)


def emit_due_schedules(
    runtime: RuntimeServices,
    *,
    now: datetime | None = None,
) -> list[Event]:
    current = now or utc_now()
    emitted: list[Event] = []
    for spec in runtime.specs:
        if not spec.enabled:
            continue
        for schedule in spec.schedules:
            scheduled_time = schedule_current_time(schedule, current)
            if not cron_matches(schedule.cron, scheduled_time):
                continue
            outcome = runtime.events.accept(
                DraftEvent(
                    schedule.event,
                    "runtime:scheduler",
                    schedule_event_payload(spec, schedule),
                    idempotency_key=schedule_idempotency_key(
                        spec.slug,
                        schedule,
                        scheduled_time,
                    ),
                )
            )
            if outcome.inserted:
                emitted.append(outcome.event)
    return emitted


def schedule_event_payload(
    spec: AgentSpec,
    schedule: ScheduleEntry,
) -> dict[str, object]:
    if schedule.event != DEFAULT_SCHEDULE_EVENT:
        return dict(schedule.payload)
    return {
        "agent_name": spec.name,
        "cron": schedule.cron,
        **schedule.payload,
    }


def schedule_current_time(schedule: ScheduleEntry, now: datetime) -> datetime:
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    if schedule.timezone is None:
        return now.astimezone(UTC)
    return now.astimezone(ZoneInfo(schedule.timezone))


def schedule_idempotency_key(
    agent_slug: str,
    schedule: ScheduleEntry,
    now: datetime,
) -> str:
    minute = now.replace(second=0, microsecond=0).isoformat()
    return f"schedule:{agent_slug}:{schedule.cron}:{minute}"


def cron_matches(cron: str, now: datetime) -> bool:
    fields = cron.split()
    if len(fields) != 5:
        raise ValueError(f"unsupported cron expression {cron!r}")
    minute, hour, day, month, weekday = fields
    return (
        cron_field_matches(minute, now.minute, 0, 59)
        and cron_field_matches(hour, now.hour, 0, 23)
        and cron_field_matches(day, now.day, 1, 31)
        and cron_field_matches(month, now.month, 1, 12)
        and cron_field_matches(weekday, (now.weekday() + 1) % 7, 0, 6)
    )


def cron_field_matches(expression: str, value: int, minimum: int, maximum: int) -> bool:
    return any(
        cron_part_matches(part.strip(), value, minimum, maximum)
        for part in expression.split(",")
    )


def cron_part_matches(part: str, value: int, minimum: int, maximum: int) -> bool:
    if not part:
        return False
    base, step = cron_step(part)
    start, end = cron_range(base, minimum, maximum)
    return start <= value <= end and (value - start) % step == 0


def cron_step(part: str) -> tuple[str, int]:
    if "/" not in part:
        return part, 1
    base, step_text = part.split("/", 1)
    step = int(step_text)
    if step <= 0:
        raise ValueError(f"unsupported cron step {part!r}")
    return base, step


def cron_range(part: str, minimum: int, maximum: int) -> tuple[int, int]:
    if part == "*":
        return minimum, maximum
    if "-" in part:
        start_text, end_text = part.split("-", 1)
        return int(start_text), int(end_text)
    value = int(part)
    return value, value


async def run_forever(
    runtime: RuntimeServices,
    *,
    poll_interval_seconds: float = 1.0,
    stop_event: asyncio.Event | None = None,
) -> None:
    running: set[asyncio.Task[str]] = set()
    while stop_event is None or not stop_event.is_set():
        while len(running) < runtime.max_concurrent:
            task = asyncio.create_task(run_once(runtime))
            running.add(task)
            await asyncio.sleep(0)
            if task.done() and task.result() == "queue empty":
                running.remove(task)
                break
        if not running:
            await asyncio.sleep(poll_interval_seconds)
            continue
        done, running = await asyncio.wait(
            running,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if all(task.result() == "queue empty" for task in done):
            await asyncio.sleep(poll_interval_seconds)
    if running:
        await asyncio.gather(*running)
