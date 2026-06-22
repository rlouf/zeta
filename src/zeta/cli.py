"""Command-line entrypoint for the Zeta runtime."""

import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

import click

from agents.loader import load_specs_recursive
from zeta.agents.runtime import compile_agent_definitions
from zeta.dispatch import (
    EventDispatcher,
    QueueItemSnapshot,
    RegisteredAgent,
    attempt_snapshots,
    queue_item_snapshots,
    queue_item_status_counts,
)
from zeta.kernel.events import Event
from zeta.rpc import run_stdio
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


def runtime_agents(project_root: Path) -> list[RegisteredAgent]:
    agents_dir = project_root.expanduser().resolve() / "agents"
    if not agents_dir.exists():
        return []
    return [
        agent
        for spec in load_specs_recursive(agents_dir)
        for agent in compile_agent_definitions(spec)
    ]


def is_runtime_event(event: Event) -> bool:
    return event.event_type.startswith(("runtime.queue_item.", "runtime.attempt."))


def next_available_queue_item(
    snapshots: list[QueueItemSnapshot],
) -> QueueItemSnapshot | None:
    for snapshot in snapshots:
        if snapshot.status == "available":
            return snapshot
    return None


def next_unrouted_event(
    events: list[Event],
    snapshots: list[QueueItemSnapshot],
) -> Event | None:
    routed_event_ids = {snapshot.event_id for snapshot in snapshots}
    for event in events:
        if is_runtime_event(event):
            continue
        if event.id not in routed_event_ids:
            return event
    return None


async def run_once(
    event_store: SqliteEventStore,
    agents: list[RegisteredAgent],
) -> str:
    dispatcher = EventDispatcher(event_store, agents=agents)
    events = event_store.list_events(Filter())
    snapshots = queue_item_snapshots(events)
    available = next_available_queue_item(snapshots)
    if available is not None:
        await dispatcher.run_queue_item(available.queue_item_id)
        return f"ran {available.queue_item_id}"
    event = next_unrouted_event(events, snapshots)
    if event is None:
        return "queue empty"
    route = await dispatcher.route(event)
    if not route.queue_items:
        return f"routed {event.id}"
    queue_item = route.queue_items[0]
    await dispatcher.run_queue_item(queue_item)
    return f"ran {queue_item.queue_item_id}"


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

    if not once:
        raise click.UsageError("only --once is supported")
    event_store = runtime_event_store(project_root, state_dir)
    try:
        message = asyncio.run(run_once(event_store, runtime_agents(project_root)))
    finally:
        event_store.close()
    click.echo(message)
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
