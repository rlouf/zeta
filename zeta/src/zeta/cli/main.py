"""Command-line entrypoint for the Zeta runtime.

Each command group lives in its own module under `commands/`. This module owns
the root group and the process exit contract only.
"""

import click
from zeta.cli.commands.agents import agents
from zeta.cli.commands.attempts import attempts
from zeta.cli.commands.events import events, waits
from zeta.cli.commands.new import new
from zeta.cli.commands.ps import ps
from zeta.cli.commands.queue import queue
from zeta.cli.commands.rpc import rpc
from zeta.cli.commands.run import run
from zeta.cli.commands.schedules import schedules
from zeta.cli.commands.serve import serve
from zeta.cli.models import models_group
from zeta.cli.traces import traces_group


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli() -> None:
    """Zeta runtime commands."""


cli.add_command(queue)
cli.add_command(attempts)
cli.add_command(events)
cli.add_command(waits)
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
