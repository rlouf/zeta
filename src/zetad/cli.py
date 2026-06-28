"""Command-line entrypoint for the Zeta runtime."""

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, cast

import click

from sigil.tools import register_builtin_tools
from zeta.capabilities.registry import CapabilityRegistry
from zeta.events import DraftEvent, Event
from zeta.records.stores.event_store import Filter
from zeta.records.stores.sqlite import event_store_path
from zetad import scheduling, worker
from zetad.rpc.stdio import run_stdio
from zetad.store import RuntimeEventStore

QUEUE_STATUS_ORDER = (
    "pending",
    "available",
    "claimed",
    "completed",
    "failed",
    "cancelled",
    "retry_scheduled",
    "unhandled",
)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli() -> None:
    """Zeta runtime commands."""


def runtime_state_dir(project_root: Path, state_dir: Path | None) -> Path:
    if state_dir is not None:
        return state_dir.expanduser()
    return project_root.expanduser().resolve() / ".zeta"


def runtime_event_store(
    project_root: Path,
    state_dir: Path | None,
) -> RuntimeEventStore:
    return RuntimeEventStore.open(
        event_store_path(runtime_state_dir(project_root, state_dir))
    )


def cli_tool_registry() -> CapabilityRegistry:
    registry = CapabilityRegistry()
    register_builtin_tools(registry)
    return registry


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


@cli.command("queue")
@click.option(
    "--project-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
    help="Project root containing .zeta runtime state.",
)
@click.option(
    "--state-dir",
    type=click.Path(file_okay=False, path_type=Path),
    help="Override the runtime state directory.",
)
@click.option("--json", "json_output", is_flag=True, help="Emit JSON.")
def queue(project_root: Path, state_dir: Path | None, json_output: bool) -> int:
    """List durable runtime queue items."""

    event_store = runtime_event_store(project_root, state_dir)
    try:
        rows = event_store.list_queue_items()
    finally:
        event_store.close()
    if json_output:
        click.echo(json.dumps(rows, ensure_ascii=False))
        return 0
    if not rows:
        click.echo("queue empty")
        return 0
    for row in rows:
        click.echo(
            "\t".join(
                [
                    str(row["status"]),
                    str(row["queue_item_id"]),
                    str(row["target_agent"]),
                    str(row["event_id"]),
                ]
            )
        )
    return 0


@cli.command("attempts")
@click.option(
    "--project-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
    help="Project root containing .zeta runtime state.",
)
@click.option(
    "--state-dir",
    type=click.Path(file_okay=False, path_type=Path),
    help="Override the runtime state directory.",
)
@click.option("--json", "json_output", is_flag=True, help="Emit JSON.")
def attempts(project_root: Path, state_dir: Path | None, json_output: bool) -> int:
    """List durable runtime attempts."""

    event_store = runtime_event_store(project_root, state_dir)
    try:
        rows = event_store.list_attempts()
    finally:
        event_store.close()
    if json_output:
        click.echo(json.dumps(rows, ensure_ascii=False))
        return 0
    if not rows:
        click.echo("attempts empty")
        return 0
    for row in rows:
        click.echo(
            "\t".join(
                [
                    str(row["status"]),
                    str(row["attempt_id"]),
                    str(row["queue_item_id"]),
                    str(row["target_agent"]),
                ]
            )
        )
    return 0


@cli.group("events", invoke_without_command=True)
@click.option(
    "--project-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
    help="Project root containing .zeta runtime state.",
)
@click.option(
    "--state-dir",
    type=click.Path(file_okay=False, path_type=Path),
    help="Override the runtime state directory.",
)
@click.option("--type-prefix", help="Only show events with this type prefix.")
@click.option("--session", "session_id", help="Only show events for one session.")
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=50,
    show_default=True,
    help="Maximum number of events to show.",
)
@click.option("--json", "json_output", is_flag=True, help="Emit JSON.")
@click.pass_context
def events(
    ctx: click.Context,
    project_root: Path,
    state_dir: Path | None,
    type_prefix: str | None,
    session_id: str | None,
    limit: int,
    json_output: bool,
) -> int:
    """List durable runtime events."""
    if ctx.invoked_subcommand is not None:
        return 0

    event_store = runtime_event_store(project_root, state_dir)
    try:
        durable_events = event_store.list_events(
            Filter(
                event_type_prefix=type_prefix,
                session_id=session_id,
                limit=limit,
            )
        )
    finally:
        event_store.close()
    if json_output:
        click.echo(json.dumps([event_record(event) for event in durable_events]))
        return 0
    if not durable_events:
        click.echo("events empty")
        return 0
    for event in durable_events:
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


