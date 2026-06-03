"""Internal recording helpers.

Shell bindings do not register ambient recording commands in the v1 surface.
"""

from __future__ import annotations

from ..failure import record_failure
from ..session import record_turn


def cmd_record_failure(
    command: str,
    status: int,
    cwd: str | None,
    stdout_snippet: str,
    stderr_snippet: str,
) -> int:
    """Record a failed shell command for later answer/proposal context."""
    record_failure(command, status, cwd, stdout_snippet, stderr_snippet)
    return 0


def cmd_record_turn(
    command: str,
    status: int,
    cwd: str | None,
    stdout_snippet: str,
    stderr_snippet: str,
) -> int:
    """Record one shell turn; fans out to failure recording on non-zero exit."""
    record_turn(command, status, cwd, stdout_snippet, stderr_snippet)
    return 0
