"""Command-line entrypoint for the Zeta runtime."""

import sys

import click

from zeta.rpc import run_stdio


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli() -> None:
    """Zeta runtime commands."""


@cli.command("rpc")
@click.option("--stdio", is_flag=True, help="Serve newline-delimited JSON-RPC.")
def rpc(stdio: bool) -> int:
    """Serve the Zeta JSON-RPC protocol."""
    if not stdio:
        raise click.UsageError("only --stdio is supported")
    run_stdio(sys.stdin, sys.stdout)
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        result = cli.main(args=argv, prog_name="zeta", standalone_mode=False)
    except click.ClickException as error:
        error.show()
        return error.exit_code
    return int(result or 0)


if __name__ == "__main__":
    raise SystemExit(main())
