"""Root Click group and process entrypoint for the Sigil CLI.

Commands live in sibling modules and register on this group via decorators.
`__init__` imports those modules so the decorators run.
"""

from __future__ import annotations

import click

from .._version import __version__

# sysexits EX_UNAVAILABLE: the model endpoint is the service that is down.
MODEL_ERROR_EXIT_CODE = 69


@click.group(
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=True,
)
@click.version_option(__version__, "-V", "--version", prog_name="sigil")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Natural-language shell assistant.

    Sigil installs punctuation glyphs into your shell, plus named commands for
    setup and inspection.

    Common routes:

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
        ctx.exit(0)


def main(argv: list[str] | None = None) -> int:
    """Parse the shell-agnostic Sigil CLI surface."""
    try:
        result = cli.main(args=argv, prog_name="sigil", standalone_mode=False)
    except click.ClickException as error:
        error.show()
        return error.exit_code
    except click.Abort:
        click.echo("Aborted!", err=True)
        return 1
    except click.exceptions.Exit as error:
        return int(error.exit_code)
    except RuntimeError as error:
        click.echo(f"sigil: {error}", err=True)
        click.echo(
            "Check the model endpoint with `sigil doctor`, then retry.", err=True
        )
        return MODEL_ERROR_EXIT_CODE
    except FileNotFoundError as error:
        program = error.filename or "required executable"
        click.echo(f"sigil: missing executable: {program}", err=True)
        click.echo("Install it or make sure it is on PATH, then retry.", err=True)
        return 127
    except PermissionError as error:
        target = error.filename or "requested path"
        click.echo(f"sigil: permission denied: {target}", err=True)
        click.echo(
            "Check the path permissions or set SIGIL_STATE_DIR to a writable directory.",
            err=True,
        )
        return 1
    return int(result or 0)
