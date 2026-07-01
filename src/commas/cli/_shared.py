"""Shared CLI helpers: stdin handling, $EDITOR composition, and JSON output."""

import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

import click


def compose_in_editor(*, hint: str) -> str | None:
    """Compose text in $VISUAL/$EDITOR; return None when the result is empty.

    The buffer opens empty above ``hint``; lines starting with ``#`` are
    stripped from the saved text, so the hint must be commented.
    """
    handle, name = tempfile.mkstemp(prefix="commas-edit-", suffix=".txt")
    buffer_path = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as buffer:
            buffer.write(f"\n{hint}\n")
        run_editor(buffer_path)
        text = buffer_path.read_text(encoding="utf-8")
    finally:
        buffer_path.unlink(missing_ok=True)
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    return "\n".join(lines).strip() or None


def run_editor(path: Path) -> None:
    """Run $VISUAL/$EDITOR on a file, reading the keyboard from the tty.

    Piped stdin must stay available as command input, so the editor gets
    /dev/tty instead whenever stdin is not a terminal.
    """
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    keyboard = None
    if not sys.stdin.isatty():
        try:
            keyboard = open("/dev/tty", encoding="utf-8")
        except OSError:
            keyboard = None
    try:
        completed = subprocess.run([*shlex.split(editor), str(path)], stdin=keyboard)
    finally:
        if keyboard is not None:
            keyboard.close()
    if completed.returncode != 0:
        raise click.ClickException(f"editor exited with status {completed.returncode}")


def piped_stdin_text() -> str | None:
    """Return piped stdin, treating empty test harness stdin as absent."""
    if sys.stdin.isatty():
        return None
    text = sys.stdin.read()
    return text if text else None


def question_with_stdin(question: str, stdin_text: str) -> str:
    """Attach piped input to a question prompt."""
    if question:
        return f"{question}\n\nPiped input:\n{stdin_text}"
    return f"Piped input:\n{stdin_text}"


def pretty_print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))
