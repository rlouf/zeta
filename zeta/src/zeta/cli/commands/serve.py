"""The `zeta serve` command group."""

import asyncio
from pathlib import Path

import click
from zeta.cli.common import cli_tool_registry, connector_names_from_option
from zeta.harness import worker


@click.command("serve")
@click.option(
    "--project-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
    help="Project root containing agents/ specs.",
)
@click.option(
    "--state-dir",
    type=click.Path(file_okay=False, path_type=Path),
    help="Override the runtime state directory.",
)
@click.option(
    "--connectors",
    help="Comma-separated connector allowlist for this runtime process.",
)
def serve(
    project_root: Path,
    state_dir: Path | None,
    connectors: str | None,
) -> int:
    """Run the worker continuously, firing due schedules."""
    runtime = worker.build_worker_services(
        project_root=project_root,
        state_dir=state_dir,
        tool_registry=cli_tool_registry(),
        connector_names=connector_names_from_option(connectors),
    )

    async def run_runtime() -> None:
        try:
            await worker.run_forever(runtime)
        finally:
            await runtime.aclose()

    asyncio.run(run_runtime())
    return 0
