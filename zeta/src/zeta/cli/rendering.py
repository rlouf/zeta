"""Record shaping and printing for the command line."""

import json
from typing import Any

import click
from zeta.events import Event
from zeta.harness.store import RuntimeEventStore
from zeta.journal.store import Filter


def event_record(event: Event) -> dict[str, object]:
    return {
        "id": event.id,
        "type": event.event_type,
        "source": event.source,
        "payload": dict(event.payload),
        "idempotency_key": event.idempotency_key,
        "caused_by": event.caused_by,
        "session_id": event.session_id,
        "run_id": event.run_id,
        "turn_id": event.turn_id,
        "timestamp_ms": event.timestamp_ms,
        "cursor": event.cursor,
    }


def event_summary_record(event: Event) -> dict[str, object]:
    return {
        "id": event.id,
        "type": event.event_type,
        "source": event.source,
        "session_id": event.session_id,
        "run_id": event.run_id,
        "turn_id": event.turn_id,
        "caused_by": event.caused_by,
        "timestamp_ms": event.timestamp_ms,
    }


def print_event_sequence(
    events: list[Event],
    *,
    json_output: bool,
    raw: bool,
    empty_message: str,
) -> int:
    if json_output:
        payload = [
            event_record(event) if raw else event_summary_record(event)
            for event in events
        ]
        click.echo(json.dumps(payload, ensure_ascii=False))
        return 0
    if not events:
        click.echo(empty_message)
        return 0
    for event in events:
        click.echo(
            "\t".join(
                [
                    str(event.cursor or ""),
                    event.event_type,
                    event.source,
                    event.id,
                ]
            )
        )
    return 0


def print_event_item(
    event: Event | None,
    *,
    json_output: bool,
    raw: bool,
    empty_message: str,
) -> int:
    if json_output:
        payload = (
            None
            if event is None
            else (event_record(event) if raw else event_summary_record(event))
        )
        click.echo(json.dumps(payload, ensure_ascii=False))
        return 0
    if event is None:
        click.echo(empty_message)
        return 0
    return print_event_sequence([event], json_output=False, raw=False, empty_message="")


def descendant_events(event_store: RuntimeEventStore, event_id: str) -> list[Event]:
    events = event_store.list_events(Filter())
    children: dict[str, list[Event]] = {}
    for event in events:
        if event.caused_by is not None:
            children.setdefault(event.caused_by, []).append(event)
    descendants: list[Event] = []
    seen: set[str] = {event_id}
    stack = list(reversed(children.get(event_id, [])))
    while stack:
        event = stack.pop()
        if event.id in seen:
            continue
        seen.add(event.id)
        descendants.append(event)
        stack.extend(reversed(children.get(event.id, [])))
    return descendants


def run_summary_records(event_store: RuntimeEventStore) -> list[dict[str, object]]:
    events_by_id = {event.id: event for event in event_store.list_events(Filter())}
    summaries: list[dict[str, object]] = []
    for attempt in event_store.list_attempts():
        trigger = events_by_id.get(str(attempt["event_id"]))
        summaries.append(run_summary_record(attempt, trigger))
    return summaries


def run_summary_record(
    attempt: dict[str, Any],
    trigger: Event | None,
) -> dict[str, object]:
    return {
        "run_id": attempt.get("run_id"),
        "attempt_id": attempt["attempt_id"],
        "queue_item_id": attempt["queue_item_id"],
        "event_id": attempt["event_id"],
        "trigger_event_type": trigger.event_type if trigger is not None else None,
        "target_agent": attempt["target_agent"],
        "status": attempt["status"],
        "session_id": attempt.get("session_id"),
        "started_at": attempt["started_at"],
        "finished_at": attempt.get("finished_at"),
        "summary": attempt.get("summary"),
        "error": attempt.get("error"),
        "input_tokens": attempt.get("input_tokens"),
        "output_tokens": attempt.get("output_tokens"),
    }


def run_detail_record(
    event_store: RuntimeEventStore,
    run_id: str,
) -> dict[str, object] | None:
    attempts_by_run = {
        str(attempt["run_id"]): attempt
        for attempt in event_store.list_attempts()
        if attempt.get("run_id") is not None
    }
    attempt = attempts_by_run.get(run_id)
    if attempt is None:
        return None
    queue_items = {
        str(row["queue_item_id"]): row for row in event_store.list_queue_items()
    }
    trigger = event_store.get(str(attempt["event_id"]))
    return {
        "run": run_summary_record(attempt, trigger),
        "trigger_event": event_record(trigger) if trigger is not None else None,
        "queue_item": queue_items.get(str(attempt["queue_item_id"])),
        "attempt": attempt,
        "result": attempt.get("result"),
        "events": attempt.get("events"),
        "tool_calls": attempt.get("tool_calls"),
        "usage": attempt.get("usage"),
    }


def run_display_id(record: dict[str, object]) -> str:
    run_id = record.get("run_id")
    if isinstance(run_id, str) and run_id:
        return run_id
    return str(record["attempt_id"])


def run_summary_text(record: dict[str, object]) -> str:
    summary = record.get("summary")
    if isinstance(summary, str) and summary:
        return summary
    error = record.get("error")
    if isinstance(error, str) and error:
        return error
    return "-"
