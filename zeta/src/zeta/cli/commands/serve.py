"""The `zeta serve` command group."""

import asyncio
from pathlib import Path

import click
from zeta.cli.common import cli_tool_registry, connector_names_from_option
from zeta.harness import worker
from zeta.rpc.eventlog import eventlog_rpc_step


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
@click.option(
    "--host", default="127.0.0.1", show_default=True, help="Push ingress host."
)
@click.option("--port", default=8080, show_default=True, help="Push ingress port.")
@click.option(
    "--route-prefix",
    default="/connectors",
    show_default=True,
    help="Push ingress route prefix.",
)
def serve(
    project_root: Path,
    state_dir: Path | None,
    connectors: str | None,
    host: str,
    port: int,
    route_prefix: str,
) -> int:
    """Run the worker continuously, firing due schedules and serving push ingress."""
    runtime = worker.build_worker_services(
        project_root=project_root,
        state_dir=state_dir,
        tool_registry=cli_tool_registry(),
        connector_names=connector_names_from_option(connectors),
        rpc_step=eventlog_rpc_step,
    )

    async def run_runtime() -> None:
        try:
            await worker.run_forever(
                runtime,
                push_host=host,
                push_port=port,
                push_route_prefix=route_prefix,
            )
        finally:
            await runtime.aclose()

    asyncio.run(run_runtime())
    return 0
