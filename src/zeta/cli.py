"""Command-line entrypoint for the Zeta runtime."""

import sys

import click

from zeta.rpc import JsonRpcServer, run_rpc_session, session_event_dispatcher
from zeta.session import default_session
from zeta.store.events import EventReader


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli() -> None:
    """Zeta runtime commands."""


@cli.command("rpc")
@click.option("--stdio", is_flag=True, help="Serve newline-delimited JSON-RPC.")
def rpc(stdio: bool) -> int:
    """Serve the Zeta JSON-RPC protocol."""
    if not stdio:
        raise click.UsageError("only --stdio is supported")
    runtime_context = default_session()
    event_reader = (
        runtime_context.event_sink
        if isinstance(runtime_context.event_sink, EventReader)
        else None
    )
    server = JsonRpcServer(
        sys.stdin,
        sys.stdout,
        tool_registry=runtime_context.tool_registry,
        event_reader=event_reader,
        event_sink=runtime_context.event_sink,
    )
    server.session_runner = lambda params: run_rpc_session(
        params,
        publish_event=server.publish_event,
        runtime_context=runtime_context,
    )
    server.event_dispatcher = session_event_dispatcher(
        runtime_context,
        publish_event=server.publish_event,
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
