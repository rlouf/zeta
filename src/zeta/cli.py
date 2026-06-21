"""Command-line entrypoint for the Zeta runtime."""

import asyncio
import sys
from typing import Any, cast

import click

from zeta.dispatch import EventDispatcher
from zeta.loop import CancellationToken
from zeta.rpc import (
    JsonRpcServer,
    rpc_cancellation_event_param,
    rpc_error_from_session_request,
    rpc_run_id_param,
)
from zeta.session import (
    SessionRequestError,
    default_session,
    session_turn_agent,
    submit_session_turn,
)
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
    cancellation_events: dict[str, CancellationToken] = {}
    dispatcher = EventDispatcher(
        runtime_context.event_sink,
        agents=[
            session_turn_agent(
                runtime_context,
                publish_event=server.publish_event,
                cancellation_event_for_run=cancellation_events.get,
            )
        ],
        publish_event=server.publish_event,
    )
    server.event_dispatcher = dispatcher

    async def run_shared_rpc_session(params: dict[str, Any]) -> dict[str, Any]:
        run_id = rpc_run_id_param(params)
        cancellation_event = rpc_cancellation_event_param(params)
        if run_id is not None and cancellation_event is not None:
            cancellation_events[run_id] = cast(CancellationToken, cancellation_event)
        try:
            return await submit_session_turn(
                params,
                run_id=run_id,
                runtime_context=runtime_context,
                event_dispatcher=dispatcher,
            )
        except SessionRequestError as exc:
            raise rpc_error_from_session_request(exc) from exc
        finally:
            if run_id is not None:
                cancellation_events.pop(run_id, None)

    server.session_runner = run_shared_rpc_session
    asyncio.run(server.serve())
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
