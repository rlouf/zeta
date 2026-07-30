"""The `zeta ps` command group."""

import json
from pathlib import Path
from typing import cast

import click
from zeta.cli.common import runtime_event_store, state_dir_option
from zeta.cli.rendering import (
    run_detail_record,
    run_display_id,
    run_summary_records,
    run_summary_text,
)


@click.command("ps")
@click.argument("run_id", required=False)
@state_dir_option
@click.option("--json", "json_output", is_flag=True, help="Emit JSON.")
def ps(run_id: str | None, state_dir: Path | None, json_output: bool) -> int:
    """List durable runtime runs, or show one RUN_ID."""

    event_store = runtime_event_store(state_dir)
    try:
        if run_id is not None:
            return print_run_detail(
                run_detail_record(event_store, run_id),
                run_id=run_id,
                json_output=json_output,
            )
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


def print_run_detail(
    record: dict[str, object] | None,
    *,
    run_id: str,
    json_output: bool,
) -> int:
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
