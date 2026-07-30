"""The `zeta schedules` command group."""

import json
from pathlib import Path

import click
from zeta.cli.common import state_dir_option
from zeta.harness import scheduling


@click.group("schedules")
def schedules() -> None:
    """Inspect authored-agent schedules."""


@schedules.command("status")
@click.option(
    "--project-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
    help="Project root containing agents/ specs.",
)
@state_dir_option
@click.option("--json", "json_output", is_flag=True, help="Emit JSON.")
def schedules_status(
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
