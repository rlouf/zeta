"""Root Click group and process entrypoint for the Sigil CLI.

Commands live in sibling modules and register on this group via decorators.
The group imports each module on first use: glyphs like `?` and the per-prompt
shell-turn recording must not pay for the heaviest workflow's import graph.
"""

import importlib
from contextlib import suppress

import click

from sigil._version import __version__

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
# sysexits EX_UNAVAILABLE: the model endpoint is the service that is down.
EXIT_MODEL_UNAVAILABLE = 69
EXIT_COMMAND_NOT_EXECUTABLE = 126
EXIT_COMMAND_NOT_FOUND = 127
EXIT_SIGNAL_BASE = 128
EXIT_INTERRUPTED = EXIT_SIGNAL_BASE + 2

MODEL_ERROR_EXIT_CODE = EXIT_MODEL_UNAVAILABLE


def examples(*lines: str) -> str:
    """Render command invocations as an Examples epilog click keeps verbatim."""
    block = "\n".join(f"  {line}" for line in lines)
    return f"\b\nExamples:\n{block}"


COMMAND_MODULES = {
    "ask": "sigil.cli.step",
    "blame": "sigil.cli.log",
    "doctor": "sigil.cli.install",
    "events": "sigil.cli.events",
    "install": "sigil.cli.install",
    "log": "sigil.cli.log",
    "model": "sigil.cli.model",
    "run": "sigil.cli.run",
    "session": "sigil.cli.session",
    "status": "sigil.cli.status",
    "step": "sigil.cli.step",
    "trace": "sigil.cli.trace",
}


class LazyCommandGroup(click.Group):
    """Import a command's module the first time the command is looked up."""

    def list_commands(self, ctx: click.Context) -> list[str]:
        return sorted({*super().list_commands(ctx), *COMMAND_MODULES})

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        command = super().get_command(ctx, cmd_name)
        if command is not None:
            return command
        module_name = COMMAND_MODULES.get(cmd_name)
        if module_name is None:
            return None
        importlib.import_module(module_name)
        return super().get_command(ctx, cmd_name)


@click.group(
    cls=LazyCommandGroup,
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=True,
)
@click.version_option(__version__, "-V", "--version", prog_name="sigil")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Natural-language shell assistant.

    Sigil installs punctuation glyphs into your shell, plus named commands for
    setup and inspection.

    Common workflows:

    \b
      ,      ask from local context
      ,,     propose one reviewed agent step
      ,,,    do one auto-approved agent step
      +      run one explicit command and capture output
      ?      status for the current session

    Setup and diagnostics:

    \b
      sigil install          install zsh glyph bindings
      sigil doctor           check install, shell, state, and model endpoint
      sigil status           show current session status
      sigil events           inspect recent Sigil activity

    Use "sigil COMMAND --help" for command-specific options.
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        ctx.exit(EXIT_OK)
    # The binding spools shell turns with zero forks; the CLI is the reader.
    # Recording must never break a command, mirroring the binding's fail-open
    # writes.
    from sigil.sessions import ingest_spooled_turns

    with suppress(OSError):
        ingest_spooled_turns()


def main(argv: list[str] | None = None) -> int:
    """Parse the shell-agnostic Sigil CLI surface."""
    try:
        result = cli.main(args=argv, prog_name="sigil", standalone_mode=False)
    except click.ClickException as error:
        error.show()
        return error.exit_code
    except click.Abort:
        click.echo("Aborted!", err=True)
        return EXIT_ERROR
    except click.exceptions.Exit as error:
        return int(error.exit_code)
    except Exception as error:
        if type(error).__name__ == "IncompatibleSchemaError":
            click.echo(f"sigil: {error}", err=True)
            click.echo(
                "Run `sigil trace reinit-store --yes` to recreate the local store.",
                err=True,
            )
            return EXIT_ERROR
        if isinstance(error, RuntimeError):
            click.echo(f"sigil: {error}", err=True)
            click.echo(
                "Check the model endpoint with `sigil doctor`, then retry.", err=True
            )
            return EXIT_MODEL_UNAVAILABLE
        if isinstance(error, FileNotFoundError):
            program = error.filename or "required executable"
            click.echo(f"sigil: missing executable: {program}", err=True)
            click.echo("Install it or make sure it is on PATH, then retry.", err=True)
            return EXIT_COMMAND_NOT_FOUND
        if isinstance(error, PermissionError):
            target = error.filename or "requested path"
            click.echo(f"sigil: permission denied: {target}", err=True)
            click.echo(
                "Check the path permissions or set SIGIL_STATE_DIR to a writable directory.",
                err=True,
            )
            return EXIT_ERROR
        raise
    return int(result or EXIT_OK)
