"""Root Click group and process entrypoint for the Sigil CLI.

Commands live in sibling modules and register on this group via decorators.
`__init__` imports those modules so the decorators run.
"""

from __future__ import annotations

import click


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli() -> None:
    pass


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
