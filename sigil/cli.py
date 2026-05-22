from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .commands import generate, previous, select
from .pi_stream import stream_events
from .question import ask
from .state import append_event


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def cmd_command(args: argparse.Namespace) -> int:
    candidates = generate(args.prompt)
    if args.json:
        print(json.dumps({"prompt": args.prompt, "commands": candidates}, ensure_ascii=False))
        return 0
    if args.select:
        command = select(args.prompt, candidates)
        if command:
            append_event({"type": "command_selected", "command": command})
            print(command)
        return 0
    for item in candidates:
        print(item["command"])
    return 0


def cmd_previous_command(args: argparse.Namespace) -> int:
    prompt, candidates = previous()
    if args.json:
        print(json.dumps({"prompt": prompt, "commands": candidates}, ensure_ascii=False))
        return 0
    command = select(prompt, candidates) if args.select else candidates[0]["command"]
    if command:
        append_event({"type": "command_selected", "command": command})
        print(command)
    return 0


def cmd_question(args: argparse.Namespace) -> int:
    return ask(args.question, str(project_root() / "bin" / "stream-pi-json"))


def main(argv: list[str] | None = None) -> int:
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

    stream_pi = sub.add_parser("stream-pi-json")
    stream_pi.set_defaults(func=lambda _args: stream_events())

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

