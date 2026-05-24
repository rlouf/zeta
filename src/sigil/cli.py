"""Command-line boundary between shell bindings and the Sigil runtime.

The CLI is intentionally boring: shell integrations should call these commands
instead of reimplementing model calls, selectors, rendering, or state handling.
"""

from __future__ import annotations

import json
import sys

import click

from .ansi import MUTED, RESET
from .commands import generate, previous, select
from .failure import record_failure, select_fix, select_previous_fix
from .pi_stream import stream_events
from .question import ask
from .security import inherited_label, make_security, normalize_security, record_id
from .session import (
    clear_current_session,
    current_session_snapshot,
    known_sessions,
    session_paths,
)
from .state import append_event, read_json


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli() -> None:
    """Punctuation-native LLM interaction for the shell."""


@cli.command("command")
@click.argument("prompt")
@click.option("--select", "select_candidate", is_flag=True)
@click.option("--json", "json_output", is_flag=True)
def cmd_command(prompt: str, select_candidate: bool, json_output: bool) -> int:
    """Generate command candidates and optionally run the selector UI."""
    candidates = generate(prompt)
    source = normalize_security(read_json("last-command.json") or {})
    security = make_security(
        glyph=",",
        integrity="local_model",
        capability="propose",
        taint=["model"],
        inputs=[record_id(source)],
        input_records=[source],
        fresh_human=True,
    )
    if json_output:
        print(
            json.dumps({"prompt": prompt, "commands": candidates}, ensure_ascii=False)
        )
        return 0
    if select_candidate:
        command = select(prompt, candidates, security)
        if command:
            append_event({"type": "command_selected", "command": command, **security})
            print(command)
        return 0
    for item in candidates:
        print(item["command"])
    return 0


@cli.command("previous-command")
@click.option("--select", "select_candidate", is_flag=True)
@click.option("--json", "json_output", is_flag=True)
def cmd_previous_command(select_candidate: bool, json_output: bool) -> int:
    """Reopen the previous command candidates for the current shell session."""
    prompt, candidates, security = previous()
    continued = append_event(
        {"type": "command_continued", "prompt": prompt, **security}
    )
    security = {**security, "inputs": [continued["id"]]}
    print(
        f"{MUTED}❯ sigil ,, · inherited: {inherited_label(security)}{RESET}",
        file=sys.stderr,
    )
    if json_output:
        print(
            json.dumps(
                {"prompt": prompt, "commands": candidates, **security},
                ensure_ascii=False,
            )
        )
        return 0
    command = (
        select(prompt, candidates, security)
        if select_candidate
        else candidates[0]["command"]
    )
    if command:
        append_event({"type": "command_selected", "command": command, **security})
        print(command)
    return 0


@cli.command("question")
@click.argument("question")
@click.option("--json", "json_output", is_flag=True)
def cmd_question(question: str, json_output: bool) -> int:
    """Answer a fresh shell question and reset the session transcript."""
    return ask(question, json_output=json_output)


@cli.command("follow-up")
@click.argument("question")
@click.option("--json", "json_output", is_flag=True)
def cmd_follow_up(question: str, json_output: bool) -> int:
    """Continue the current session transcript with a follow-up question."""
    return ask(question, follow_up=True, json_output=json_output)


@cli.command("render-pi-stream", hidden=True)
@click.option("--json", "json_output", is_flag=True)
def cmd_render_pi_stream(json_output: bool) -> int:
    """Render Pi's JSON event stream for the question pipeline."""
    return stream_events(json_output=json_output)


def print_json(value: object) -> None:
    """Print inspection data in a stable machine-readable shape."""
    print(json.dumps(value, ensure_ascii=False, indent=2))


@cli.command("session")
@click.argument(
    "session_command",
    required=False,
    default="show",
    type=click.Choice(["show", "path", "list", "clear"]),
)
@click.option("--json", "json_output", is_flag=True)
def cmd_session(session_command: str, json_output: bool) -> int:
    """Inspect or clear the current shell session state."""
    if session_command == "path":
        paths = session_paths()
        if json_output:
            print_json(paths)
        else:
            print(paths["session"])
        return 0
    if session_command == "list":
        sessions = known_sessions()
        if json_output:
            print_json(sessions)
        else:
            for session in sessions:
                print(f"{session['session_id']}\t{session['path']}")
        return 0
    if session_command == "clear":
        removed = clear_current_session()
        if json_output:
            print_json({"removed": removed})
        else:
            if removed:
                for path in removed:
                    print(f"removed {path}")
            else:
                print("session already clear")
        return 0

    snapshot = current_session_snapshot()
    if json_output:
        print_json(snapshot)
    else:
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


@cli.group("failure")
def failure_group() -> None:
    """Failure state commands used by shell bindings."""


@failure_group.command("record")
@click.option("--status", type=int, required=True)
@click.option("--cwd")
@click.argument("command")
def cmd_failure_record(command: str, status: int, cwd: str | None) -> int:
    """Record a failed shell command for later repair."""
    record_failure(command, status, cwd)
    return 0


@cli.command("fix")
def cmd_fix() -> int:
    """Suggest fixes for the last recorded failed shell command."""
    command = select_fix()
    if command:
        append_event({"type": "fix_selected", "command": command})
        print(command)
    return 0


@cli.command("previous-fix")
def cmd_previous_fix() -> int:
    """Reopen previous repair candidates."""
    command = select_previous_fix()
    if command:
        append_event({"type": "fix_selected", "command": command})
        print(command)
    return 0


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
    return int(result or 0)


if __name__ == "__main__":
    raise SystemExit(main())
