"""User-level configuration paths.

Zeta separates the user configuration root from project runtime state. This
module holds the user root only. Project state discovery lives in the journal,
because it walks up from a working directory.

This module imports nothing from Zeta, so any layer may use it.
"""

from __future__ import annotations

from pathlib import Path


def zeta_state_dir() -> Path:
    """Return the user-level configuration directory."""

    return Path.home() / ".zeta"
