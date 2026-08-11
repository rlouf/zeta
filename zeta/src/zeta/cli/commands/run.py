"""The `zeta run` command group."""

import asyncio
from pathlib import Path

import click
from zeta.cli.common import (
    cli_tool_registry,
    connector_names_from_option,
    state_dir_option,
)
from zeta.harness import worker


@click.command("run")
@click.option(
    "--project-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
    help="Project root containing agents/ specs.",
)
@state_dir_option
@click.option(
    "--connectors",
    help="Comma-separated connector allowlist for this runtime process.",
)
def run(
    project_root: Path,
    state_dir: Path | None,
    connectors: str | None,
) -> int:
    """Fire due schedules, then drain queued work until the queue is empty."""

    runtime = worker.build_worker_services(
        project_root=project_root,
        state_dir=state_dir,
        tool_registry=cli_tool_registry(),
        connector_names=connector_names_from_option(connectors),
    )

    async def drain() -> str:
        try:
            return await worker.run_until_idle(runtime)
        finally:
            await runtime.aclose()

    click.echo(asyncio.run(drain()))
    return 0
