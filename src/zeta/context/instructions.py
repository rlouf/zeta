"""Project instruction discovery for Zeta prompts."""

import os
from pathlib import Path

from zeta.run.context import zeta_state_dir

MAX_INSTRUCTION_FILE_CHARS = 24_000
MAX_INSTRUCTION_TOTAL_CHARS = 48_000


def load_project_instructions(cwd: str | Path | None = None) -> str:
    """Load project instruction files from parent directories, global to local.

    Sizes are capped per file and overall so one runaway AGENTS.md cannot
    swallow the prompt budget. On total overflow the broadest sections are
    dropped first: local instructions override broader ones, so they are
    the last to go.
    """
    current = Path(cwd or os.getcwd()).resolve()
    sections: list[str] = []
    seen: set[Path] = set()
    for directory in instruction_directories(current):
        path = agents_file(directory)
        if path is None:
            continue
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        section = project_instruction_section(path)
        if section:
            sections.append(section)
    while sum(len(section) for section in sections) > MAX_INSTRUCTION_TOTAL_CHARS:
        sections.pop(0)
    return "\n\n".join(sections)


def project_instruction_section(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    text = text.strip()
    if not text:
        return None
    if len(text) > MAX_INSTRUCTION_FILE_CHARS:
        text = text[:MAX_INSTRUCTION_FILE_CHARS].rstrip() + "\n... truncated ..."
    return f"Project instructions from {path}:\n{text}"


def instruction_directories(current: Path) -> list[Path]:
    return [zeta_state_dir(), *reversed(current.parents), current]


def agents_file(directory: Path) -> Path | None:
    """Return the exact-case AGENTS.md in a directory, if present.

    Matching against directory entries keeps lookups exact-case on
    case-insensitive filesystems.
    """
    try:
        for entry in directory.iterdir():
            if entry.name == "AGENTS.md" and entry.is_file():
                return entry
    except OSError:
        return None
    return None
