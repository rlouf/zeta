"""Status command for shell-native diagnostics."""

import click

from ..status import current_status, format_status
from ._base import cli, examples
from ._shared import pretty_print_json


@cli.command(
    "status",
    epilog=examples(
        "sigil status",
        "sigil status --json",
    ),
)
@click.option(
    "--json", "json_output", is_flag=True, help="Emit status as JSON for scripts."
)
def cmd_status(json_output: bool) -> int:
    """Show the current session's status without calling a model.

    The `?` glyph calls this command. It reports the last failure, the
    last delegation outcome, pending staged work, today's session cost,
    and the active model with its selection source.

    Exits 1 when the session needs attention — the last recorded command
    failed — and 0 when clean.
    """
    status = current_status()
    if json_output:
        pretty_print_json(status.to_dict())
    else:
        print(format_status(status))
    if status.state != "clean":
        return 1
    return 0
