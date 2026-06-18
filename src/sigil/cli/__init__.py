"""Command-line boundary between shell bindings and the Sigil runtime.

The CLI is intentionally boring: shell integrations should call these commands
instead of reimplementing model calls, selectors, rendering, or state handling.

Each command lives in a sibling module and registers on the shared `cli` group
via decorators; the group imports each module on first use.
"""

from ._base import cli, main

__all__ = ["cli", "main"]
