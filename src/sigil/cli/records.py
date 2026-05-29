"""Hidden recording commands invoked by the shell bindings."""

from __future__ import annotations

import click

from ._base import cli
from ..failure import record_failure
from ..session import record_turn


@cli.command("record-failure", hidden=True)
@click.option("--status", type=int, required=True)
@click.option("--cwd")
@click.option("--stdout-snippet", default="")
@click.option("--stderr-snippet", default="")
@click.argument("command")
def cmd_record_failure(
    command: str,
    status: int,
    cwd: str | None,
    stdout_snippet: str,
    stderr_snippet: str,
) -> int:
    """Record a failed shell command for later comma proposal context."""
    record_failure(command, status, cwd, stdout_snippet, stderr_snippet)
    return 0


@cli.command("record-turn", hidden=True)
@click.option("--status", type=int, required=True)
@click.option("--cwd")
@click.option("--stdout-snippet", default="")
@click.option("--stderr-snippet", default="")
@click.argument("command")
def cmd_record_turn(
    command: str,
    status: int,
    cwd: str | None,
    stdout_snippet: str,
    stderr_snippet: str,
) -> int:
    """Record one shell turn; fans out to failure recording on non-zero exit."""
    record_turn(command, status, cwd, stdout_snippet, stderr_snippet)
    return 0
