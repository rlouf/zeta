"""Status command for shell-native diagnostics."""

from __future__ import annotations

import click

from ..status import current_status, format_status
from ._base import cli
from ._shared import pretty_print_json


@cli.command("status")
@click.option("--json", "json_output", is_flag=True, help="Emit status as JSON.")
def cmd_status(json_output: bool) -> int:
    """Show the current session's shortest useful status."""
    status = current_status()
    if json_output:
        pretty_print_json(status.to_dict())
    else:
        print(format_status(status))
    if status.state != "clean":
        return 1
    return 0
