"""The `events` group."""

from __future__ import annotations

from datetime import datetime

import click

from ..session import read_event_log
from ._base import cli
from ._shared import pretty_print_json

EVENT_LIST_COLUMNS = ("time", "route", "event", "session", "detail")
ROUTE_GLYPHS = frozenset({",", ",,", ",,,", "?", "ask"})


@cli.command("events")
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
    """Inspect Sigil's read-only event log."""
    if raw and not json_output:
        raise click.UsageError("--raw requires --json")
    return print_events_list(json_output=json_output, raw=raw, limit=limit)


def print_events_list(*, json_output: bool, raw: bool, limit: int) -> int:
    """Print a bounded recent view of the global event log."""
    events = read_event_log()
    recent = events[-limit:]
    if json_output:
        pretty_print_json(recent if raw else [event_summary(event) for event in recent])
        return 0
    if not recent:
        print("no events recorded")
        return 0
    print_events_table([event_summary(event) for event in recent])
    return 0


def print_events_table(summaries: list[dict[str, object]]) -> None:
    """Print event summaries as a width-aligned table."""
    rows = [
        {
            "time": str(summary["time_label"]),
            "route": str(summary["route"]),
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


def event_summary(event: dict[str, object]) -> dict[str, object]:
    """Return a user-facing summary for one raw event log entry."""
    event_id = str(event.get("id") or "")
    session = str(event.get("session") or "")
    event_type = str(event.get("type") or "event")
    route = event_route(event)
    return {
        "id": event_id or "-",
        "short_id": short_token(event_id),
        "time": event.get("time"),
        "time_label": format_event_time(event.get("time")),
        "type": event_type,
        "route": route,
        "event": event_label(event_type),
        "session": session or "-",
        "short_session": short_token(session),
        "cwd": str(event.get("cwd") or "-"),
        "detail": event_detail(event, event_type),
    }


def short_token(value: str) -> str:
    """Return a short stable token for terminal listings."""
    return value[:8] if value else "-"


def format_event_time(value: object) -> str:
    """Format an event timestamp for a compact local terminal view."""
    if not isinstance(value, int | float):
        return "-"
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def event_route(event: dict[str, object]) -> str:
    """Return the route glyph for an event."""
    glyph = event.get("glyph")
    if isinstance(glyph, str) and glyph in ROUTE_GLYPHS:
        return glyph
    return "-"


def event_label(event_type: str) -> str:
    """Return the lifecycle label without route information."""
    labels = {
        "answer_requested": "answer request",
        "answer": "answer",
        "tool_start": "tool start",
        "tool_end": "tool end",
        "failure_recorded": "failure recorded",
    }
    return labels.get(event_type, event_type.replace("_", " "))


def event_detail(event: dict[str, object], event_type: str) -> str:
    """Return the most useful human summary available on an event."""
    if event_type == "answer_requested":
        return clean_summary_text(event.get("input")) or "answer request"
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
