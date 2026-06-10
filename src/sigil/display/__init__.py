"""Terminal display for Sigil routes: rendering machinery and summaries."""

from __future__ import annotations

from ..tty import MUTED, RESET
from .render import (
    ContextUsageFooter,
    RichStreamRenderer,
    StreamRenderer,
    TerminalStreamRenderer,
    ThinkingStatus,
    TraceAwareStreamRenderer,
    TraceRenderState,
    context_usage_line,
    create_stream_renderer,
    estimated_tool_result_context_tokens,
    is_interactive,
    muted,
    render_tool_result_summary,
    render_tool_start,
    should_color,
    thinking_status_factory,
)
from .summarize import (
    render_handoff_lines,
    shell_result_summary,
    summarize,
    text_content,
    tool_result_summary,
    truncate,
)

__all__ = [
    "MUTED",
    "RESET",
    "ContextUsageFooter",
    "RichStreamRenderer",
    "StreamRenderer",
    "TerminalStreamRenderer",
    "ThinkingStatus",
    "TraceAwareStreamRenderer",
    "TraceRenderState",
    "context_usage_line",
    "create_stream_renderer",
    "estimated_tool_result_context_tokens",
    "is_interactive",
    "muted",
    "render_handoff_lines",
    "render_tool_result_summary",
    "render_tool_start",
    "shell_result_summary",
    "should_color",
    "summarize",
    "text_content",
    "thinking_status_factory",
    "tool_result_summary",
    "truncate",
]
