"""The `zeta attempts` command group."""

import json
from pathlib import Path

import click
from zeta.cli.common import runtime_event_store, state_dir_option


@click.group("attempts")
def attempts() -> None:
    """Inspect durable runtime attempts."""


@attempts.command("list")
@state_dir_option
@click.option("--json", "json_output", is_flag=True, help="Emit JSON.")
def attempts_list(state_dir: Path | None, json_output: bool) -> int:
    """List durable runtime attempts."""

    event_store = runtime_event_store(state_dir)
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
