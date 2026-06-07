"""The `events` group."""

from __future__ import annotations

from datetime import datetime
from typing import cast

import click

from ._base import cli
from ._shared import pretty_print_json
from ..session import read_event_log

EVENT_LIST_COLUMNS = ("time", "route", "event", "session", "detail")
ROUTE_GLYPHS = frozenset({",", ",,", ",,,", "?", "ask"})


@cli.group("events", invoke_without_command=True)
@click.option("--json", "json_output", is_flag=True)
@click.option("--raw", is_flag=True, help="With --json, return raw event payloads.")
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=20,
    show_default=True,
    help="Number of recent events to show.",
)
@click.pass_context
def cmd_events(ctx: click.Context, json_output: bool, raw: bool, limit: int) -> int:
    """Inspect Sigil's read-only event log."""
    if ctx.invoked_subcommand is not None:
        return 0
    return print_events_list(json_output=json_output, raw=raw, limit=limit)


@cmd_events.command("list")
@click.option("--json", "json_output", is_flag=True)
@click.option("--raw", is_flag=True, help="With --json, return raw event payloads.")
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=20,
    show_default=True,
    help="Number of recent events to show.",
)
def cmd_events_list(json_output: bool, raw: bool, limit: int) -> int:
    """Show recent events from the global event log."""
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
        "event": event_label(event, event_type),
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
    """Return the route glyph for an event, including nested operator events."""
    glyph = event.get("glyph")
    if isinstance(glyph, str) and glyph in ROUTE_GLYPHS:
        return glyph
    operator = event.get("operator")
    if isinstance(operator, dict):
        operator = cast("dict[str, object]", operator)
        nested = operator.get("glyph")
        if isinstance(nested, str) and nested in ROUTE_GLYPHS:
            return nested
    return "-"


def event_label(event: dict[str, object], event_type: str) -> str:
    """Return the lifecycle label without route information."""
    operator = event.get("operator")
    if event_type == "operator_completed" and isinstance(operator, dict):
        operator = cast("dict[str, object]", operator)
        return str(operator.get("name") or "operator")
    labels = {
        "answer_requested": "answer request",
        "answer_done": "answer",
        "answer": "answer",
        "tool_start": "tool start",
        "tool_end": "tool end",
        "operator_command_executed": "executed command",
        "plan_created": "plan created",
        "plan_step_decision": "plan decision",
        "plan_step_executed": "executed command",
        "plan_completed": "plan complete",
        "plan_aborted": "plan aborted",
        "command_selected": "staged command",
        "failure_recorded": "failure recorded",
    }
    return labels.get(event_type, event_type.replace("_", " "))


def event_detail(event: dict[str, object], event_type: str) -> str:
    """Return the most useful human summary available on an event."""
    operator_detail = operator_completed_detail(event, event_type)
    if operator_detail is not None:
        return operator_detail
    if event_type == "answer_requested":
        return clean_summary_text(event.get("input")) or "answer request"
    if event_type == "tool_start":
        tool = clean_summary_text(event.get("tool")) or "tool"
        detail = clean_summary_text(event.get("detail"))
        return f"{tool}: {detail}" if detail else tool
    if event_type == "tool_end":
        return clean_summary_text(event.get("tool")) or "tool finished"
    if event_type == "operator_command_executed":
        return command_status_summary(event)
    if event_type == "failure_recorded":
        return command_status_summary(event)
    if event_type.startswith("plan_"):
        return staged_step_detail(event, event_type)
    if event_type == "answer_done":
        return f"{event.get('bytes') or 0} bytes"
    return fallback_event_detail(event, event_type)


def operator_completed_detail(event: dict[str, object], event_type: str) -> str | None:
    """Return the summary for an operator_completed event, or None."""
    if event_type != "operator_completed":
        return None
    operator = event.get("operator")
    if not isinstance(operator, dict):
        return None
    operator = cast("dict[str, object]", operator)
    prompt = clean_summary_text(operator.get("prompt"))
    output = clean_summary_text(event.get("output_snippet"))
    detail = prompt or output
    return detail or "-"


def staged_step_detail(event: dict[str, object], event_type: str) -> str:
    """Return the summary for a plan staged step event."""
    if event_type == "plan_step_executed":
        return command_status_summary(event)
    return clean_summary_text(event.get("objective")) or clean_summary_text(
        event.get("command")
    )


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
