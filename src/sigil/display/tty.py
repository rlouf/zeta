"""Terminal color constants and predicates shared by CLI rendering."""

from __future__ import annotations

import os
from typing import TextIO

MUTED = "\033[38;2;110;106;134m"
LOVE = "\033[38;2;235;111;146m"
IRIS = "\033[38;2;196;167;231m"
ITALIC = "\033[3m"
RESET = "\033[0m"


def is_interactive(stream: TextIO) -> bool:
    """Return whether a stream is attached to an interactive terminal."""
    return bool(getattr(stream, "isatty", lambda: False)())


def should_color(stream: TextIO) -> bool:
    """Return whether terminal color should be emitted to a stream."""
    return is_interactive(stream) and "NO_COLOR" not in os.environ


def muted(text: str, *, enabled: bool) -> str:
    """Apply muted terminal styling when color is enabled."""
    if not enabled:
        return text
    return f"{MUTED}{text}{RESET}"


def iris_italic(text: str, *, enabled: bool) -> str:
    """Apply italic Rose Pine iris styling when color is enabled."""
    if not enabled:
        return text
    return f"{ITALIC}{IRIS}{text}{RESET}"
