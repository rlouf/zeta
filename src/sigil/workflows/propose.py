"""Reviewed agent-step workflow for the `,,` glyph."""

from collections.abc import Iterable
from pathlib import Path
from typing import TextIO

from .step import HandoffOutput, step


def propose(
    objective: str,
    *,
    system: str | None = None,
    stdin_text: str = "",
    max_steps: int | None = None,
    allowed_tools: Iterable[str] | None = None,
    handoff_path: str | Path | None = None,
    handoff_output: HandoffOutput = "detail",
    trace_output: TextIO | None = None,
) -> int:
    """Run a reviewed step that stages mutating work as shell handoffs."""
    return step(
        objective,
        workflow="propose",
        system=system,
        stdin_text=stdin_text,
        max_steps=max_steps,
        allowed_tools=allowed_tools,
        handoff_path=handoff_path,
        handoff_output=handoff_output,
        trace_output=trace_output,
    )
