"""Small terminal rendering helpers for Zeta routes."""

from __future__ import annotations

from typing import Any, TextIO

from .stream import TRACE_LABEL_WIDTH, muted, should_color, summarize


def render_tool_start(name: str, params: dict[str, Any], *, output: TextIO) -> None:
    """Print a visible tool-start line using the same shape as the stream renderer."""
    detail = summarize(name, params)
    status = f"❯ {name:<{TRACE_LABEL_WIDTH}}  {detail}" if detail else f"❯ {name}"
    print(muted(status, enabled=should_color(output)), file=output, flush=True)
