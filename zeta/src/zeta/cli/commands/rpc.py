"""The `zeta rpc` command group."""

import sys

import click
from zeta.rpc.stdio import run_stdio


@click.group("rpc")
def rpc() -> None:
    """Serve the Zeta JSON-RPC protocol."""


@rpc.command("stdio")
def rpc_stdio() -> int:
    """Serve newline-delimited JSON-RPC over standard I/O."""

    run_stdio(sys.stdin, sys.stdout)
    return 0
