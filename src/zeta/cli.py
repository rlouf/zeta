"""Command-line entrypoint for the Zeta runtime."""

from __future__ import annotations

import sys

import click

from .context import default_context
from .rpc import JsonRpcServer, run_rpc_session


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli() -> None:
    """Zeta runtime commands."""


@cli.command("rpc")
@click.option("--stdio", is_flag=True, help="Serve newline-delimited JSON-RPC.")
def rpc(stdio: bool) -> int:
    """Serve the Zeta JSON-RPC protocol."""
    if not stdio:
        raise click.UsageError("only --stdio is supported")
    runtime_context = default_context()
    server = JsonRpcServer(
        sys.stdin,
        sys.stdout,
        tool_registry=runtime_context.tool_registry,
        event_reader=runtime_context.event_sink,
    )
    server.session_runner = lambda params: run_rpc_session(
        params,
        publish_event=server.publish_event,
        runtime_context=runtime_context,
    )
    server.serve()
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
