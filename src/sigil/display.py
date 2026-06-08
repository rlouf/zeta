"""Small terminal rendering helpers for Sigil routes."""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Protocol, TextIO, cast

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.padding import Padding

from .protocols import (
    SHELL_HANDOFF_OUTCOME_CANCELLED,
    SHELL_HANDOFF_OUTCOME_EXECUTED,
    SHELL_HANDOFF_OUTCOME_NO_PENDING,
)
from .tty import MUTED, RESET

TRACE_LABEL_WIDTH = 5
THINKING_STATUS_INTERVAL_SECONDS = 1.0
RICH_STREAM_REFRESH_SECONDS = 0.125
RICH_STREAM_LEFT_PADDING = 2
THINKING_STATUS_LEFT_PADDING = RICH_STREAM_LEFT_PADDING


class StreamRenderer(Protocol):
    """Render visible assistant text deltas."""

    def content_delta(self, text: str) -> None:
        """Handle one visible assistant text delta."""
        ...

    def ensure_trace_boundary(self) -> None:
        """Finalize the current assistant block before trace output."""
        ...

    def finish(self) -> None:
        """Finalize the current assistant block."""
        ...


class TraceRenderState:
    """Track whether trace output already separated the next text block."""

    def __init__(self) -> None:
        self.text_separator_pending = False
        self.text_separator_rendered = False

    def mark_trace_finished(self) -> None:
        self.text_separator_pending = True

    def clear_pending_separator(self) -> None:
        self.text_separator_pending = False

    def render_text_separator(self, output: TextIO) -> bool:
        if not self.text_separator_pending:
            return False
        print(file=output)
        self.text_separator_pending = False
        self.text_separator_rendered = True
        return True


def create_stream_renderer(
    output: TextIO,
    *,
    json_output: bool = False,
) -> StreamRenderer | None:
    """Return the renderer Sigil should use for human assistant text."""
    if json_output:
        return None
    if is_interactive(output):
        return RichStreamRenderer(output)
    return TerminalStreamRenderer(output)


class TerminalStreamRenderer:
    """Render visible assistant text deltas to a terminal stream."""

    def __init__(self, output: TextIO) -> None:
        self.output = output
        self.wrote_text = False
        self.ends_with_newline = True

    def content_delta(self, text: str) -> None:
        if not text:
            return
        print(text, file=self.output, end="", flush=True)
        self.wrote_text = True
        self.ends_with_newline = text.endswith("\n")

    def ensure_trace_boundary(self) -> None:
        if not self.wrote_text or self.ends_with_newline:
            return
        print(file=self.output, flush=True)
        self.ends_with_newline = True

    def finish(self) -> None:
        self.ensure_trace_boundary()


class TraceAwareStreamRenderer:
    """Add trace/text spacing policy around a concrete stream renderer."""

    def __init__(
        self,
        renderer: StreamRenderer,
        trace_state: TraceRenderState,
        output: TextIO,
    ) -> None:
        self.renderer = renderer
        self.trace_state = trace_state
        self.output = output
        self.text_active = False

    def content_delta(self, text: str) -> None:
        if not text:
            return
        if not self.text_active:
            if not self.trace_state.render_text_separator(self.output):
                print(file=self.output)
            self.text_active = True
        self.renderer.content_delta(text)

    def ensure_trace_boundary(self) -> None:
        self.renderer.ensure_trace_boundary()
        if self.text_active:
            print(file=self.output)
            self.text_active = False
        self.trace_state.clear_pending_separator()

    def finish(self) -> None:
        self.renderer.finish()
        if self.text_active:
            print(file=self.output)
            self.text_active = False


