"""The `events` group."""

from collections.abc import Mapping
from datetime import datetime

import click

from commas.cli._base import cli, examples
from commas.cli._shared import pretty_print_json
from commas.state import causal_chain, event_store_path, events_for_turn, read_events
from zeta.records.events import Event
from zeta.records.stores.event_store import Filter
from zeta.records.stores.sqlite import SqliteEventStore

EVENT_LIST_COLUMNS = ("time", "workflow", "event", "session", "detail")
WORKFLOW_GLYPHS = frozenset({",", ",,", ",,,", "?", "ask"})


@cli.group(
    "events",
    invoke_without_command=True,
    epilog=examples(
        "commas events --limit 50",
        "commas events --session shell-a",
        "commas events list --json",
        "commas events trace evt_123 --raw --json",
        "commas events descendants evt_123",
        "commas events turn turn-123",
        "commas events --json --raw",
    ),
)
@click.option("--json", "json_output", is_flag=True, help="Emit events as JSON.")
@click.option("--raw", is_flag=True, help="With --json, return raw event payloads.")
@click.option("--session", "session_id", help="Only show events for one session.")
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=20,
    show_default=True,
    help="Number of recent events to show.",
)
@click.pass_context
def cmd_events(
    context: click.Context,
    json_output: bool,
    raw: bool,
    session_id: str | None,
    limit: int,
) -> int:
    """Inspect Commas's read-only event journal.

    This is the raw view underneath `commas log`: the most recent audit
    and debug records from the event journal, one row per event.
    """
    if context.invoked_subcommand is not None:
        return 0
    if raw and not json_output:
        raise click.UsageError("--raw requires --json")
    return print_events_list(
        json_output=json_output,
        raw=raw,
        limit=limit,
        session_id=session_id,
    )


@cmd_events.command("list", epilog=examples("commas events list --limit 50 --json"))
@click.option("--json", "json_output", is_flag=True, help="Emit events as JSON.")
@click.option("--raw", is_flag=True, help="With --json, return raw event payloads.")
@click.option("--session", "session_id", help="Only show events for one session.")
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=20,
    show_default=True,
    help="Number of recent events to show.",
)
def cmd_events_list(
    json_output: bool,
    raw: bool,
    session_id: str | None,
    limit: int,
) -> int:
    """List recent events."""
    if raw and not json_output:
        raise click.UsageError("--raw requires --json")
    return print_events_list(
        json_output=json_output,
        raw=raw,
        limit=limit,
        session_id=session_id,
    )


@cmd_events.command("trace", epilog=examples("commas events trace evt_123 --json"))
@click.argument("event_id")
@click.option("--json", "json_output", is_flag=True, help="Emit events as JSON.")
@click.option("--raw", is_flag=True, help="With --json, return raw event payloads.")
def cmd_events_trace(event_id: str, json_output: bool, raw: bool) -> int:
    """Show the causal chain from root to EVENT_ID."""
    if raw and not json_output:
        raise click.UsageError("--raw requires --json")
    return print_event_sequence(
        causal_chain(event_id),
        json_output=json_output,
        raw=raw,
        empty_message=f"event not found: {event_id}",
    )


@cmd_events.command("root", epilog=examples("commas events root evt_123 --json"))
@click.argument("event_id")
@click.option("--json", "json_output", is_flag=True, help="Emit event as JSON.")
@click.option("--raw", is_flag=True, help="With --json, return the raw event payload.")
def cmd_events_root(event_id: str, json_output: bool, raw: bool) -> int:
    """Show the root cause for EVENT_ID."""
    if raw and not json_output:
        raise click.UsageError("--raw requires --json")
    chain = causal_chain(event_id)
    root = chain[0] if chain else None
    return print_event_item(
        root,
        json_output=json_output,
        raw=raw,
        empty_message=f"event not found: {event_id}",
    )


