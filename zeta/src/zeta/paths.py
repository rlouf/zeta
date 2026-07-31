"""Zeta paths.

Two roots, deliberately separate. The user configuration root is fixed at
`~/.zeta`. Project runtime state is discovered by walking up from a working
directory, much as Git discovers `.git`.

This module imports nothing from Zeta, so any layer may use it.
"""

from __future__ import annotations

import os
from pathlib import Path


def zeta_state_dir() -> Path:
    """Return the user-level configuration directory."""

    return Path.home() / ".zeta"


def resolve_state_dir(
    state_dir: Path | None = None,
    *,
    start: Path | None = None,
) -> Path:
    """Discover project runtime state without creating it.

    The home marker is ignored while discovering from one of its descendants
    because ``~/.zeta`` also owns user-level configuration.
    """
    if state_dir is not None:
        return state_dir.expanduser().resolve()
    env_state_dir = os.environ.get("ZETA_STATE_DIR")
    if env_state_dir:
        return Path(env_state_dir).expanduser().resolve()

    discovery_start = (start or Path.cwd()).expanduser().resolve()
    home = Path.home().expanduser().resolve()
    search_roots = (discovery_start, *discovery_start.parents)
    if discovery_start != home and discovery_start.is_relative_to(home):
        search_roots = search_roots[: search_roots.index(home)]
    for root in search_roots:
        marker = root / ".zeta"
        if marker.is_dir():
            return marker
        if marker.exists() or marker.is_symlink():
            raise NotADirectoryError(
                f"runtime state marker is not a directory: {marker}"
            )
    return discovery_start / ".zeta"