class RichStreamRenderer:
    """Render streaming assistant Markdown in an interactive terminal."""

    def __init__(
        self,
        output: TextIO,
        *,
        width: int | None = None,
        refresh_interval: float = RICH_STREAM_REFRESH_SECONDS,
        left_padding: int = RICH_STREAM_LEFT_PADDING,
        clock: Callable[[], float] = time.monotonic,
        console: Console | None = None,
    ) -> None:
        self.output = output
        self.console = console or Console(
            file=output,
            force_terminal=True,
            color_system="auto" if should_color(output) else None,
            width=width,
            highlight=False,
        )
        self.refresh_interval = refresh_interval
        self.left_padding = left_padding
        self.clock = clock
        self.live: Live | None = None
        self.buffer: list[str] = []
        self.wrote_text = False
        self.last_refresh = 0.0

    def content_delta(self, text: str) -> None:
        if not text:
            return
        self.buffer.append(text)
        self.wrote_text = True
        now = self.clock()
        if self.live is None:
            self.start_live(now)
            return
        if now - self.last_refresh >= self.refresh_interval:
            self.refresh(now)

    def ensure_trace_boundary(self) -> None:
        self.finalize_block(clear=True)

    def finish(self) -> None:
        self.finalize_block(clear=True)

    def start_live(self, now: float) -> None:
        self.live = Live(
            self.renderable(),
            console=self.console,
            auto_refresh=False,
            transient=False,
            redirect_stdout=False,
            redirect_stderr=False,
        )
        self.live.start(refresh=True)
        self.last_refresh = now

    def refresh(self, now: float | None = None) -> None:
        if self.live is None:
            return
        self.live.update(self.renderable(), refresh=True)
        self.last_refresh = self.clock() if now is None else now

    def finalize_block(self, *, clear: bool) -> None:
        if not self.wrote_text:
            return
        self.refresh()
        if self.live is not None:
            self.live.stop()
            self.live = None
        if clear:
            self.buffer.clear()
            self.wrote_text = False
            self.last_refresh = 0.0

    def renderable(self) -> Padding:
        return Padding(Markdown("".join(self.buffer)), (0, 0, 0, self.left_padding))


def render_tool_start(
    name: str,
    params: dict[str, Any],
    *,
    output: TextIO,
    newline: bool = True,
) -> None:
    """Print a visible tool-start line using the same shape as the stream renderer.

    Pass ``newline=False`` to leave the line open so a result summary can be
    appended onto it; the caller is then responsible for closing the line.
    """
    detail = summarize(name, params)
    status = f"❯ {name:<{TRACE_LABEL_WIDTH}}  {detail}" if detail else f"❯ {name}"
    end = "\n" if newline else ""
    print(muted(status, enabled=should_color(output)), file=output, flush=True, end=end)


def render_tool_result_summary(
    name: str,
    result: dict[str, Any],
    *,
    output: TextIO,
    mark_text_separator: TraceRenderState | None = None,
) -> None:
    """Append a result summary onto an open tool-start line and close it."""
    lines = tool_result_summary(name, result)
    if not lines:
        print(file=output)
        if mark_text_separator is not None:
            mark_text_separator.mark_trace_finished()
        return
    if len(lines) == 1:
        print(f"  ({lines[0]})", file=output)
    else:
        print(file=output)
        for line in lines:
            print(f"  {line}", file=output)
    if mark_text_separator is not None:
        mark_text_separator.mark_trace_finished()


def render_context_usage(
    telemetry: dict[str, Any] | None,
    *,
    output: TextIO,
    render_state: ContextUsageRenderState | None = None,
) -> bool:
    line = context_usage_line(telemetry)
    if not line:
        return False
    if render_state is not None and not render_state.should_render(line):
        return False
    print(muted(line, enabled=should_color(output)), file=output, flush=True)
    return True


class ContextUsageRenderState:
    """Avoid repeated context footer lines unless visible output moved the footer."""

    def __init__(self) -> None:
        self.last_line = ""
        self.output_since_render = True

    def mark_output(self) -> None:
        self.output_since_render = True

    def should_render(self, line: str) -> bool:
        if line == self.last_line and not self.output_since_render:
            return False
        self.last_line = line
        self.output_since_render = False
        return True


def context_usage_line(telemetry: dict[str, Any] | None) -> str:
    if not isinstance(telemetry, dict):
        return ""
    usage = telemetry.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    prompt_tokens = usage_token_count(usage.get("prompt_tokens"))
    completion_tokens = usage_token_count(usage.get("completion_tokens"))
    context_tokens = current_context_token_estimate(
        prompt_tokens,
        completion_tokens,
    )
    model_context_tokens = usage_token_count(telemetry.get("model_context_tokens"))
    if context_tokens is None and model_context_tokens is None:
        return ""
    context_text = (
        f"≈ {format_token_count(context_tokens)}"
        if context_tokens is not None
        else "unavailable"
    )
    if model_context_tokens is None:
        return f"◌ context  {context_text} tokens"
    return (
        f"◌ context  {context_text} / {format_token_count(model_context_tokens)} tokens"
    )


def current_context_token_estimate(
    prompt_tokens: int | None,
    completion_tokens: int | None,
) -> int | None:
    if prompt_tokens is None:
        return None
    if completion_tokens is None:
        return prompt_tokens
    return prompt_tokens + completion_tokens


def usage_token_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0:
        return None
    return value


def format_token_count(value: int) -> str:
    return f"{value:,}"


