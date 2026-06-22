"""Stateful terminal display objects for active agent turns."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Protocol, TextIO

from rich.console import Console
from rich.constrain import Constrain
from rich.live import Live
from rich.markdown import Markdown
from rich.padding import Padding

from sigil.display.summarize import (
    short_trace_id,
    summarize,
    text_content,
    tool_result_summary,
)
from sigil.display.tty import iris_italic, is_interactive, muted, should_color
from zeta.capabilities.base import proposed_effect
from zeta.context.budget import estimated_tokens_for_text

THINKING_STATUS_INTERVAL_SECONDS = 1.0
RICH_STREAM_REFRESH_SECONDS = 0.125
RICH_STREAM_LEFT_PADDING = 2
THINKING_STATUS_LEFT_PADDING = RICH_STREAM_LEFT_PADDING
THINKING_TRACE_LINES = 6
CONTEXT_USAGE_BAR_WIDTH = 20
CONTEXT_USAGE_BAR_FILLED = "█"
CONTEXT_USAGE_BAR_EMPTY = "░"
PROGRESS_MODE_COMPACT = "compact"
PROGRESS_MODE_TRACE = "trace"
PROGRESS_MODE_QUIET = "quiet"
PROGRESS_MODES = frozenset(
    {PROGRESS_MODE_COMPACT, PROGRESS_MODE_TRACE, PROGRESS_MODE_QUIET}
)
SLOW_TOOL_START_NAMES = frozenset({"web_search"})
TERMINAL_DIGEST_EVENT_THRESHOLD = 6
TERMINAL_DIGEST_SECONDS_THRESHOLD = 10.0
TERMINAL_DIGEST_CHAPTER_LINES = 2

REASONING_PHASE_PATTERNS = (
    re.compile(
        r"\b(?:i am|i'm|i will|i'll|now i(?:'ll| will)?|next i(?:'ll| will)?)\s+([^.!?\n]{4,80})",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:need to|going to)\s+([^.!?\n]{4,80})", re.IGNORECASE),
)
REASONING_PHASE_PREFIXES = (
    "checking",
    "inspecting",
    "mapping",
    "understanding",
    "validating",
    "fixing",
)
GENERIC_REASONING_PHASES = frozenset(
    {
        "checking",
        "inspecting",
        "looking",
        "mapping",
        "thinking",
        "understanding",
        "validating",
        "working",
    }
)


@dataclass(frozen=True)
class ProgressEvent:
    """One lossy display event for terminal progress rendering."""

    kind: str
    phase: str
    subject: str
    line: str
    failed: bool = False
    exact: bool = False


class TerminalDigestRenderer:
    """Render terminal progress as compact lines and bounded chapters."""

    def __init__(
        self,
        output: TextIO,
        *,
        mode: str = PROGRESS_MODE_COMPACT,
        objective: str = "",
        clock: Callable[[], float] = time.monotonic,
        chapter_event_threshold: int = TERMINAL_DIGEST_EVENT_THRESHOLD,
        chapter_seconds_threshold: float = TERMINAL_DIGEST_SECONDS_THRESHOLD,
        max_chapter_lines: int = TERMINAL_DIGEST_CHAPTER_LINES,
    ) -> None:
        self.output = output
        self.mode = mode if mode in PROGRESS_MODES else PROGRESS_MODE_COMPACT
        self.objective = objective
        self.clock = clock
        self.started_at = clock()
        self.chapter_event_threshold = chapter_event_threshold
        self.chapter_seconds_threshold = chapter_seconds_threshold
        self.max_chapter_lines = max_chapter_lines
        self.current_phase = ""
        self.intent_phase = ""
        self.current_chapter_phase = ""
        self.current_chapter_lines = 0
        self.event_count = 0
        self.chapter_mode = False
        self.pending_args: dict[str, list[dict[str, Any]]] = {}
        self.events: list[ProgressEvent] = []
        self.files_touched: set[str] = set()
        self.commands_run: list[str] = []
        self.failures = 0
        self.artifacts: set[str] = set()
        self.last_action = ""

    def observe_tool_call(self, name: str, args: dict[str, Any]) -> None:
        self.pending_args.setdefault(name, []).append(dict(args))
        summary = summarize(name, args)
        if summary:
            self.last_action = summary
        event = progress_event_for_tool_call(name, args)
        if event is not None:
            self.observe_event(event)

    def observe_tool_result(self, name: str, result: dict[str, Any]) -> None:
        args = self.pop_args(name)
        event = progress_event_for_tool_result(name, result, args)
        if event is None:
            return
        self.observe_event(event)

    def observe_event(self, event: ProgressEvent) -> None:
        self.events.append(event)
        self.event_count += 1
        self.current_phase = self.intent_phase or event.phase
        self.last_action = event.subject
        if event.kind == "mutation" and event.subject:
            self.files_touched.add(event.subject)
        if event.kind in {"command", "failure"} and event.subject:
            self.commands_run.append(event.subject)
        if event.failed:
            self.failures += 1
        self.render_event(event)

    def pop_args(self, name: str) -> dict[str, Any]:
        args = self.pending_args.get(name)
        if not args:
            return {}
        value = args.pop(0)
        if not args:
            self.pending_args.pop(name, None)
        return value

    def render_event(self, event: ProgressEvent) -> None:
        if self.mode == PROGRESS_MODE_TRACE:
            return
        if self.mode == PROGRESS_MODE_QUIET:
            if event.failed or event.exact:
                print(event.line, file=self.output)
            return
        if self.should_enter_chapter_mode():
            self.chapter_mode = True
        if not self.chapter_mode:
            print(event.line, file=self.output)
            return
        phase = self.current_phase or event.phase
        if phase != self.current_chapter_phase:
            self.start_chapter(phase)
        if event.exact or self.current_chapter_lines < self.max_chapter_lines:
            print(f"  {event.line}", file=self.output)
            self.current_chapter_lines += 1

    def should_enter_chapter_mode(self) -> bool:
        if self.chapter_mode:
            return True
        if self.event_count > self.chapter_event_threshold:
            return True
        return self.clock() - self.started_at >= self.chapter_seconds_threshold

    def start_chapter(self, phase: str) -> None:
        if self.current_chapter_phase:
            print(file=self.output)
        else:
            print(file=self.output)
        self.current_chapter_phase = phase
        self.current_chapter_lines = 0
        print(
            f"[{format_elapsed(self.clock() - self.started_at)}] {phase}",
            file=self.output,
        )

    def status_detail(self) -> str:
        if not self.current_phase and self.event_count == 0:
            return ""
        parts = []
        if self.current_phase:
            parts.append(self.current_phase[:1].lower() + self.current_phase[1:])
        if self.event_count:
            parts.append(f"{self.event_count} events")
        if self.last_action:
            parts.append(f"last: {self.last_action}")
        return " · ".join(parts)

    def observe_reasoning_delta(self, text: str) -> None:
        phase = reasoning_phase(text)
        if phase:
            self.intent_phase = phase
            self.current_phase = phase

    def finalize(self, turn: dict[str, Any], context_bar: str = "") -> None:
        line = final_digest_line(turn, self, context_bar)
        if line:
            print(file=self.output)
            print(line, file=self.output)


def reasoning_phase(text: str) -> str:
    normalized = " ".join(text.strip().split())
    if not normalized:
        return ""
    for pattern in REASONING_PHASE_PATTERNS:
        match = pattern.search(normalized)
        if match is None:
            continue
        title = phase_title(match.group(1))
        return "" if is_generic_reasoning_phase(title) else title
    lower = normalized.lower()
    for prefix in REASONING_PHASE_PREFIXES:
        if lower.startswith(prefix):
            title = phase_title(normalized)
            return "" if is_generic_reasoning_phase(title) else title
    return ""


def is_generic_reasoning_phase(title: str) -> bool:
    return title.strip(" .:;-—").lower() in GENERIC_REASONING_PHASES


def phase_title(text: str) -> str:
    words = []
    for word in text.strip(" .:;-—").split():
        cleaned = word.strip(",;:()[]{}")
        if cleaned:
            words.append(cleaned)
        if len(words) >= 5:
            break
    if not words:
        return ""
    phrase = " ".join(words)
    return phrase[:1].upper() + phrase[1:]


def progress_event_for_tool_result(
    name: str,
    result: dict[str, Any],
    args: dict[str, Any] | None = None,
) -> ProgressEvent | None:
    args = {} if args is None else args
    failed = result.get("ok") is False
    summary = progress_result_summary(name, result)
    subject = progress_subject(name, result, args)
    if name == "read":
        return ProgressEvent(
            "read",
            "Mapping repo",
            subject,
            success_line("read", subject, summary, failed),
            failed=failed,
        )
    if name == "ls":
        return ProgressEvent(
            "list",
            "Mapping repo",
            subject,
            success_line("listed", subject, summary, failed),
            failed=failed,
        )
    if name in {"grep", "find"}:
        return ProgressEvent(
            "search",
            "Mapping repo",
            subject,
            success_line("searched", subject, summary, failed),
            failed=failed,
        )
    if name in {"write", "edit"}:
        return mutation_progress_event(name, result, subject, summary, failed)
    if name == "bash":
        return command_progress_event(subject, summary, failed)
    return ProgressEvent(
        "tool",
        "Working",
        subject or name,
        success_line(name, subject, summary, failed),
        failed=failed,
    )


def progress_event_for_tool_call(
    name: str,
    args: dict[str, Any],
) -> ProgressEvent | None:
    if name not in SLOW_TOOL_START_NAMES:
        return None
    subject = summarize(name, args)
    action = " ".join(part for part in (name, subject) if part).strip()
    return ProgressEvent(
        "tool_start",
        "Working",
        subject,
        f"→ {action}" if action else f"→ {name}",
        exact=True,
    )


def progress_result_summary(name: str, result: dict[str, Any]) -> str:
    lines = tool_result_summary(name, result)
    if (
        result.get("ok") is True
        and name in {"read", "ls", "grep", "find"}
        and lines
        in (
            ["0 lines"],
            ["0 entries"],
            ["0 matches"],
        )
    ):
        return "ok"
    return " · ".join(lines) or "ok"


def mutation_progress_event(
    name: str,
    result: dict[str, Any],
    subject: str,
    summary: str,
    failed: bool,
) -> ProgressEvent:
    staged = proposed_effect(result) is not None or isinstance(
        result.get("handoff"), dict
    )
    if failed:
        line = success_line(name, subject, summary, failed=True)
    elif subject:
        line = f"+ {subject}"
    else:
        line = f"+ {summary}"
    return ProgressEvent(
        "mutation",
        "Applying changes",
        subject,
        line,
        failed=failed,
        exact=True if staged or failed else False,
    )


def command_progress_event(
    subject: str,
    summary: str,
    failed: bool,
) -> ProgressEvent:
    return ProgressEvent(
        "failure" if failed else "command",
        "Validating",
        subject,
        success_line("", subject, summary, failed),
        failed=failed,
        exact=True,
    )


def success_line(verb: str, subject: str, summary: str, failed: bool) -> str:
    prefix = "✗" if failed else "✓"
    action = " ".join(part for part in (verb, subject) if part).strip()
    if not action:
        action = summary
        summary = ""
    suffix = f" · {summary}" if summary else ""
    return f"{prefix} {action}{suffix}"


def progress_subject(
    name: str,
    result: dict[str, Any],
    args: dict[str, Any],
) -> str:
    metadata = result.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    effect = proposed_effect(result) or {}
    handoff = result.get("handoff")
    handoff = handoff if isinstance(handoff, dict) else {}
    if name == "ls":
        return ls_progress_subject(metadata, args)
    if name == "web_search":
        return summarize(name, args)
    for source in (metadata, effect, handoff, args):
        for key in progress_subject_fields(name):
            value = source.get(key)
            if isinstance(value, str) and value:
                return text_content_path(value)
    return summarize(name, args)


def ls_progress_subject(metadata: dict[str, Any], args: dict[str, Any]) -> str:
    subject = ""
    for source in (metadata, args):
        value = source.get("path")
        if isinstance(value, str) and value:
            subject = text_content_path(value)
            break
    details = []
    recursive = metadata.get("recursive")
    if recursive is True or args.get("recursive") is True:
        details.append("recursive")
    suffix = f" ({', '.join(details)})" if details else ""
    return f"{subject}{suffix}" if subject else suffix.strip()


def progress_subject_fields(name: str) -> tuple[str, ...]:
    if name == "bash":
        return ("command", "cmd")
    if name in {"write", "edit", "read"}:
        return ("path", "location", "file_path", "artifact")
    return ("pattern", "query", "path", "glob")


def text_content_path(value: str) -> str:
    return value if "\n" not in value else value.splitlines()[0]


def progress_mode_from_env(env: Mapping[str, str] | None = None) -> str:
    values = os.environ if env is None else env
    mode = values.get("SIGIL_PROGRESS", PROGRESS_MODE_COMPACT).lower()
    if mode in PROGRESS_MODES:
        return mode
    return PROGRESS_MODE_COMPACT


def final_digest_line(
    turn: dict[str, Any],
    renderer: TerminalDigestRenderer,
    context_bar: str = "",
) -> str:
    cost = turn.get("cost")
    cost = cost if isinstance(cost, dict) else {}
    effects = turn.get("effects")
    effects = effects if isinstance(effects, list) else []
    files = effect_file_count(effects) or len(renderer.files_touched)
    commands = effect_command_count(effects) or len(renderer.commands_run)
    failures = renderer.failures
    parts = [f"Done in {format_duration(int(cost.get('wall_ms') or 0))}"]
    if files:
        parts.append(plural(files, "file"))
    if commands:
        parts.append(plural(commands, "command"))
    if failures:
        parts.append(plural(failures, "failure"))
    turn_id = str(turn.get("turn_id") or "")
    if turn_id:
        parts.append(f"log {short_trace_id(turn_id)}")
    if context_bar:
        parts.append(context_bar)
    return " · ".join(parts)


def effect_file_count(effects: list[Any]) -> int:
    paths = {
        effect.get("path")
        for effect in effects
        if isinstance(effect, dict)
        and str(effect.get("kind") or "").startswith("file_")
        and isinstance(effect.get("path"), str)
    }
    return len(paths)


def effect_command_count(effects: list[Any]) -> int:
    return sum(
        1
        for effect in effects
        if isinstance(effect, dict) and effect.get("kind") == "command"
    )


def plural(count: int, noun: str) -> str:
    suffix = "" if count == 1 else "s"
    return f"{count} {noun}{suffix}"


def format_duration(wall_ms: int) -> str:
    seconds = max(int(wall_ms / 1000), 0)
    if seconds < 60:
        return f"{seconds}s"
    minutes, remainder = divmod(seconds, 60)
    return f"{minutes}m{remainder:02d}s"


def format_elapsed(seconds: float) -> str:
    total = max(int(seconds), 0)
    minutes, remainder = divmod(total, 60)
    return f"{minutes:02d}:{remainder:02d}"


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


def create_stream_renderer(output: TextIO) -> StreamRenderer:
    """Return the renderer Sigil should use for human assistant text."""
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
        before_output: Callable[[], None] | None = None,
    ) -> None:
        self.renderer = renderer
        self.trace_state = trace_state
        self.output = output
        self.before_output = before_output
        self.text_active = False

    def reasoning_delta(self, text: str) -> None:
        del text

    def content_delta(self, text: str) -> None:
        if not text:
            return
        if self.before_output is not None:
            self.before_output()
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
        self.width = width
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
        total_width = self.width or self.console.width
        content_width = max(1, total_width - self.left_padding)
        return Padding(
            Constrain(Markdown("".join(self.buffer)), content_width),
            (0, 0, 0, self.left_padding),
        )


class ContextUsageFooter:
    """Render context usage as an ephemeral terminal footer during active turns."""

    def __init__(self, output: TextIO, *, enabled: bool | None = None) -> None:
        self.output = output
        self.enabled = is_interactive(output) if enabled is None else enabled
        self.last_line = ""
        self.active = False
        self.current_context_tokens: int | None = None
        self.model_context_tokens: int | None = None
        self.pending_context_tokens = 0

    def update(self, telemetry: dict[str, Any] | None) -> bool:
        """Refresh the active footer without leaving scrollback in TTY mode."""
        usage = provider_context_usage_tokens(telemetry)
        if usage is not None:
            self.current_context_tokens, self.model_context_tokens = usage
            self.pending_context_tokens = 0
        line = context_usage_line(telemetry)
        return self.render_line(line)

    def update_for_tool_result(
        self,
        telemetry: dict[str, Any] | None,
        result: dict[str, Any],
    ) -> bool:
        """Refresh context usage after a tool result enters the next prompt."""
        usage = provider_context_usage_tokens(telemetry)
        if usage is not None:
            self.current_context_tokens, self.model_context_tokens = usage
            self.pending_context_tokens = 0
        self.pending_context_tokens += estimated_tool_result_context_tokens(result)
        estimated_line = self.estimated_context_usage_line()
        if estimated_line:
            return self.render_line(estimated_line)
        return self.render_line(context_usage_line(telemetry))

    def estimated_context_usage_line(self) -> str:
        if (
            self.current_context_tokens is None
            or self.model_context_tokens is None
            or self.pending_context_tokens <= 0
        ):
            return ""
        return context_usage_line(
            {
                "estimated_context_tokens": (
                    self.current_context_tokens + self.pending_context_tokens
                ),
                "model_context_tokens": self.model_context_tokens,
            }
        )

    def render_line(self, line: str) -> bool:
        if not line:
            return False
        if self.active and line == self.last_line:
            return False
        self.last_line = line
        if not self.enabled:
            return False
        self.write(f"\r\x1b[2K{muted(line, enabled=should_color(self.output))}")
        self.active = True
        return True

    def clear(self) -> None:
        """Remove the active terminal footer before printing normal output."""
        if not self.active:
            return
        self.write("\r\x1b[2K")
        self.active = False

    def current_line(self) -> str:
        return self.last_line

    def finalize(
        self,
        telemetry: dict[str, Any] | None = None,
        *,
        print_line: bool = True,
    ) -> bool:
        """Leave one final context line in scrollback, or just clear if print_line=False."""
        usage = provider_context_usage_tokens(telemetry)
        if usage is not None:
            self.current_context_tokens, self.model_context_tokens = usage
            self.pending_context_tokens = 0
        line = context_usage_line(telemetry) or self.last_line
        if line:
            self.last_line = line
        if not print_line:
            if self.active:
                self.write("\r\x1b[2K")
                self.active = False
            if not line:
                return False
            return True
        if not line:
            return False
        if self.enabled:
            prefix = "\r\x1b[2K" if self.active else ""
            self.write(f"{prefix}{muted(line, enabled=should_color(self.output))}\n")
            self.active = False
            return True
        print(
            muted(line, enabled=should_color(self.output)), file=self.output, flush=True
        )
        return True

    def write(self, text: str) -> None:
        print(text, file=self.output, end="", flush=True)


def context_bar_text(telemetry: dict[str, Any] | None) -> str:
    if not isinstance(telemetry, dict):
        return ""
    estimated_context_tokens = usage_token_count(
        telemetry.get("estimated_context_tokens")
    )
    if estimated_context_tokens is not None:
        model_context_tokens = usage_token_count(telemetry.get("model_context_tokens"))
        if model_context_tokens is None or model_context_tokens <= 0:
            return ""
        context_tokens = estimated_context_tokens
    else:
        usage = provider_context_usage_tokens(telemetry)
        if usage is None:
            return ""
        context_tokens, model_context_tokens = usage
    percent = context_usage_percent(context_tokens, model_context_tokens)
    bar = context_usage_bar(context_tokens, model_context_tokens)
    suffix = " est." if estimated_context_tokens is not None else ""
    return f"[{bar}] {percent}%{suffix}"


def context_usage_line(telemetry: dict[str, Any] | None) -> str:
    bar = context_bar_text(telemetry)
    return f"context  {bar}" if bar else ""


def provider_context_usage_tokens(
    telemetry: dict[str, Any] | None,
) -> tuple[int, int] | None:
    if not isinstance(telemetry, dict):
        return None
    usage = telemetry.get("usage")
    if not isinstance(usage, dict):
        return None
    prompt_tokens = usage_token_count(usage.get("prompt_tokens"))
    completion_tokens = usage_token_count(usage.get("completion_tokens"))
    context_tokens = current_context_token_estimate(
        prompt_tokens,
        completion_tokens,
        usage_token_count(usage.get("total_tokens")),
    )
    model_context_tokens = usage_token_count(telemetry.get("model_context_tokens"))
    if (
        context_tokens is None
        or model_context_tokens is None
        or model_context_tokens <= 0
    ):
        return None
    return context_tokens, model_context_tokens


def context_usage_percent(context_tokens: int, model_context_tokens: int) -> int:
    percent = round((context_tokens / model_context_tokens) * 100)
    return clamp(percent, 0, 100)


def context_usage_bar(context_tokens: int, model_context_tokens: int) -> str:
    progress = context_tokens / model_context_tokens
    filled_cells = clamp(
        round(progress * CONTEXT_USAGE_BAR_WIDTH),
        0,
        CONTEXT_USAGE_BAR_WIDTH,
    )
    empty_cells = CONTEXT_USAGE_BAR_WIDTH - filled_cells
    return (
        CONTEXT_USAGE_BAR_FILLED * filled_cells + CONTEXT_USAGE_BAR_EMPTY * empty_cells
    )


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))


def current_context_token_estimate(
    prompt_tokens: int | None,
    completion_tokens: int | None,
    total_tokens: int | None = None,
) -> int | None:
    if prompt_tokens is not None and completion_tokens is not None:
        return prompt_tokens + completion_tokens
    return total_tokens


def usage_token_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0:
        return None
    return value


def estimated_tool_result_context_tokens(result: dict[str, Any]) -> int:
    try:
        text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        text = text_content(result)
    return estimated_tokens_for_text(text)


class ThinkingStatus:
    """Render an ephemeral thinking timer while a blocking model request runs.

    When the model streams reasoning, the last few lines show as a muted
    tail above the timer and are erased with it. The full reasoning is
    recorded in the trace and rendered by `sigil session transcript`.
    """

    def __init__(
        self,
        output: TextIO,
        *,
        enabled: bool | None = None,
        interval: float = THINKING_STATUS_INTERVAL_SECONDS,
        left_padding: int = THINKING_STATUS_LEFT_PADDING,
        clock: Callable[[], float] = time.monotonic,
        before_start: Callable[[], None] | None = None,
        detail: Callable[[], str] | None = None,
        reasoning_observer: Callable[[str], None] | None = None,
        reasoning_lines: int = THINKING_TRACE_LINES,
        width: int | None = None,
    ) -> None:
        self.output = output
        self.enabled = is_interactive(output) if enabled is None else enabled
        self.interval = interval
        self.left_padding = left_padding
        self.clock = clock
        self.before_start = before_start
        self.detail = detail
        self.reasoning_observer = reasoning_observer
        self.reasoning_lines = reasoning_lines
        self.width = width
        self.trace_enabled = thinking_trace_enabled()
        self.reasoning_parts: list[str] = []
        self.reasoning_dirty = False
        self.reasoning_seen = False
        self.started_at = 0.0
        self.last_seconds: int | None = None
        self.wrote_status = False
        self.rendered_line_count = 0
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()

    def __enter__(self) -> ThinkingStatus:
        if not self.enabled:
            return self
        if self.before_start is not None:
            self.before_start()
        self.started_at = self.clock()
        self.refresh()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None = None,
        exc: BaseException | None = None,
        _traceback: TracebackType | None = None,
    ) -> bool:
        if not self.enabled:
            return False
        self.stop.set()
        if self.thread is not None:
            self.thread.join()
        self.clear()
        return False

    def reasoning_delta(self, text: str) -> None:
        """Stream one reasoning delta into the rolling tail."""
        if text and self.reasoning_observer is not None:
            self.reasoning_observer(text)
        if text:
            self.reasoning_seen = True
        if not text or not self.enabled or not self.trace_enabled:
            return
        with self.lock:
            self.reasoning_parts.append(text)
            self.reasoning_dirty = True

    def run(self) -> None:
        while not self.stop.wait(self.interval):
            self.refresh()

    def refresh(self) -> None:
        seconds = max(int(self.clock() - self.started_at), 0)
        if seconds == self.last_seconds and not self.reasoning_dirty:
            return
        self.last_seconds = seconds
        self.reasoning_dirty = False
        prefix = ""
        if self.wrote_status:
            prefix = clear_terminal_lines(self.rendered_line_count)
        prefix += "\n\r\x1b[2K"
        text = self.status_text(seconds)
        self.wrote_status = True
        self.rendered_line_count = text.count("\n") + 2
        self.write(f"{prefix}{text}")

    def status_text(self, seconds: int) -> str:
        color = should_color(self.output)
        lines: list[str] = []
        if self.detail is not None:
            detail = self.detail()
            if detail:
                lines.append(muted(f"{' ' * self.left_padding}{detail}", enabled=color))
        tail = self.reasoning_tail()
        for tail_line in tail:
            lines.append(iris_italic(tail_line, enabled=color))
        if tail:
            lines.append("")
        phase = "thinking" if self.reasoning_seen else "prefill"
        lines.append(
            muted(status_wait_text(phase, seconds, self.left_padding), enabled=color)
        )
        return "\n".join(lines)

    def reasoning_tail(self) -> list[str]:
        """Return the last reasoning lines, padded and width-truncated.

        Truncation rather than wrapping: a line that wraps would break the
        rendered-line accounting the eraser depends on. Width is counted in
        characters, an approximation for double-width glyphs.
        """
        with self.lock:
            text = "".join(self.reasoning_parts)
        if not text:
            return []
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return []
        width = self.width if self.width is not None else terminal_width(self.output)
        limit = max(width - 1, 10)
        tail = []
        for line in lines[-self.reasoning_lines :]:
            padded = f"{' ' * self.left_padding}{line}"
            if len(padded) > limit:
                padded = padded[: limit - 1] + "\u2026"
            tail.append(padded)
        return tail

    def clear(self) -> None:
        if not self.wrote_status:
            return
        self.write(clear_terminal_lines(self.rendered_line_count))
        self.wrote_status = False
        self.rendered_line_count = 0

    def write(self, text: str) -> None:
        with self.lock:
            print(text, file=self.output, end="", flush=True)


def status_wait_text(phase: str, seconds: int, left_padding: int = 0) -> str:
    return f"{' ' * left_padding}{phase} {seconds}s"


def thinking_trace_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return whether the live reasoning tail is enabled."""
    values = os.environ if env is None else env
    return values.get("SIGIL_THINKING_TRACE", "1").lower() not in {"0", "false"}


def terminal_width(output: TextIO) -> int:
    try:
        return os.get_terminal_size(output.fileno()).columns
    except (OSError, ValueError, AttributeError):
        return 80


def clear_terminal_lines(line_count: int) -> str:
    if line_count <= 1:
        return "\r\x1b[2K"
    clear_lines = ["\r\x1b[2K"]
    for _ in range(line_count - 1):
        clear_lines.append("\x1b[1A\r\x1b[2K")
    return "".join(clear_lines)


def thinking_status_factory(
    output: TextIO,
    *,
    enabled: bool | None = None,
    before_start: Callable[[], None] | None = None,
    detail: Callable[[], str] | None = None,
    reasoning_observer: Callable[[str], None] | None = None,
) -> Callable[[], ThinkingStatus]:
    return lambda: ThinkingStatus(
        output,
        enabled=enabled,
        before_start=before_start,
        detail=detail,
        reasoning_observer=reasoning_observer,
    )
