"""Command-line boundary between shell bindings and the Sigil runtime.

The CLI is intentionally boring: shell integrations should call these commands
instead of reimplementing model calls, selectors, rendering, or state handling.

Each command lives in a sibling module and registers on the shared `cli` group
via decorators; importing those modules here runs the decorators.
"""

from __future__ import annotations

from ._base import cli, main
from . import (  # noqa: F401  (imported for command registration side effects)
    act,
    ask,
    command,
    display,
    events,
    install,
    run,
    session,
    transcript,
    zeta_step,
)

__all__ = ["cli", "main"]
