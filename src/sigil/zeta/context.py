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
    swallow the prompt budget.
    """
    current = Path(cwd or os.getcwd()).resolve()
    directories = _context_directories(current)
    sections: list[str] = []
    seen: set[Path] = set()
    total_chars = 0
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
        section = f"Project context from {path}:\n{text}"
        if total_chars + len(section) > MAX_CONTEXT_TOTAL_CHARS:
            break
        total_chars += len(section)
        sections.append(section)
    return "\n\n".join(sections)
