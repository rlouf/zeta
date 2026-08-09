"""Command-line entrypoint for the Zeta runtime.

Each command group lives in its own module under `commands/`. This module owns
the root group and the process exit contract only.
"""

import os
import sys
import sysconfig
from pathlib import Path

import click
from zeta.cli.commands.agents import agents
from zeta.cli.commands.attempts import attempts
from zeta.cli.commands.events import cancel, events, sessions, waits
from zeta.cli.commands.new import new
from zeta.cli.commands.ps import ps
from zeta.cli.commands.queue import queue
from zeta.cli.commands.rpc import rpc
from zeta.cli.commands.run import run
from zeta.cli.commands.schedules import schedules
from zeta.cli.commands.serve import serve
from zeta.cli.models import models_group
from zeta.cli.traces import traces_group


@click.group(
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=True,
)
@click.pass_context
def cli(context: click.Context) -> None:
    """Runs the interactive interface by default and exposes runtime commands."""
    if context.invoked_subcommand is not None:
        return
    suffix = ".exe" if os.name == "nt" else ""
    executable = Path(sysconfig.get_path("scripts")) / f"zeta-tui{suffix}"
    zeta = Path(sys.argv[0]).resolve()
    try:
        os.execv(executable, [str(executable), str(zeta)])
    except OSError as error:
        raise click.ClickException(f"cannot launch bundled TUI: {error}") from error


cli.add_command(queue)
cli.add_command(attempts)
cli.add_command(events)
cli.add_command(waits)
cli.add_command(sessions)
cli.add_command(cancel)
cli.add_command(new)
cli.add_command(ps)
cli.add_command(run)
cli.add_command(serve)
cli.add_command(schedules)
cli.add_command(agents)
cli.add_command(rpc)
cli.add_command(traces_group)
cli.add_command(models_group)


def main(argv: list[str] | None = None) -> int:
    try:
        result = cli.main(args=argv, prog_name="zeta", standalone_mode=False)
    except click.ClickException as error:
        error.show()
        return error.exit_code
    except NotADirectoryError as error:
        cli_error = click.ClickException(str(error))
        cli_error.show()
        return cli_error.exit_code
    return int(result or 0)
