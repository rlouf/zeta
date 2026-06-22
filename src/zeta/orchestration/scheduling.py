"""Schedule authored agent events."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from agents.spec import DEFAULT_SCHEDULE_EVENT, AgentSpec, ScheduleEntry
from zeta.records.events import DraftEvent, Event
from zeta.records.stores import EventWriter


def utc_now() -> datetime:
    return datetime.now(UTC)


def emit_due_schedules(
    event_sink: EventWriter,
    specs: Iterable[AgentSpec],
    *,
    now: datetime | None = None,
) -> list[Event]:
    current = now or utc_now()
    emitted: list[Event] = []
    for spec in specs:
        if not spec.enabled:
            continue
        for schedule in spec.schedules:
            scheduled_time = schedule_current_time(schedule, current)
            if not cron_matches(schedule.cron, scheduled_time):
                continue
            outcome = event_sink.accept(
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
