"""Command-line entrypoint for the Zeta runtime."""

import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

import click

from zeta.dispatch import (
    attempt_snapshots,
    queue_item_snapshots,
    queue_item_status_counts,
)
from zeta.kernel.events import Event
from zeta.rpc import run_stdio
from zeta.runtime import local as runtime_local
from zeta.store.events import Filter, SqliteEventStore, event_store_path

QUEUE_STATUS_ORDER = (
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


def runtime_event_store(project_root: Path, state_dir: Path | None) -> SqliteEventStore:
    return SqliteEventStore(
        event_store_path(runtime_state_dir(project_root, state_dir))
    )


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
    """List projected runtime queue items."""

    event_store = runtime_event_store(project_root, state_dir)
    try:
        snapshots = queue_item_snapshots(
            event_store.list_events(Filter(event_type_prefix="runtime.queue_item."))
        )
    finally:
        event_store.close()
    if json_output:
        click.echo(
            json.dumps(
                [asdict(snapshot) for snapshot in snapshots],
                ensure_ascii=False,
            )
        )
        return 0
    if not snapshots:
        click.echo("queue empty")
        return 0
    for snapshot in snapshots:
        click.echo(
            "\t".join(
                [
                    snapshot.status,
                    snapshot.queue_item_id,
                    snapshot.target_agent,
                    snapshot.event_id,
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
    """List projected runtime attempts."""

    event_store = runtime_event_store(project_root, state_dir)
    try:
        snapshots = attempt_snapshots(
            event_store.list_events(Filter(event_type_prefix="runtime.attempt."))
        )
    finally:
        event_store.close()
    if json_output:
        click.echo(
            json.dumps(
                [asdict(snapshot) for snapshot in snapshots],
                ensure_ascii=False,
            )
        )
        return 0
    if not snapshots:
        click.echo("attempts empty")
        return 0
    for snapshot in snapshots:
        click.echo(
            "\t".join(
                [
                    snapshot.status,
                    snapshot.attempt_id,
                    snapshot.queue_item_id,
                    snapshot.target_agent,
                ]
            )
        )
    return 0


@cli.command("events")
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
def events(
    project_root: Path,
    state_dir: Path | None,
    type_prefix: str | None,
    session_id: str | None,
    limit: int,
    json_output: bool,
) -> int:
    """List durable runtime events."""

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


@cli.command("run")
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
def run(project_root: Path, state_dir: Path | None, once: bool) -> int:
    """Run the local runtime worker."""

    runtime = runtime_local.build_runtime(
        project_root=project_root, state_dir=state_dir
    )
    try:
        if once:
            message = asyncio.run(runtime_local.run_once(runtime))
            click.echo(message)
        else:
            asyncio.run(runtime_local.run_forever(runtime))
    finally:
        runtime.close()
    return 0


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
    """Show projected runtime queue counts."""

    event_store = runtime_event_store(project_root, state_dir)
    try:
        snapshots = queue_item_snapshots(
            event_store.list_events(Filter(event_type_prefix="runtime.queue_item."))
        )
    finally:
        event_store.close()
    counts = queue_item_status_counts(snapshots)
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
