"""The `session` command: inspect or clear the current shell session state."""

import click

from sigil.cli._base import cli, examples
from sigil.cli._shared import pretty_print_json
from sigil.sessions import (
    clear_current_session,
    current_session_snapshot,
    known_sessions,
    rename_current_session,
    session_paths,
)

JSON_HELP = "Emit session state as JSON."


@cli.group(
    "session",
    invoke_without_command=True,
    epilog=examples(
        "sigil session",
        "sigil session transcript --limit 20",
        'sigil session rename "frontend work"',
        "sigil session clear",
    ),
)
@click.option("--json", "json_output", is_flag=True, help=JSON_HELP)
@click.pass_context
def cmd_session(ctx: click.Context, json_output: bool) -> None:
    """Inspect or clear the current shell session state.

    A session is one terminal: the zsh binding sets SIGIL_SESSION_ID per
    pty, so terminal windows and tmux panes keep separate continuity while
    subshells share it. Bare `sigil session` runs `session show`.

    State lives under ~/.sigil/; set SIGIL_STATE_DIR to move it.
    """
    if ctx.invoked_subcommand is None:
        ctx.exit(print_session_snapshot(json_output))


@cmd_session.command(
    "show",
    epilog=examples(
        "sigil session show",
        "sigil session show --json",
    ),
)
@click.option("--json", "json_output", is_flag=True, help=JSON_HELP)
def session_show(json_output: bool) -> int:
    """Show the current session's continuity files.

    Prints the session id, its state directory, and a one-line summary of
    each continuity file present. Bare `sigil session` runs this command.
    """
    return print_session_snapshot(json_output)


@cmd_session.command(
    "path",
    epilog=examples(
        "sigil session path",
        "ls $(sigil session path)",
    ),
)
@click.option("--json", "json_output", is_flag=True, help=JSON_HELP)
def session_path(json_output: bool) -> int:
    """Print the current session state directory.

    The directory lives under ~/.sigil/ unless SIGIL_STATE_DIR moves it.
    """
    return print_session_path(json_output)


@cmd_session.command(
    "list",
    epilog=examples("sigil session list"),
)
@click.option("--json", "json_output", is_flag=True, help=JSON_HELP)
def session_list(json_output: bool) -> int:
    """List all known shell sessions.

    Each line shows a session id, optional display name, last working
    directory, last event type, and state directory, tab-separated.
    """
    return print_session_list(json_output)


@cmd_session.command(
    "rename",
    epilog=examples(
        'sigil session rename "frontend work"',
        "sigil session rename frontend work",
    ),
)
@click.argument("name", nargs=-1, required=True)
@click.option("--json", "json_output", is_flag=True, help=JSON_HELP)
def session_rename(name: tuple[str, ...], json_output: bool) -> int:
    """Give the current session a human display name."""
    clean_name = " ".join(" ".join(name).split())
    if not clean_name:
        raise click.UsageError("session name cannot be blank")
    renamed = rename_current_session(clean_name)
    if json_output:
        pretty_print_json(renamed)
        return 0
    print(f"renamed session {renamed['session_id']} -> {renamed['name']}")
    return 0


@cmd_session.command(
    "clear",
    epilog=examples("sigil session clear"),
)
@click.option("--json", "json_output", is_flag=True, help=JSON_HELP)
def session_clear(json_output: bool) -> int:
    """Clear state scoped to the current session.

    Removes this session's continuity files and clears this session's trace
    records. The databases themselves survive.
    """
    return print_session_clear(json_output)


@cmd_session.command(
    "transcript",
    epilog=examples(
        "sigil session transcript",
        "sigil session transcript --limit 20",
    ),
)
@click.option("--limit", type=int, default=None, help="Show only the last N events.")
@click.option("--json", "json_output", is_flag=True, help=JSON_HELP)
def session_transcript(limit: int | None, json_output: bool) -> int:
    """Render the session's agent conversation as a transcript.

    Shows questions, answers, and compact tool traces. Each answer is
    tagged with the id of the exact prompt the model saw, usable with
    `sigil trace show`; model reasoning appears in full as italic
    markdown above the answer it led to.
    """
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
            str(session.get("name") or "-"),
            str(session.get("last_cwd") or "-"),
            str(session.get("last_event_type") or "-"),
            str(session["path"]),
        ]
        print("\t".join(parts))
    return 0


def print_session_clear(json_output: bool) -> int:
    """Clear the current session state and report what changed."""
    result = clear_current_session()
    if json_output:
        pretty_print_json(result)
        return 0
    removed = result["removed"]
    cleared = result["cleared"]
    for path in removed:
        print(f"removed {path}")
    for path in cleared:
        print(f"cleared session records from {path}")
    if not removed and not cleared:
        print("session already clear")
    return 0


def print_session_transcript(limit: int | None, json_output: bool) -> int:
    """Render the current session timeline as a conversation."""
    # Imported lazily: `sigil.cli` must not load zeta or rich at import time.
    from sigil import zeta_session_for_sigil
    from sigil.agent_io import current_timeline
    from zeta.records.events import event_view

    events = [
        event_view(event)
        for event in current_timeline(runtime_context=zeta_session_for_sigil())
    ]
    if limit is not None and limit > 0:
        events = events[-limit:]
    if json_output:
        pretty_print_json(events)
        return 0
    if not events:
        print("no agent turns recorded in this session")
        return 0
    from rich.console import Console

    from sigil.display.render import render_transcript

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
    if snapshot.get("name"):
        print(f"name {snapshot['name']}")
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
