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
from agents.spec import AgentSpec, ScheduleEntry
from zeta.agents.runtime import compile_agent_definitions
from zeta.capabilities.registry import CapabilityRegistry
from zeta.dispatch import (
    EventDispatcher,
    RegisteredAgent,
)
from zeta.kernel.events import DraftEvent, Event
from zeta.runtime.config import zeta_state_dir
from zeta.runtime.scope import SessionScope
from zeta.store.events import Filter, SqliteEventStore, event_store_path


@dataclass(frozen=True)
class RuntimeServices:
    """Project-local runtime resources owned by a worker loop."""

    project_root: Path
    state_dir: Path
    events: SqliteEventStore
    specs: tuple[AgentSpec, ...]
    agents: tuple[RegisteredAgent, ...]

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

    from zeta.store.events import SqliteEventStore, event_store_path
    from zeta.store.substrate import SqliteStore, zeta_sqlite_path

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
        agents=agents_for_specs(specs),
    )


def project_specs(project_root: Path) -> tuple[AgentSpec, ...]:
    agents_dir = project_root / "agents"
    if not agents_dir.exists():
        return ()
    return tuple(load_specs_recursive(agents_dir))


def project_agents(project_root: Path) -> tuple[RegisteredAgent, ...]:
    return agents_for_specs(project_specs(project_root))


def agents_for_specs(specs: tuple[AgentSpec, ...]) -> tuple[RegisteredAgent, ...]:
    return tuple(agent for spec in specs for agent in compile_agent_definitions(spec))


def is_runtime_event(event: Event) -> bool:
    return event.event_type.startswith(("runtime.queue_item.", "runtime.attempt."))


async def run_once(runtime: RuntimeServices) -> str:
    emit_due_schedules(runtime)
    enqueue_pending_events(runtime)
    dispatcher = EventDispatcher(runtime.events, agents=runtime.agents)
    claimed = claim_available_queue_item(runtime)
    if claimed is None:
        return "queue empty"
    lifecycle_events = await dispatcher.run_queue_item(claimed)
    return run_once_message(claimed, lifecycle_events)


def enqueue_pending_events(runtime: RuntimeServices) -> int:
    queued = 0
    for event in runtime.events.list_events(Filter()):
        if is_runtime_event(event) or event.event_type.startswith("zeta."):
            continue
        if runtime.events.event_has_queue_item(event.id):
            continue
        runtime.events.ensure_pending_queue_item(event)
        queued += 1
    return queued


def run_once_message(queue_item_id: str, lifecycle_events: list[Event]) -> str:
    for event in lifecycle_events:
        if event.event_type == "runtime.queue_item.unhandled":
            return f"routed {event.payload['event_id']}"
        if event.event_type == "runtime.queue_item.available" and event.payload.get(
            "target_agent"
        ):
            return f"routed {event.payload['event_id']}"
    return f"ran {queue_item_id}"


def claim_available_queue_item(runtime: RuntimeServices) -> str | None:
    now_ms = runtime_time_ms()
    runtime.events.reconcile_expired_queue_claims(now_ms=now_ms)
    return runtime.events.claim_next_queue_item(
        "local-runtime",
        lease_ms=60_000,
        now_ms=now_ms,
    )


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
                    dict(schedule.payload),
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
    while stop_event is None or not stop_event.is_set():
        outcome = await run_once(runtime)
        if outcome == "queue empty":
            await asyncio.sleep(poll_interval_seconds)
