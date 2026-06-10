"""The `session` command: inspect or clear the current shell session state."""

from __future__ import annotations

import click

from ..session import (
    clear_current_session,
    current_session_snapshot,
    known_sessions,
    session_paths,
)
from ._base import cli
from ._shared import pretty_print_json

JSON_HELP = "Emit session state as JSON."


@cli.group("session", invoke_without_command=True)
@click.option("--json", "json_output", is_flag=True, help=JSON_HELP)
@click.pass_context
def cmd_session(ctx: click.Context, json_output: bool) -> None:
    """Inspect or clear the current shell session state."""
    if ctx.invoked_subcommand is None:
        ctx.exit(print_session_snapshot(json_output))


@cmd_session.command("show")
@click.option("--json", "json_output", is_flag=True, help=JSON_HELP)
def session_show(json_output: bool) -> int:
    """Show the current session's continuity files."""
    return print_session_snapshot(json_output)


@cmd_session.command("path")
@click.option("--json", "json_output", is_flag=True, help=JSON_HELP)
def session_path(json_output: bool) -> int:
    """Print the current session state directory."""
    return print_session_path(json_output)


@cmd_session.command("list")
@click.option("--json", "json_output", is_flag=True, help=JSON_HELP)
def session_list(json_output: bool) -> int:
    """List all known shell sessions."""
    return print_session_list(json_output)


@cmd_session.command("clear")
@click.option("--json", "json_output", is_flag=True, help=JSON_HELP)
def session_clear(json_output: bool) -> int:
    """Remove the current session's state directory."""
    return print_session_clear(json_output)


@cmd_session.command("transcript")
@click.option("--limit", type=int, default=None, help="Show only the last N events.")
@click.option("--json", "json_output", is_flag=True, help=JSON_HELP)
def session_transcript(limit: int | None, json_output: bool) -> int:
    """Render the session's agent conversation as a transcript."""
    return print_session_transcript(limit, json_output)


def print_session_path(json_output: bool) -> int:
    """Print the current session state directory."""
    paths = session_paths()
    if json_output:
        pretty_print_json(paths)
    else:
        print(paths["session"])
    return 0


def print_session_list(json_output: bool) -> int:
    """Print all known shell sessions."""
    sessions = known_sessions()
    if json_output:
        pretty_print_json(sessions)
        return 0
    if not sessions:
        print("no sessions recorded")
        return 0
    for session in sessions:
        parts = [
            str(session["session_id"]),
            str(session.get("last_cwd") or "-"),
            str(session.get("last_event_type") or "-"),
            str(session["path"]),
        ]
        print("\t".join(parts))
    return 0


def print_session_clear(json_output: bool) -> int:
    """Clear the current session state and report removed paths."""
    removed = clear_current_session()
    if json_output:
        pretty_print_json({"removed": removed})
        return 0
    if removed:
        for path in removed:
            print(f"removed {path}")
    else:
        print("session already clear")
    return 0


def print_session_transcript(limit: int | None, json_output: bool) -> int:
    """Render the current session timeline as a conversation."""
    # Imported lazily: `sigil.cli` must not load zeta or rich at import time.
    from ..zeta.timeline import current_timeline

    events = current_timeline()
    if limit is not None and limit > 0:
        events = events[-limit:]
    if json_output:
        pretty_print_json(events)
        return 0
    if not events:
        print("no agent turns recorded in this session")
        return 0
    from rich.console import Console

    from ..display.render import render_transcript

    console = Console()
    if console.is_terminal:
        with console.pager(styles=True):
            render_transcript(events, console=console)
        return 0
    render_transcript(events, console=console)
    return 0


def print_session_snapshot(json_output: bool) -> int:
    """Print a summary of the current session's state files."""
    snapshot = current_session_snapshot()
    if json_output:
        pretty_print_json(snapshot)
        return 0
    print(f"session {snapshot['session_id']}")
    print(snapshot["path"])
    for name, value in snapshot["files"].items():
        if value is None:
            continue
        if isinstance(value, list):
            print(f"{name}: {len(value)} entries")
        elif isinstance(value, dict):
            print(f"{name}: {len(value)} keys")
        else:
            print(f"{name}: present")
    return 0
