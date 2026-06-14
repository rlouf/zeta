"""Project instruction discovery for Zeta prompts."""

from __future__ import annotations

import os
from pathlib import Path

MAX_CONTEXT_FILE_CHARS = 24_000
MAX_CONTEXT_TOTAL_CHARS = 48_000


def _context_directories(current: Path) -> list[Path]:
    global_directory = Path.home() / ".zeta"
    return [global_directory, *reversed(current.parents), current]


def _agents_file(directory: Path) -> Path | None:
    """Return the exact-case AGENTS.md in a directory, if present.

    Matching against directory entries (rather than probing the path) keeps
    lookups exact-case on case-insensitive filesystems.
    """
    try:
        for entry in directory.iterdir():
            if entry.name == "AGENTS.md" and entry.is_file():
                return entry
    except OSError:
        return None
    return None


def load_project_context(cwd: str | Path | None = None) -> str:
    """Load project instruction files from parent directories, global to local.

    Sizes are capped per file and overall so one runaway AGENTS.md cannot
    swallow the prompt budget. On total overflow the broadest sections are
    dropped first: local instructions override broader ones, so they are
    the last to go.
    """
    current = Path(cwd or os.getcwd()).resolve()
    directories = _context_directories(current)
    sections: list[str] = []
    seen: set[Path] = set()
    for directory in directories:
        path = _agents_file(directory)
        if path is None:
            continue
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        text = text.strip()
        if not text:
            continue
        if len(text) > MAX_CONTEXT_FILE_CHARS:
            text = text[:MAX_CONTEXT_FILE_CHARS].rstrip() + "\n... truncated ..."
        sections.append(f"Project context from {path}:\n{text}")
    while sum(len(section) for section in sections) > MAX_CONTEXT_TOTAL_CHARS:
        sections.pop(0)
    return "\n\n".join(sections)
