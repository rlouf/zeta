"""Schedule authored agent events."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from zeta.agents.resources import load_agent_project, validate_agent_project
from zeta.agents.spec import AgentSpec, ScheduleEntry, scheduled_event_type
from zeta.events import DraftEvent, Event
from zeta.records.stores import EventWriter, SqliteEventStore, event_store_path


@dataclass(frozen=True)
class SchedulerServices:
    """Project-local resources consumed by the scheduler service."""

    project_root: Path
    state_dir: Path
    events: SqliteEventStore

    def close(self) -> None:
        self.events.close()


def build_scheduler_services(
    *,
    project_root: Path,
    state_dir: Path | None = None,
) -> SchedulerServices:
    resolved_project_root = project_root.expanduser().resolve()
    resolved_state_dir = (
        state_dir.expanduser().resolve()
        if state_dir is not None
        else resolved_project_root / ".zeta"
    )
    return SchedulerServices(
        project_root=resolved_project_root,
        state_dir=resolved_state_dir,
        events=SqliteEventStore(event_store_path(resolved_state_dir)),
    )


def utc_now() -> datetime:
    return datetime.now(UTC)


def request_due_project_schedules(
    runtime: SchedulerServices,
    *,
    now: datetime | None = None,
) -> list[Event]:
    project = load_agent_project(runtime.project_root / "agents")
    validate_agent_project(project)
    return request_due_schedules(runtime.events, project.specs, now=now)


def request_due_schedules(
    event_sink: EventWriter,
    specs: Iterable[AgentSpec],
    *,
    now: datetime | None = None,
) -> list[Event]:
    current = now or utc_now()
    requested: list[Event] = []
    for spec in specs:
        if not spec.enabled:
            continue
        for schedule in spec.schedules:
            scheduled_time = schedule_current_time(schedule, current)
            if not cron_matches(schedule.cron, scheduled_time):
                continue
            draft = schedule_event_draft(spec, schedule, scheduled_time)
            outcome = event_sink.accept(draft)
            if outcome.inserted:
                requested.append(outcome.event)
    return requested


def schedule_event_draft(
    spec: AgentSpec,
    schedule: ScheduleEntry,
    scheduled_time: datetime,
) -> DraftEvent:
    return DraftEvent(
        scheduled_event_type(spec.slug),
        "zeta:scheduler",
        {},
        idempotency_key=schedule_idempotency_key(
            spec.slug,
            schedule,
            scheduled_time,
        ),
    )


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
