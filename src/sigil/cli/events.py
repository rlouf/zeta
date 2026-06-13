"""The `events` group."""

from __future__ import annotations

from datetime import datetime

import click

from ..events import Event, Filter, event_store, time_from_timestamp_micros
from ..session import read_events
from ._base import cli, examples
from ._shared import pretty_print_json

EVENT_LIST_COLUMNS = ("time", "workflow", "event", "session", "detail")
WORKFLOW_GLYPHS = frozenset({",", ",,", ",,,", "?", "ask"})


@cli.command(
    "events",
    epilog=examples(
        "sigil events --limit 50",
        "sigil events --json --raw",
    ),
)
@click.option("--json", "json_output", is_flag=True, help="Emit events as JSON.")
@click.option("--raw", is_flag=True, help="With --json, return raw event payloads.")
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=20,
    show_default=True,
    help="Number of recent events to show.",
)
def cmd_events(json_output: bool, raw: bool, limit: int) -> int:
    """Inspect Sigil's read-only event journal.

    This is the raw view underneath `sigil log`: the most recent audit
    and debug records from the event journal, one row per event.
    """
    if raw and not json_output:
        raise click.UsageError("--raw requires --json")
    return print_events_list(json_output=json_output, raw=raw, limit=limit)


def print_events_list(*, json_output: bool, raw: bool, limit: int) -> int:
    """Print a bounded recent view of the global event journal."""
    if raw:
        events = event_store().list_events(Filter())
        pretty_print_json([normalized_event(event) for event in events[-limit:]])
        return 0
    events = read_events()
    recent = events[-limit:]
    if json_output:
        pretty_print_json([event_summary(event) for event in recent])
        return 0
    if not recent:
        print("no events recorded")
        return 0
    print_events_table([event_summary(event) for event in recent])
    return 0


def normalized_event(event: Event) -> dict[str, object]:
    """Return the durable event envelope for raw JSON output."""
    return {
        "id": event.id,
        "type": event.event_type,
        "source": event.source,
        "payload": event.payload,
        "idempotency_key": event.idempotency_key,
        "caused_by": event.caused_by,
        "session_id": event.session_id,
        "timestamp_micros": event.timestamp_micros,
        "time": time_from_timestamp_micros(event.timestamp_micros),
    }


def print_events_table(summaries: list[dict[str, object]]) -> None:
    """Print event summaries as a width-aligned table."""
    rows = [
        {
            "time": str(summary["time_label"]),
            "workflow": str(summary["workflow"]),
            "event": str(summary["event"]),
            "session": str(summary["short_session"]),
            "detail": str(summary["detail"]),
        }
        for summary in summaries
    ]
    widths = {
        column: max(len(column), *(len(row[column]) for row in rows))
        for column in EVENT_LIST_COLUMNS
        if column != "detail"
    }
    header = "  ".join(
        column.ljust(widths[column]) if column != "detail" else column
        for column in EVENT_LIST_COLUMNS
    )
    print(header)
    for row in rows:
        print(
            "  ".join(
                row[column].ljust(widths[column]) if column != "detail" else row[column]
                for column in EVENT_LIST_COLUMNS
            )
        )


def event_summary(event: Event) -> dict[str, object]:
    """Return a user-facing summary for one raw event journal entry."""
    payload = event.payload
    event_id = event.id
    session = event.session_id or ""
    event_type = event.event_type
    event_time = time_from_timestamp_micros(event.timestamp_micros)
    workflow = event_workflow(payload)
    return {
        "id": event_id or "-",
        "short_id": short_token(event_id),
        "time": event_time,
        "time_label": format_event_time(event_time),
        "type": event_type,
        "workflow": workflow,
        "event": event_label(event_type),
        "session": session or "-",
        "short_session": short_token(session),
        "cwd": str(payload.get("cwd") or "-"),
        "detail": event_detail(payload, event_type),
    }


def short_token(value: str) -> str:
    """Return a short stable token for terminal listings."""
    return value[:8] if value else "-"


def format_event_time(value: object) -> str:
    """Format an event timestamp for a compact local terminal view."""
    if not isinstance(value, int | float):
        return "-"
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def event_workflow(event: dict[str, object]) -> str:
    """Return the workflow glyph or name for an event."""
    glyph = event.get("glyph")
    if isinstance(glyph, str) and glyph in WORKFLOW_GLYPHS:
        return glyph
    workflow = event.get("workflow")
    if isinstance(workflow, str) and workflow:
        return workflow
    return "-"


def event_label(event_type: str) -> str:
    """Return the lifecycle label without workflow information."""
    labels = {
        "ask_requested": "ask request",
        "answer": "answer",
        "tool_start": "tool start",
        "tool_end": "tool end",
        "failure_recorded": "failure recorded",
    }
    return labels.get(event_type, event_type.replace("_", " "))


def event_detail(event: dict[str, object], event_type: str) -> str:
    """Return the most useful human summary available on an event."""
    if event_type == "ask_requested":
        return clean_summary_text(event.get("input")) or "ask request"
    if event_type == "tool_start":
        tool = clean_summary_text(event.get("tool")) or "tool"
        detail = clean_summary_text(event.get("detail"))
        return f"{tool}: {detail}" if detail else tool
    if event_type == "tool_end":
        return clean_summary_text(event.get("tool")) or "tool finished"
    if event_type == "failure_recorded":
        return command_status_summary(event)
    return fallback_event_detail(event, event_type)


def fallback_event_detail(event: dict[str, object], event_type: str) -> str:
    """Return the first available command/output snippet or a readable type."""
    for key in ("command", "output_snippet", "stdout_snippet", "stderr_snippet"):
        detail = clean_summary_text(event.get(key))
        if detail:
            return detail
    return event_type.replace("_", " ")


def command_status_summary(event: dict[str, object]) -> str:
    """Summarize a command-like event with status."""
    command = event.get("command")
    if isinstance(command, list):
        command_text = " ".join(str(part) for part in command)
    else:
        command_text = clean_summary_text(command)
    status = event.get("status")
    if isinstance(status, int):
        return f"{command_text} -> {status}" if command_text else f"status {status}"
    return command_text or "command"


def clean_summary_text(value: object, *, limit: int = 96) -> str:
    """Return a single-line bounded summary string."""
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
