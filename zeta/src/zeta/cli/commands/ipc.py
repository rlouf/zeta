"""The `zeta ipc` command group."""

import sys

import click
from zeta.ipc.stdio import run_stdio


@click.group("ipc")
def ipc() -> None:
    """Serve the Zeta process communication protocol."""


@ipc.command("stdio")
def ipc_stdio() -> int:
    """Serve IPC over standard input and output."""

    run_stdio(sys.stdin, sys.stdout)
    return 0