@events.command("publish")
@click.argument("event_type")
@click.option(
    "--project-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
    help="Project root containing .zeta runtime state.",
)
@click.option(
    "--state-dir",
    type=click.Path(file_okay=False, path_type=Path),
    help="Override the runtime state directory.",
)
@click.option("--source", default="manual", show_default=True, help="Event source.")
@click.option(
    "--payload-json",
    default="{}",
    show_default=True,
    help="JSON object payload.",
)
@click.option("--idempotency-key", help="Optional idempotency key.")
@click.option("--caused-by", help="Optional parent event id.")
@click.option("--session", "session_id", help="Optional runtime session id.")
@click.option("--run-id", help="Optional runtime run id.")
@click.option("--turn-id", help="Optional runtime turn id.")
@click.option("--json", "json_output", is_flag=True, help="Emit JSON.")
def events_publish(
    event_type: str,
    project_root: Path,
    state_dir: Path | None,
    source: str,
    payload_json: str,
    idempotency_key: str | None,
    caused_by: str | None,
    session_id: str | None,
    run_id: str | None,
    turn_id: str | None,
    json_output: bool,
) -> int:
    """Publish one durable event into the local runtime log."""
    if not event_type:
        raise click.ClickException("event_type must be non-empty")
    payload = event_payload_from_json(payload_json)
    event_store = runtime_event_store(project_root, state_dir)
    try:
        outcome = event_store.accept(
            DraftEvent(
                event_type,
                source,
                payload,
                idempotency_key=idempotency_key,
                caused_by=caused_by,
                session_id=session_id,
                run_id=run_id,
                turn_id=turn_id,
            )
        )
    finally:
        event_store.close()

    if json_output:
        click.echo(
            json.dumps(
                {"inserted": outcome.inserted, "event": event_record(outcome.event)},
                ensure_ascii=False,
            )
        )
        return 0
    status = "published" if outcome.inserted else "already published"
    click.echo(f"{status} {outcome.event.event_type} {outcome.event.id}")
    return 0


def event_payload_from_json(payload_json: str) -> dict[str, object]:
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"invalid payload JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise click.ClickException("payload JSON must be an object")
    return dict(payload)


@cli.command("runs")
@click.option(
    "--project-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
    help="Project root containing .zeta runtime state.",
)
@click.option(
    "--state-dir",
    type=click.Path(file_okay=False, path_type=Path),
    help="Override the runtime state directory.",
)
@click.option("--json", "json_output", is_flag=True, help="Emit JSON.")
def runs(project_root: Path, state_dir: Path | None, json_output: bool) -> int:
    """List durable runtime runs."""

    event_store = runtime_event_store(project_root, state_dir)
    try:
        rows = run_summary_records(event_store)
    finally:
        event_store.close()
    if json_output:
        click.echo(json.dumps(rows, ensure_ascii=False))
        return 0
    if not rows:
        click.echo("runs empty")
        return 0
    for row in rows:
        click.echo(
            "\t".join(
                [
                    str(row["status"]),
                    run_display_id(row),
                    str(row["target_agent"]),
                    str(row["trigger_event_type"] or "-"),
                    str(row["session_id"] or "-"),
                    run_summary_text(row),
                ]
            )
        )
    return 0


@cli.group("run", invoke_without_command=True)
@click.option(
    "--project-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
    help="Project root containing .zeta runtime state and agents/ specs.",
)
@click.option(
    "--state-dir",
    type=click.Path(file_okay=False, path_type=Path),
    help="Override the runtime state directory.",
)
@click.option("--once", is_flag=True, help="Process at most one unit of work.")
@click.pass_context
def run(
    ctx: click.Context,
    project_root: Path,
    state_dir: Path | None,
    once: bool,
) -> int:
    """Run the local runtime worker."""
    if ctx.invoked_subcommand is not None:
        return 0

    runtime = worker.build_worker_services(
        project_root=project_root,
        state_dir=state_dir,
        tool_registry=cli_tool_registry(),
    )
    try:
        if once:
            message = asyncio.run(worker.run_once(runtime))
            click.echo(message)
        else:
            asyncio.run(worker.run_forever(runtime))
    finally:
        runtime.close()
    return 0


