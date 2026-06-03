"""Internal transcript commands for shell bindings."""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

import click

from ._base import cli
from .. import handoff


@cli.group("transcript", hidden=True)
def cmd_transcript() -> None:
    """Record and reconcile Sigil shell transcript events."""


@cmd_transcript.command("shell-result")
def transcript_shell_result() -> int:
    """Append the shell handoff result used for empty Zeta continuation."""
    print_json(handoff.append_shell_result())
    return 0


@cmd_transcript.command("shell-turn")
def transcript_shell_turn() -> int:
    """Record one shell command executed after a Zeta handoff."""
    try:
        turn = read_json_stdin(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        raise click.BadParameter(str(exc), param_hint="stdin") from exc
    print_json(handoff.append_shell_turn(turn))
    return 0


def read_json_stdin(stdin: TextIO) -> dict[str, Any]:
    raw = stdin.read()
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("expected JSON object")
    return data


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))
