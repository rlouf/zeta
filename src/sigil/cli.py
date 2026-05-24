"""Command-line boundary between shell bindings and the Sigil runtime.

The CLI is intentionally boring: shell integrations should call these commands
instead of reimplementing model calls, selectors, rendering, or state handling.
"""

from __future__ import annotations

import argparse
import json
import sys

from .ansi import MUTED, RESET
from .commands import generate, previous, select
from .failure import record_failure, select_fix, select_previous_fix
from .pi_stream import stream_events
from .question import ask
from .security import inherited_label, make_security, normalize_security, record_id
from .session import clear_current_session, current_session_snapshot, known_sessions, session_paths
from .state import append_event, read_json


def cmd_command(args: argparse.Namespace) -> int:
    """Generate command candidates and optionally run the selector UI."""
    candidates = generate(args.prompt)
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
    if args.json:
        print(json.dumps({"prompt": args.prompt, "commands": candidates}, ensure_ascii=False))
        return 0
    if args.select:
        command = select(args.prompt, candidates, security)
        if command:
            append_event({"type": "command_selected", "command": command, **security})
            print(command)
        return 0
    for item in candidates:
        print(item["command"])
    return 0


def cmd_previous_command(args: argparse.Namespace) -> int:
    """Reopen the previous command candidates for the current shell session."""
    prompt, candidates, security = previous()
    continued = append_event({"type": "command_continued", "prompt": prompt, **security})
    security = {**security, "inputs": [continued["id"]]}
    print(
        f"{MUTED}❯ sigil ,, · inherited: {inherited_label(security)}{RESET}",
        file=sys.stderr,
    )
    if args.json:
        print(json.dumps({"prompt": prompt, "commands": candidates, **security}, ensure_ascii=False))
        return 0
    command = select(prompt, candidates, security) if args.select else candidates[0]["command"]
    if command:
        append_event({"type": "command_selected", "command": command, **security})
        print(command)
    return 0


def cmd_question(args: argparse.Namespace) -> int:
    """Answer a fresh shell question and reset the session transcript."""
    return ask(args.question)


def cmd_follow_up(args: argparse.Namespace) -> int:
    """Continue the current session transcript with a follow-up question."""
    return ask(args.question, follow_up=True)


def print_json(value: object) -> None:
    """Print inspection data in a stable machine-readable shape."""
    print(json.dumps(value, ensure_ascii=False, indent=2))


def cmd_session(args: argparse.Namespace) -> int:
    """Inspect or clear the current shell session state."""
    if args.session_command == "path":
        paths = session_paths()
        if args.json:
            print_json(paths)
        else:
            print(paths["session"])
        return 0
    if args.session_command == "list":
        sessions = known_sessions()
        if args.json:
            print_json(sessions)
        else:
            for session in sessions:
                print(f"{session['session_id']}\t{session['path']}")
        return 0
    if args.session_command == "clear":
        removed = clear_current_session()
        if args.json:
            print_json({"removed": removed})
        else:
            if removed:
                for path in removed:
                    print(f"removed {path}")
            else:
                print("session already clear")
        return 0

    snapshot = current_session_snapshot()
    if args.json:
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


def cmd_failure_record(args: argparse.Namespace) -> int:
    """Record a failed shell command for later repair."""
    record_failure(args.command, args.status, args.cwd)
    return 0


def cmd_fix(args: argparse.Namespace) -> int:
    """Suggest fixes for the last recorded failed shell command."""
    command = select_fix()
    if command:
        append_event({"type": "fix_selected", "command": command})
        print(command)
    return 0


def cmd_previous_fix(args: argparse.Namespace) -> int:
    """Reopen previous repair candidates."""
    command = select_previous_fix()
    if command:
        append_event({"type": "fix_selected", "command": command})
        print(command)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse the shell-agnostic Sigil CLI surface."""
    parser = argparse.ArgumentParser(prog="sigil")
    sub = parser.add_subparsers(dest="command_name", required=True)

    command = sub.add_parser("command")
    command.add_argument("prompt")
    command.add_argument("--select", action="store_true")
    command.add_argument("--json", action="store_true")
    command.set_defaults(func=cmd_command)

    previous_command = sub.add_parser("previous-command")
    previous_command.add_argument("--select", action="store_true")
    previous_command.add_argument("--json", action="store_true")
    previous_command.set_defaults(func=cmd_previous_command)

    question = sub.add_parser("question")
    question.add_argument("question")
    question.set_defaults(func=cmd_question)

    follow_up = sub.add_parser("follow-up")
    follow_up.add_argument("question")
    follow_up.set_defaults(func=cmd_follow_up)

    stream_pi = sub.add_parser("stream-pi-json")
    stream_pi.set_defaults(func=lambda _args: stream_events())

    session = sub.add_parser("session")
    session.add_argument("session_command", nargs="?", choices=("show", "path", "list", "clear"), default="show")
    session.add_argument("--json", action="store_true")
    session.set_defaults(func=cmd_session)

    failure = sub.add_parser("failure")
    failure_sub = failure.add_subparsers(dest="failure_command", required=True)

    failure_record = failure_sub.add_parser("record")
    failure_record.add_argument("--status", type=int, required=True)
    failure_record.add_argument("--cwd")
    failure_record.add_argument("command")
    failure_record.set_defaults(func=cmd_failure_record)

    fix = sub.add_parser("fix")
    fix.set_defaults(func=cmd_fix)

    previous_fix = sub.add_parser("previous-fix")
    previous_fix.set_defaults(func=cmd_previous_fix)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