@cmd_events.command(
    "descendants",
    epilog=examples("commas events descendants evt_123 --json"),
)
@click.argument("event_id")
@click.option("--json", "json_output", is_flag=True, help="Emit events as JSON.")
@click.option("--raw", is_flag=True, help="With --json, return raw event payloads.")
def cmd_events_descendants(event_id: str, json_output: bool, raw: bool) -> int:
    """Show events caused by EVENT_ID, recursively."""
    if raw and not json_output:
        raise click.UsageError("--raw requires --json")
    return print_event_sequence(
        event_descendants(event_id),
        json_output=json_output,
        raw=raw,
        empty_message=f"no descendants for event: {event_id}",
    )


@cmd_events.command("turn", epilog=examples("commas events turn turn-123 --json"))
@click.argument("turn_id")
@click.option("--json", "json_output", is_flag=True, help="Emit events as JSON.")
@click.option("--raw", is_flag=True, help="With --json, return raw event payloads.")
def cmd_events_turn(turn_id: str, json_output: bool, raw: bool) -> int:
    """Show events associated with TURN_ID."""
    if raw and not json_output:
        raise click.UsageError("--raw requires --json")
    return print_event_sequence(
        events_for_turn(turn_id),
        json_output=json_output,
        raw=raw,
        empty_message=f"no events for turn: {turn_id}",
    )


def print_events_list(
    *,
    json_output: bool,
    raw: bool,
    limit: int,
    session_id: str | None = None,
) -> int:
    """Print a bounded recent view of the global event journal."""
    if raw:
        events = SqliteEventStore(event_store_path()).list_events(
            Filter(session_id=session_id)
        )
        pretty_print_json([normalized_event(event) for event in events[-limit:]])
        return 0
    events = read_events()
    if session_id is not None:
        events = [event for event in events if event.session_id == session_id]
    recent = events[-limit:]
    if json_output:
        pretty_print_json([event_summary(event) for event in recent])
        return 0
    if not recent:
        print("no events recorded")
        return 0
    print_events_table([event_summary(event) for event in recent])
    return 0


def print_event_sequence(
    events: list[Event],
    *,
    json_output: bool,
    raw: bool,
    empty_message: str,
) -> int:
    if json_output:
        payload = [
            normalized_event(event) if raw else event_summary(event) for event in events
        ]
        pretty_print_json(payload)
        return 0
    if not events:
        print(empty_message)
        return 0
    print_events_table([event_summary(event) for event in events])
    return 0


def print_event_item(
    event: Event | None,
    *,
    json_output: bool,
    raw: bool,
    empty_message: str,
) -> int:
    if json_output:
        payload = None
        if event is not None:
            payload = normalized_event(event) if raw else event_summary(event)
        pretty_print_json(payload)
        return 0
    if event is None:
        print(empty_message)
        return 0
    print_events_table([event_summary(event)])
    return 0


def event_descendants(event_id: str) -> list[Event]:
    events = read_events()
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


def normalized_event(event: Event) -> dict[str, object]:
    """Return the durable event envelope for raw JSON output."""
    return {
        "id": event.id,
        "type": event.event_type,
        "source": event.source,
        "payload": dict(event.payload),
        "idempotency_key": event.idempotency_key,
        "caused_by": event.caused_by,
        "session_id": event.session_id,
        "turn_id": event.turn_id,
        "timestamp_ms": event.timestamp_ms,
        "time": event.timestamp_ms / 1_000,
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
    event_time = event.timestamp_ms / 1_000
    workflow = event_workflow(payload)
    return {
        "id": event_id or "-",
        "short_id": short_token(event_id),
        "caused_by": event.caused_by,
        "short_caused_by": short_token(event.caused_by or ""),
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


def event_workflow(event: Mapping[str, object]) -> str:
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


def event_detail(event: Mapping[str, object], event_type: str) -> str:
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


def fallback_event_detail(event: Mapping[str, object], event_type: str) -> str:
    """Return the first available command/output snippet or a readable type."""
    for key in ("command", "output_snippet", "stdout_snippet", "stderr_snippet"):
        detail = clean_summary_text(event.get(key))
        if detail:
            return detail
    return event_type.replace("_", " ")


def command_status_summary(event: Mapping[str, object]) -> str:
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
