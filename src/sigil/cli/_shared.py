"""Shared CLI helpers: stdin handling, confirmation, and JSON output."""

from __future__ import annotations

import json
import sys

from ..tty import confirm_on_tty

MAX_CONFIRM_STDIN_CHARS = 4000
MAX_CONFIRM_STDIN_LINES = 80


def piped_stdin_text() -> str | None:
    """Return piped stdin, treating empty test harness stdin as absent."""
    if sys.stdin.isatty():
        return None
    text = sys.stdin.read()
    return text if text else None


def question_with_stdin(question: str, stdin_text: str) -> str:
    """Attach piped input to a web-authorized question prompt."""
    if question:
        return f"{question}\n\nPiped input:\n{stdin_text}"
    return f"Piped input:\n{stdin_text}"


def should_confirm_piped_input(invocation: object) -> bool:
    """Return whether an operator needs piped-input confirmation."""
    return (
        getattr(invocation, "base", None) in {",", "@"}
        and getattr(invocation, "mode", None) == "pipeline"
        and bool(getattr(invocation, "stdin", ""))
    )


def should_run_act_operator(invocation: object) -> bool:
    """Return whether this invocation targets the implemented act runner."""
    return getattr(invocation, "base", None) == "," and getattr(
        invocation, "depth", 0
    ) in {2, 3}


def stdin_preview(text: str) -> str:
    """Return a bounded preview of piped stdin for confirmation prompts."""
    lines = text.splitlines()
    preview_lines = lines[:MAX_CONFIRM_STDIN_LINES]
    preview = "\n".join(preview_lines)
    truncated = len(lines) > MAX_CONFIRM_STDIN_LINES
    if len(preview) > MAX_CONFIRM_STDIN_CHARS:
        preview = preview[:MAX_CONFIRM_STDIN_CHARS]
        truncated = True
    if truncated:
        preview += "\n..."
    return preview


def confirm_piped_input(stdin_text: str) -> bool:
    """Show a bounded stdin preview and ask whether it may influence a command."""
    print("Sigil received piped input:", file=sys.stderr)
    print("", file=sys.stderr)
    print(stdin_preview(stdin_text), file=sys.stderr)
    print("", file=sys.stderr)
    return confirm_on_tty("Use this input? [y/N] ")


def pretty_print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def print_json_line(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False))
