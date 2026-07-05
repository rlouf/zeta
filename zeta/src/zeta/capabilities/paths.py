"""Base-directory resolution for file capability paths.

An agent may declare a `base_dir`. When set, file tools resolve relative paths
against it so agent prompts can use vault-relative paths (`inbox/`,
`relationships/prospects/`) instead of baked-in absolute paths. The active base
is carried in a context variable so it is task-local under concurrent runs, and
so it composes with the future out-of-process host (which will instead set a real
working directory, making `resolve_path` a no-op).
"""

from __future__ import annotations

import contextvars
from pathlib import Path

__all__ = ["resolve_path", "set_base_dir", "reset_base_dir"]

_base_dir: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "zeta_base_dir", default=None
)


def set_base_dir(base_dir: Path | None) -> contextvars.Token[Path | None]:
    """Set the active base directory for the current task; return a reset token."""
    return _base_dir.set(base_dir)


def reset_base_dir(token: contextvars.Token[Path | None]) -> None:
    """Restore the base directory to its value before the matching ``set``."""
    _base_dir.reset(token)


def resolve_path(path: str) -> Path:
    """Resolve a tool path against the active base directory.

    Absolute paths (including ``~``-expanded ones) pass through unchanged; a
    relative path is joined under the base when one is set.
    """
    resolved = Path(path).expanduser()
    base = _base_dir.get()
    if base is None or resolved.is_absolute():
        return resolved
    return base / resolved
