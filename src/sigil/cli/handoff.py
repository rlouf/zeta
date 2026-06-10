"""Internal shell-handoff commands for shell bindings."""

from __future__ import annotations

import click

from .. import handoff
from ._base import cli
from ._shared import pretty_print_json


@cli.group("handoff", hidden=True)
def cmd_handoff() -> None:
    """Record and reconcile shell turns after a Zeta handoff."""


@cmd_handoff.command("shell-turn")
@click.option("--command", required=True, help="Command text the user executed.")
@click.option("--status", type=int, required=True, help="Command exit status.")
@click.option("--cwd", default=None, help="Working directory of the command.")
def handoff_shell_turn(command: str, status: int, cwd: str | None) -> int:
    """Record one shell command executed after a Zeta handoff."""
    pretty_print_json(handoff.append_shell_turn(command, status, cwd))
    return 0
