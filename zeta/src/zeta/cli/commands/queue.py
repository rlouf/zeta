"""The `zeta queue` command group."""

import json
from pathlib import Path

import click
from zeta.cli.common import runtime_event_store, state_dir_option


@click.group("queue")
def queue() -> None:
    """Inspect durable runtime queue items."""


@queue.command("list")
@state_dir_option
@click.option("--json", "json_output", is_flag=True, help="Emit JSON.")
def queue_list(state_dir: Path | None, json_output: bool) -> int:
    """List durable runtime queue items."""

    event_store = runtime_event_store(state_dir)
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


@queue.command("status")
@state_dir_option
def queue_status(state_dir: Path | None) -> int:
    """Show durable runtime queue counts."""

    event_store = runtime_event_store(state_dir)
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


QUEUE_STATUS_ORDER = (
    "pending",
    "available",
    "claimed",
    "completed",
    "failed",
    "cancelled",
    "retry_scheduled",
    "dead_lettered",
    "unhandled",
)
