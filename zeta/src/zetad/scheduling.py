"""Schedule authored agent events."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from connectors import EventConnectorRegistry
from croniter import croniter
from zeta.agents.resources import (
    load_agent_project,
    load_connector_registry,
    validate_agent_project,
)
from zeta.agents.spec import AgentSpec, ScheduleEntry, scheduled_event_type
from zeta.events import DraftEvent, Event
from zeta.records.stores.event_store import EventReader, EventWriter, Filter
from zeta.records.stores.sqlite import event_store_path

from zetad.store import RuntimeEventStore

SCHEDULER_TICK_PREFIX = "scheduler.tick."


@dataclass(frozen=True)
class SchedulerServices:
    """Project-local resources consumed by the scheduler service."""

    project_root: Path
    state_dir: Path
    events: RuntimeEventStore
    registry: EventConnectorRegistry | None = None

    def close(self) -> None:
        self.events.close()


@dataclass(frozen=True)
class ScheduleStatus:
    agent: str
    cron: str
    timezone: str | None
    status: str
    last_published_at: str | None
    next_at: str
    reason: str

    def as_record(self) -> dict[str, str | None]:
        return {
            "agent": self.agent,
            "cron": self.cron,
            "timezone": self.timezone,
            "status": self.status,
            "last_published_at": self.last_published_at,
            "next_at": self.next_at,
            "reason": self.reason,
        }


def build_scheduler_services(
    *,
    project_root: Path,
    state_dir: Path | None = None,
    registry: EventConnectorRegistry | None = None,
    connector_names: Iterable[str] | None = None,
) -> SchedulerServices:
    resolved_project_root = project_root.expanduser().resolve()
    resolved_state_dir = (
        state_dir.expanduser().resolve()
        if state_dir is not None
        else resolved_project_root / ".zeta"
    )
    resolved_registry = registry or load_connector_registry(
        resolved_project_root / "agents",
        connector_names=connector_names,
    )
    return SchedulerServices(
        project_root=resolved_project_root,
        state_dir=resolved_state_dir,
        events=RuntimeEventStore.open(event_store_path(resolved_state_dir)),
        registry=resolved_registry,
    )


def utc_now() -> datetime:
    return datetime.now(UTC)


def request_due_project_schedules(
    runtime: SchedulerServices,
    *,
    now: datetime | None = None,
) -> list[Event]:
    project = load_agent_project(
        runtime.project_root / "agents",
        registry=runtime.registry,
    )
    validate_agent_project(project)
    return request_due_schedules(runtime.events, project.specs, now=now)


def project_schedule_status(
    runtime: SchedulerServices,
    *,
    now: datetime | None = None,
) -> list[ScheduleStatus]:
    project = load_agent_project(
        runtime.project_root / "agents",
        registry=runtime.registry,
    )
    validate_agent_project(project)
    return schedule_status(runtime.events, project.specs, now=now)


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
        for schedule_index, schedule in enumerate(spec.schedules):
            scheduled_time = due_schedule_time(schedule, current)
            if scheduled_time is None:
                record_missed_schedules(
                    event_sink,
                    spec,
                    schedule_index,
                    schedule,
                    current,
                )
                continue
            draft = schedule_event_draft(spec, schedule, scheduled_time)
            outcome = event_sink.accept(draft)
            status = "published" if outcome.inserted else "skipped"
            reason = (
                schedule_tick_reason(
                    scheduled_time, schedule_current_time(schedule, current)
                )
                if outcome.inserted
                else "already published"
            )
            event_sink.accept(
                schedule_tick_draft(
                    spec,
                    schedule_index,
                    schedule,
                    scheduled_time,
                    schedule_current_time(schedule, current),
                    next_schedule_time(schedule, current),
                    status=status,
                    reason=reason,
                    published_event_id=outcome.event.id,
                )
            )
            if outcome.inserted:
                requested.append(outcome.event)
    return requested


def record_missed_schedules(
    event_sink: EventWriter,
    spec: AgentSpec,
    schedule_index: int,
    schedule: ScheduleEntry,
    current: datetime,
) -> None:
    if not isinstance(event_sink, EventReader):
        return
    missed_time = previous_schedule_time(schedule, current)
    if same_schedule_date(schedule, missed_time, current):
        return
    if not schedule_has_prior_activity(event_sink, spec, schedule_index, schedule):
        return
    if schedule_tick_recorded(event_sink, spec, schedule_index, schedule, missed_time):
        return
    next_time = next_schedule_time(schedule, current)
    event_sink.accept(
        schedule_tick_draft(
            spec,
            schedule_index,
            schedule,
            missed_time,
            current,
            next_time,
            status="missed",
            reason="previous-day tick not backfilled",
        )
    )


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


def schedule_tick_draft(
    spec: AgentSpec,
    schedule_index: int,
    schedule: ScheduleEntry,
    scheduled_time: datetime,
    observed_time: datetime,
    next_time: datetime,
    *,
    status: str,
    reason: str,
    published_event_id: str | None = None,
) -> DraftEvent:
    payload = {
        "agent": spec.slug,
        "schedule_index": schedule_index,
        "event_type": scheduled_event_type(spec.slug),
        "cron": schedule.cron,
        "timezone": schedule.timezone,
        "scheduled_at": scheduled_time.isoformat(),
        "observed_at": observed_time.isoformat(),
        "next_at": next_time.isoformat(),
        "status": status,
        "reason": reason,
        "published_event_id": published_event_id,
    }
    return DraftEvent(
        f"{SCHEDULER_TICK_PREFIX}{status}",
        "zeta:scheduler",
        payload,
        idempotency_key=schedule_tick_idempotency_key(
            spec.slug,
            schedule_index,
            schedule,
            status,
            scheduled_time,
        ),
        caused_by=published_event_id,
    )


def schedule_current_time(schedule: ScheduleEntry, now: datetime) -> datetime:
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    if schedule.timezone is None:
        return now.astimezone(UTC)
    return now.astimezone(ZoneInfo(schedule.timezone))


def due_schedule_time(schedule: ScheduleEntry, now: datetime) -> datetime | None:
    candidate = previous_schedule_time(schedule, now)
    if same_schedule_date(schedule, candidate, now):
        return candidate
    return None


def previous_schedule_time(schedule: ScheduleEntry, now: datetime) -> datetime:
    if len(schedule.cron.split()) != 5:
        raise ValueError(f"unsupported cron expression {schedule.cron!r}")
    current = schedule_current_time(schedule, now)
    base = current.replace(second=59, microsecond=999_999)
    candidate = croniter(schedule.cron, base).get_prev(datetime)
    if candidate.tzinfo is None:
        candidate = candidate.replace(tzinfo=base.tzinfo)
    return candidate


def next_schedule_time(schedule: ScheduleEntry, now: datetime) -> datetime:
    if len(schedule.cron.split()) != 5:
        raise ValueError(f"unsupported cron expression {schedule.cron!r}")
    current = schedule_current_time(schedule, now)
    candidate = croniter(schedule.cron, current).get_next(datetime)
    if candidate.tzinfo is None:
        candidate = candidate.replace(tzinfo=current.tzinfo)
    return candidate


def same_schedule_date(
    schedule: ScheduleEntry,
    scheduled_time: datetime,
    now: datetime,
) -> bool:
    current = schedule_current_time(schedule, now)
    local_scheduled_time = scheduled_time.astimezone(current.tzinfo)
    return local_scheduled_time.date() == current.date()


def schedule_tick_reason(scheduled_time: datetime, observed_time: datetime) -> str:
    scheduled_minute = scheduled_time.replace(second=0, microsecond=0)
    observed_minute = observed_time.replace(second=0, microsecond=0)
    if scheduled_minute == observed_minute:
        return "due now"
    return "same-day backfill"


def schedule_tick_idempotency_key(
    agent_slug: str,
    schedule_index: int,
    schedule: ScheduleEntry,
    status: str,
    scheduled_time: datetime,
) -> str:
    minute = scheduled_time.replace(second=0, microsecond=0).isoformat()
    timezone = schedule.timezone or ""
    return (
        f"scheduler:{status}:{agent_slug}:{schedule_index}:"
        f"{schedule.cron}:{timezone}:{minute}"
    )


def schedule_idempotency_key(
    agent_slug: str,
    schedule: ScheduleEntry,
    now: datetime,
) -> str:
    minute = now.replace(second=0, microsecond=0).isoformat()
    return f"schedule:{agent_slug}:{schedule.cron}:{minute}"


def schedule_status(
    event_reader: EventReader,
    specs: Iterable[AgentSpec],
    *,
    now: datetime | None = None,
) -> list[ScheduleStatus]:
    current = now or utc_now()
    decisions = event_reader.list_events(
        Filter(event_type_prefix=SCHEDULER_TICK_PREFIX)
    )
    rows: list[ScheduleStatus] = []
    for spec in specs:
        if not spec.enabled:
            continue
        for schedule_index, schedule in enumerate(spec.schedules):
            rows.append(
                schedule_status_row(
                    decisions,
                    spec,
                    schedule_index,
                    schedule,
                    current,
                )
            )
    return rows


def schedule_status_row(
    decisions: Iterable[Event],
    spec: AgentSpec,
    schedule_index: int,
    schedule: ScheduleEntry,
    now: datetime,
) -> ScheduleStatus:
    matching = [
        event
        for event in decisions
        if schedule_decision_matches(event, spec, schedule_index, schedule)
    ]
    latest_due_time = previous_schedule_time(schedule, now)
    latest_due_iso = latest_due_time.isoformat()
    latest_decision = next(
        (
            event
            for event in reversed(matching)
            if event.payload.get("scheduled_at") == latest_due_iso
        ),
        None,
    )
    latest_published = next(
        (
            event
            for event in reversed(matching)
            if event.payload.get("status") == "published"
        ),
        None,
    )
    if latest_decision is None:
        status = "pending"
        reason = "next tick is in the future"
    else:
        status = str(latest_decision.payload["status"])
        reason = str(latest_decision.payload["reason"])
    last_published_at = (
        str(latest_published.payload["scheduled_at"])
        if latest_published is not None
        else None
    )
    return ScheduleStatus(
        agent=spec.slug,
        cron=schedule.cron,
        timezone=schedule.timezone,
        status=status,
        last_published_at=last_published_at,
        next_at=next_schedule_time(schedule, now).isoformat(),
        reason=reason,
    )


def schedule_decision_matches(
    event: Event,
    spec: AgentSpec,
    schedule_index: int,
    schedule: ScheduleEntry,
) -> bool:
    return (
        event.payload.get("agent") == spec.slug
        and event.payload.get("schedule_index") == schedule_index
        and event.payload.get("cron") == schedule.cron
        and event.payload.get("timezone") == schedule.timezone
    )


def schedule_has_prior_activity(
    event_reader: EventReader,
    spec: AgentSpec,
    schedule_index: int,
    schedule: ScheduleEntry,
) -> bool:
    return any(
        schedule_decision_matches(event, spec, schedule_index, schedule)
        for event in event_reader.list_events(
            Filter(event_type_prefix=SCHEDULER_TICK_PREFIX)
        )
    )


def schedule_tick_recorded(
    event_reader: EventReader,
    spec: AgentSpec,
    schedule_index: int,
    schedule: ScheduleEntry,
    scheduled_time: datetime,
) -> bool:
    scheduled_at = scheduled_time.isoformat()
    return any(
        schedule_decision_matches(event, spec, schedule_index, schedule)
        and event.payload.get("scheduled_at") == scheduled_at
        for event in event_reader.list_events(
            Filter(event_type_prefix=SCHEDULER_TICK_PREFIX)
        )
    )
