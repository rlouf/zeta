"""The `log` group: queries over the delegation ledger."""

from __future__ import annotations

import click

from ._base import cli


@cli.group("log")
def cmd_log() -> None:
    """Query the delegation ledger."""


@cmd_log.command("reindex")
def cmd_log_reindex() -> int:
    """Rebuild the ledger index from the event log."""
    # Imported lazily: `sigil.cli` must stay light at import time.
    from ..ledger import default_ledger_index, reindex

    turns, effects = reindex(default_ledger_index())
    click.echo(f"indexed {turns} turn record(s), {effects} effect record(s)")
    return 0