@run.command("show")
@click.argument("run_id")
@click.option(
    "--project-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
    help="Project root containing .zeta runtime state.",
)
@click.option(
    "--state-dir",
    type=click.Path(file_okay=False, path_type=Path),
    help="Override the runtime state directory.",
)
@click.option("--json", "json_output", is_flag=True, help="Emit JSON.")
def run_show(
    run_id: str,
    project_root: Path,
    state_dir: Path | None,
    json_output: bool,
) -> int:
    """Show one durable runtime run."""

    event_store = runtime_event_store(project_root, state_dir)
    try:
        record = run_detail_record(event_store, run_id)
    finally:
        event_store.close()
    if record is None:
        raise click.ClickException(f"run not found: {run_id}")
    if json_output:
        click.echo(json.dumps(record, ensure_ascii=False))
        return 0
    raw_run_record = record["run"]
    if not isinstance(raw_run_record, dict):
        raise click.ClickException(f"run record was invalid: {run_id}")
    run_record = cast("dict[str, object]", raw_run_record)
    click.echo(f"run: {run_display_id(run_record)}")
    click.echo(f"status: {run_record['status']}")
    click.echo(f"agent: {run_record['target_agent']}")
    click.echo(f"trigger: {run_record['trigger_event_type']} {run_record['event_id']}")
    click.echo(f"session: {run_record['session_id'] or '-'}")
    click.echo(f"started: {run_record['started_at']}")
    click.echo(f"finished: {run_record['finished_at'] or '-'}")
    summary = run_summary_text(run_record)
    if summary != "-":
        click.echo()
        click.echo(summary)
    return 0


@cli.group("schedule", invoke_without_command=True)
@click.option(
    "--project-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
    help="Project root containing .zeta runtime state and agents/ specs.",
)
@click.option(
    "--state-dir",
    type=click.Path(file_okay=False, path_type=Path),
    help="Override the runtime state directory.",
)
@click.option("--once", is_flag=True, help="Request due schedules, then exit.")
@click.pass_context
def schedule(
    ctx: click.Context,
    project_root: Path,
    state_dir: Path | None,
    once: bool,
) -> int:
    """Run the local scheduler service."""
    if ctx.invoked_subcommand is not None:
        return 0

    runtime = scheduling.build_scheduler_services(
        project_root=project_root,
        state_dir=state_dir,
    )
    try:
        while True:
            requested = scheduling.request_due_project_schedules(runtime)
            for request in requested:
                click.echo(f"requested {request.event_type} {request.id}")
            if once:
                return 0
            time.sleep(seconds_until_next_minute())
    finally:
        runtime.close()


@schedule.command("status")
@click.option(
    "--project-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
    help="Project root containing .zeta runtime state and agents/ specs.",
)
@click.option(
    "--state-dir",
    type=click.Path(file_okay=False, path_type=Path),
    help="Override the runtime state directory.",
)
@click.option("--json", "json_output", is_flag=True, help="Emit JSON.")
def schedule_status(
    project_root: Path,
    state_dir: Path | None,
    json_output: bool,
) -> int:
    """Show authored-agent schedule status."""

    runtime = scheduling.build_scheduler_services(
        project_root=project_root,
        state_dir=state_dir,
    )
    try:
        rows = scheduling.project_schedule_status(runtime)
    finally:
        runtime.close()
    if json_output:
        click.echo(json.dumps([row.as_record() for row in rows], ensure_ascii=False))
        return 0
    if not rows:
        click.echo("schedules empty")
        return 0
    for row in rows:
        click.echo(
            "\t".join(
                [
                    row.agent,
                    row.cron,
                    row.status,
                    row.last_published_at or "-",
                    row.next_at,
                    row.reason,
                ]
            )
        )
    return 0


def seconds_until_next_minute() -> float:
    return 60 - (time.time() % 60)


@cli.command("status")
@click.option(
    "--project-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
    help="Project root containing .zeta runtime state.",
)
@click.option(
    "--state-dir",
    type=click.Path(file_okay=False, path_type=Path),
    help="Override the runtime state directory.",
)
def status(project_root: Path, state_dir: Path | None) -> int:
    """Show durable runtime queue counts."""

    event_store = runtime_event_store(project_root, state_dir)
    try:
        rows = event_store.list_queue_items()
    finally:
        event_store.close()
    counts: dict[str, int] = {}
    for row in rows:
        status_name = str(row["status"])
        counts[status_name] = counts.get(status_name, 0) + 1
    if not counts:
        click.echo("queue empty")
        return 0
    for status_name in QUEUE_STATUS_ORDER:
        count = counts.get(status_name)
        if count is not None:
            click.echo(f"{status_name}: {count}")
    return 0


@cli.command("rpc")
@click.option("--stdio", is_flag=True, help="Serve newline-delimited JSON-RPC.")
def rpc(stdio: bool) -> int:
    """Serve the Zeta JSON-RPC protocol."""
    if not stdio:
        raise click.UsageError("only --stdio is supported")
    run_stdio(sys.stdin, sys.stdout)
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        result = cli.main(args=argv, prog_name="zeta", standalone_mode=False)
    except click.ClickException as error:
        error.show()
        return error.exit_code
    return int(result or 0)


if __name__ == "__main__":
    raise SystemExit(main())
