"""The `status` command: the current session's shortest useful status."""

from __future__ import annotations

import click

from ._base import cli
from ._shared import pretty_print_json
from ..status import current_status, format_status


@cli.command("status")
@click.option("--json", "json_output", is_flag=True)
def cmd_status(json_output: bool) -> int:
    """Show the current session's shortest useful status."""
    status = current_status()
    if json_output:
        pretty_print_json(status.to_dict())
    else:
        print(format_status(status))
    if status.state != "clean":
        raise click.exceptions.Exit(1)
    return 0
