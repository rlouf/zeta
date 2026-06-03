"""Internal display formatting commands for shell bindings."""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

import click

from ._base import cli
from .. import display as sigil_display


@cli.group("display", hidden=True)
def cmd_display() -> None:
    """Format internal Sigil display payloads."""


@cmd_display.command("tool-result")
@click.argument("name")
def display_tool_result(name: str) -> int:
    """Render a compact tool-result summary."""
    try:
        result = read_json_stdin(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        raise click.BadParameter(str(exc), param_hint="stdin") from exc
    for line in sigil_display.tool_result_summary(name, result):
        print(line)
    return 0


@cmd_display.command("shell-result")
def display_shell_result() -> int:
    """Render a compact shell-result summary."""
    try:
        event = read_json_stdin(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        raise click.BadParameter(str(exc), param_hint="stdin") from exc
    for line in sigil_display.shell_result_summary(event):
        print(line)
    return 0


def read_json_stdin(stdin: TextIO) -> dict[str, Any]:
    """Read a JSON object from stdin."""
    raw = stdin.read()
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("expected JSON object")
    return data