class ThinkingStatus:
    """Render an ephemeral thinking timer while a blocking model request runs."""

    def __init__(
        self,
        output: TextIO,
        *,
        enabled: bool | None = None,
        interval: float = THINKING_STATUS_INTERVAL_SECONDS,
        left_padding: int = THINKING_STATUS_LEFT_PADDING,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.output = output
        self.enabled = is_interactive(output) if enabled is None else enabled
        self.interval = interval
        self.left_padding = left_padding
        self.clock = clock
        self.started_at = 0.0
        self.last_seconds: int | None = None
        self.wrote_status = False
        self.wrote_separator = False
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()

    def __enter__(self) -> "ThinkingStatus":
        if not self.enabled:
            return self
        self.started_at = self.clock()
        self.refresh()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> bool:
        if not self.enabled:
            return False
        self.stop.set()
        if self.thread is not None:
            self.thread.join()
        self.clear()
        return False

    def run(self) -> None:
        while not self.stop.wait(self.interval):
            self.refresh()

    def refresh(self) -> None:
        seconds = max(int(self.clock() - self.started_at), 0)
        if seconds == self.last_seconds:
            return
        self.last_seconds = seconds
        prefix = "\r\x1b[2K"
        if not self.wrote_status:
            prefix = "\n\r\x1b[2K"
            self.wrote_separator = True
        self.wrote_status = True
        self.write(f"{prefix}{thinking_status_text(seconds, self.left_padding)}")

    def clear(self) -> None:
        if not self.wrote_status:
            return
        if self.wrote_separator:
            self.write("\r\x1b[2K\x1b[1A\r\x1b[2K")
        else:
            self.write("\r\x1b[2K")
        self.wrote_status = False
        self.wrote_separator = False

    def write(self, text: str) -> None:
        with self.lock:
            print(text, file=self.output, end="", flush=True)


def thinking_status_text(seconds: int, left_padding: int = 0) -> str:
    return f"{' ' * left_padding}> Thinking ({seconds}s...)"


def thinking_status_factory(
    output: TextIO,
    *,
    enabled: bool | None = None,
) -> Callable[[], ThinkingStatus]:
    return lambda: ThinkingStatus(output, enabled=enabled)


def render_handoff_lines(handoff: dict[str, Any]) -> list[str]:
    """Return user-facing lines for a staged tool handoff."""
    reason = str(handoff.get("reason") or "")
    command = str(handoff.get("command") or "")
    artifact = str(handoff.get("artifact") or "")
    lines = []
    if reason:
        lines.append(reason)
    if artifact:
        lines.append(f"artifact: {artifact}")
    if command:
        lines.append(command)
    return lines


def is_interactive(stream: TextIO) -> bool:
    """Return whether a stream is attached to an interactive terminal."""
    return bool(getattr(stream, "isatty", lambda: False)())


def should_color(stream: TextIO) -> bool:
    """Return whether terminal color should be emitted to a stream."""
    return is_interactive(stream) and "NO_COLOR" not in os.environ


def muted(text: str, *, enabled: bool) -> str:
    """Apply muted terminal styling when color is enabled."""
    if not enabled:
        return text
    return f"{MUTED}{text}{RESET}"


def summarize(tool: str, args: object) -> str:
    """Extract a short human-readable label for a tool call."""
    if not isinstance(args, dict):
        return ""
    tool_args = cast(dict[str, object], args)
    fields_by_tool = {
        "read": ("path", "file_path"),
        "edit": ("location", "path", "file_path"),
        "write": ("path", "file_path"),
        "bash": ("command", "cmd"),
        "grep": ("pattern", "query", "path", "glob"),
        "find": ("pattern", "query", "path", "glob"),
        "ls": ("pattern", "query", "path", "glob"),
    }
    for field in fields_by_tool.get(tool, ()):
        value = tool_args.get(field)
        if value:
            return str(value)
    return " ".join(
        f"{key}={value}"
        for key, value in tool_args.items()
        if isinstance(value, (str, int, float, bool))
    )


def tool_result_summary(name: str, result: dict[str, Any]) -> list[str]:
    """Return compact user-facing lines for a Zeta tool result."""
    handoff = result.get("handoff")
    if isinstance(handoff, dict):
        return handoff_summary(name, handoff)

    metadata = result.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    direct_summary = direct_tool_result_summary(name, metadata)
    if direct_summary:
        return direct_summary
    if result.get("ok") is False:
        return failed_tool_result_summary(result)
    text = text_content(result)
    if name == "read":
        return read_result_summary(text)
    if name == "ls":
        return ls_result_summary(text, metadata)
    if name == "grep":
        return grep_result_summary(text, metadata)
    if name == "edit" and metadata.get("mode") == "direct_replace":
        return edit_result_summary(metadata)
    if result.get("ok") is True:
        return ["ok"]
    return []


def failed_tool_result_summary(result: dict[str, Any]) -> list[str]:
    message = failed_tool_result_message(result)
    if message:
        return [truncate(message)]
    text = text_content(result).strip()
    if text:
        return [truncate(text.splitlines()[0])]
    return ["failed"]


def failed_tool_result_message(result: dict[str, Any]) -> str:
    error = result.get("error")
    if isinstance(error, dict):
        return format_tool_error(error)
    return str(result.get("message") or "").strip()


def format_tool_error(error: dict[str, Any]) -> str:
    code = str(error.get("code") or "").strip()
    message = str(error.get("message") or "").strip()
    return ": ".join(part for part in (code, message) if part)


def read_result_summary(text: str) -> list[str]:
    return [f"{count_lines(text)} lines"]


def ls_result_summary(text: str, metadata: dict[str, Any]) -> list[str]:
    entries = metadata.get("entries")
    if isinstance(entries, int):
        return [f"{entries} entries"]
    return [f"{count_lines(text)} entries"]


def grep_result_summary(text: str, metadata: dict[str, Any]) -> list[str]:
    match_count = metadata.get("matches")
    file_count = metadata.get("files")
    if isinstance(match_count, int):
        return [grep_metadata_summary(match_count, file_count, metadata)]
    matches = [line for line in text.splitlines() if line]
    files = {line.split(":", 1)[0] for line in matches if ":" in line}
    if files:
        return [f"{len(matches)} matches · {len(files)} files"]
    return [f"{len(matches)} matches"]


def grep_metadata_summary(
    matches: int,
    files: object,
    metadata: dict[str, Any],
) -> str:
    summary = f"{matches} matches"
    if isinstance(files, int) and files:
        summary += f" · {files} files"
    if metadata.get("truncated") is True:
        summary += " · truncated"
    return summary


def edit_result_summary(metadata: dict[str, Any]) -> list[str]:
    location = metadata.get("location")
    if isinstance(location, str) and location:
        return [f"applied · {location}"]
    return ["applied"]


def direct_tool_result_summary(name: str, metadata: dict[str, Any]) -> list[str]:
    """Return compact summaries for tools that ran directly."""
    if name == "bash" and metadata.get("mode") == "direct":
        status = metadata.get("status")
        if isinstance(status, int):
            if status == 0:
                return ["succeeded"]
            return [f"failed · exit {status}"]
        return ["executed"]
    if name == "write" and metadata.get("mode") == "direct":
        path = metadata.get("path")
        if isinstance(path, str) and path:
            return [f"wrote · {path}"]
        return ["wrote"]
    return []


def shell_result_summary(event: dict[str, Any]) -> list[str]:
    """Return compact user-facing lines for a shell handoff result event."""
    result = event.get("result")
    if not isinstance(result, dict):
        return []
    outcome = str(result.get("outcome") or "")
    if outcome == SHELL_HANDOFF_OUTCOME_EXECUTED:
        command = result.get("executed_command") or result.get("command") or ""
        status = result.get("status")
        turns = result.get("shell_turns")
        turn_count = len(turns) if isinstance(turns, list) else 0
        suffix = f" · {turn_count} shell turn" + ("" if turn_count == 1 else "s")
        return [
            "❯ shell  captured",
            f"  {truncate(command)}",
            f"  exit {status}{suffix}",
        ]
    if outcome == SHELL_HANDOFF_OUTCOME_CANCELLED:
        expected = result.get("expected_command") or ""
        actual = result.get("actual_command") or ""
        lines = [
            "❯ shell  changed" if actual else "❯ shell  cancelled",
            f"  expected: {truncate(expected)}",
        ]
        if actual:
            lines.append(f"  ran:      {truncate(actual)}")
        return lines
    if outcome == SHELL_HANDOFF_OUTCOME_NO_PENDING:
        return ["❯ shell  no handoff"]
    return []


def handoff_summary(name: str, handoff: dict[str, Any]) -> list[str]:
    """Return compact lines for a tool result that stages shell work."""
    artifact = str(handoff.get("artifact") or "")
    if name == "bash":
        return ["staged"]
    if name == "edit":
        return [f"staged patch · {artifact}" if artifact else "staged patch"]
    if name == "write":
        return [f"staged write · {artifact}" if artifact else "staged write"]
    if artifact:
        return [f"staged · {artifact}"]
    return ["staged"]


def text_content(value: dict[str, Any]) -> str:
    """Return joined text content from a tool result."""
    parts = value.get("content")
    if not isinstance(parts, list):
        return ""
    return "\n".join(
        str(part.get("text") or "")
        for part in parts
        if isinstance(part, dict) and part.get("type") == "text"
    )


def count_lines(text: str) -> int:
    """Return the display line count for a string."""
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def truncate(value: object, limit: int = 96) -> str:
    """Return a single display line bounded to a fixed width."""
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
