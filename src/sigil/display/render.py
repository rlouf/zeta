"""Terminal rendering machinery: stream renderers, footer, and status."""

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
from rich.panel import Panel
from rich.text import Text

from ..zeta.prompt.budget import estimated_tokens_for_text
from .summarize import short_trace_id, summarize, text_content, tool_result_summary
from .tty import iris_italic, is_interactive, muted, should_color

TRACE_LABEL_WIDTH = 5
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
TERMINAL_DIGEST_EVENT_THRESHOLD = 6
TERMINAL_DIGEST_SECONDS_THRESHOLD = 10.0
TERMINAL_DIGEST_CHAPTER_LINES = 2
NARRATOR_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["phase", "summary_lines"],
    "properties": {
        "phase": {"type": "string", "maxLength": 48},
        "summary_lines": {
            "type": "array",
            "maxItems": 2,
            "items": {"type": "string", "maxLength": 96},
        },
    },
}

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


@dataclass(frozen=True)
class ProgressEvent:
    """One lossy display event for terminal progress rendering."""

    kind: str
    phase: str
    subject: str
    line: str
    failed: bool = False
    exact: bool = False


class AsyncNarrator:
    """Run optional structured narration in one background request."""

    def __init__(
        self,
        structured_output: Callable[..., dict[str, Any]],
        *,
        selected_model: str | None = None,
        selected_url: str | None = None,
        api: str | None = None,
        timeout: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.structured_output = structured_output
        self.selected_model = selected_model
        self.selected_url = selected_url
        self.api = api
        self.timeout = timeout
        self.clock = clock
        self.generation = 0
        self.result: dict[str, Any] | None = None
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()

    def submit(
        self,
        *,
        objective: str,
        events: list[ProgressEvent],
        previous_phase: str,
    ) -> None:
        """Start one narration request if none is already running."""
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                self.generation += 1
                return
            self.generation += 1
            generation = self.generation
            self.result = None
            self.thread = threading.Thread(
                target=self.run,
                args=(generation, objective, tuple(events[-8:]), previous_phase),
                daemon=True,
            )
            self.thread.start()

    def run(
        self,
        generation: int,
        objective: str,
        events: tuple[ProgressEvent, ...],
        previous_phase: str,
    ) -> None:
        started = self.clock()
        try:
            data = self.structured_output(
                narrator_messages(objective, events, previous_phase),
                schema=NARRATOR_RESPONSE_SCHEMA,
                response_name="terminal_progress_narration",
                max_tokens=160,
                selected_model=self.selected_model,
                selected_url=self.selected_url,
                api=self.api,
            )
        except Exception:
            return
        if self.clock() - started > self.timeout:
            return
        if not valid_narrator_result(data):
            return
        with self.lock:
            if generation == self.generation:
                self.result = data

    def consume(self) -> dict[str, Any] | None:
        with self.lock:
            result = self.result
            self.result = None
            return result

    def wait(self) -> None:
        thread = self.thread
        if thread is not None:
            thread.join()


def narrator_messages(
    objective: str,
    events: tuple[ProgressEvent, ...],
    previous_phase: str,
) -> list[dict[str, Any]]:
    lines = "\n".join(f"- {event.line}" for event in events)
    return [
        {
            "role": "system",
            "content": (
                "You narrate an AI terminal agent's progress.\n"
                "Rules:\n"
                "- One short phase title and up to two summary lines.\n"
                "- No speculation beyond the events.\n"
                "- Do not mention internal JSON/tool schemas.\n"
                "- If nothing meaningful changed, use an empty phase."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Objective:\n{objective}\n\n"
                f"Recent events:\n{lines}\n\n"
                f"Previous phase:\n{previous_phase}"
            ),
        },
    ]


def valid_narrator_result(data: dict[str, Any]) -> bool:
    phase = data.get("phase")
    summary_lines = data.get("summary_lines")
    if not isinstance(phase, str):
        return False
    if not isinstance(summary_lines, list):
        return False
    return all(isinstance(line, str) for line in summary_lines)


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
        narrator: AsyncNarrator | None = None,
    ) -> None:
        self.output = output
        self.mode = mode if mode in PROGRESS_MODES else PROGRESS_MODE_COMPACT
        self.objective = objective
        self.clock = clock
        self.started_at = clock()
        self.chapter_event_threshold = chapter_event_threshold
        self.chapter_seconds_threshold = chapter_seconds_threshold
        self.max_chapter_lines = max_chapter_lines
        self.narrator = narrator
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

    def observe_tool_result(self, name: str, result: dict[str, Any]) -> None:
        args = self.pop_args(name)
        event = progress_event_for_tool_result(name, result, args)
        if event is None:
            return
        self.observe_event(event)

    def observe_event(self, event: ProgressEvent) -> None:
        self.flush_narration()
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
        self.maybe_submit_narration()
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

    def maybe_submit_narration(self) -> None:
        if self.narrator is None:
            return
        self.narrator.submit(
            objective=self.objective,
            events=self.events,
            previous_phase=self.current_phase,
        )

    def flush_narration(self) -> None:
        if self.narrator is None:
            return
        data = self.narrator.consume()
        if data is None:
            return
        phase = str(data.get("phase") or "").strip()
        if phase:
            self.current_phase = phase

    def status_detail(self) -> str:
        self.flush_narration()
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

    def finalize(self, turn: dict[str, Any]) -> None:
        self.flush_narration()
        line = final_digest_line(turn, self)
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
        return phase_title(match.group(1))
    lower = normalized.lower()
    for prefix in REASONING_PHASE_PREFIXES:
        if lower.startswith(prefix):
            return phase_title(normalized)
    return ""


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
    handoff = result.get("handoff")
    staged = isinstance(handoff, dict)
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
    handoff = result.get("handoff")
    handoff = handoff if isinstance(handoff, dict) else {}
    for source in (metadata, handoff, args):
        for key in progress_subject_fields(name):
            value = source.get(key)
            if isinstance(value, str) and value:
                return text_content_path(value)
    return summarize(name, args)


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
        # Reasoning is process display, owned by the thinking status; the
        # answer renderer never shows it.
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

    def finalize(self, telemetry: dict[str, Any] | None = None) -> bool:
        """Leave one final context line in scrollback."""
        usage = provider_context_usage_tokens(telemetry)
        if usage is not None:
            self.current_context_tokens, self.model_context_tokens = usage
            self.pending_context_tokens = 0
        line = context_usage_line(telemetry) or self.last_line
        if not line:
            return False
        self.last_line = line
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


def context_usage_line(telemetry: dict[str, Any] | None) -> str:
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
    return f"context  [{bar}] {percent}%{suffix}"


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
) -> int | None:
    if prompt_tokens is None or completion_tokens is None:
        return None
    return prompt_tokens + completion_tokens


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
        exc_type: type[BaseException] | None = None,
        exc: BaseException | None = None,
        traceback: TracebackType | None = None,
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
        if not text or not self.enabled or not self.trace_enabled:
            return
        with self.lock:
            self.reasoning_parts.append(text)
            self.reasoning_seen = True
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
        lines.append(
            muted(thinking_status_text(seconds, self.left_padding), enabled=color)
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


def thinking_status_text(seconds: int, left_padding: int = 0) -> str:
    return f"{' ' * left_padding}thinking {seconds}s"


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


TRANSCRIPT_SKIP_EVENT_TYPES = frozenset({"model_usage"})


def render_transcript(events: list[dict[str, Any]], *, console: Console) -> None:
    """Render a session timeline as a conversation transcript.

    Consecutive tool exchanges stay contiguous; a blank line separates
    everything else.
    """
    seen_call_ids: set[str] = set()
    pending_results = transcript_results_index(events)
    previous_kind: str | None = None
    for event in events:
        renderables = transcript_event_renderables(
            event,
            seen_call_ids,
            pending_results,
        )
        if not renderables:
            continue
        kind = transcript_block_kind(event)
        if previous_kind is not None and (kind, previous_kind) != ("tool", "tool"):
            console.print()
        for renderable in renderables:
            console.print(renderable)
        previous_kind = kind


def transcript_block_kind(event: dict[str, Any]) -> str:
    event_type = str(event.get("type") or "")
    if event_type in {"tool_call", "tool_result"}:
        return "tool"
    if event_type == "assistant_message" and not str(event.get("content") or ""):
        return "tool"
    return "message"


def transcript_results_index(
    events: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Index tool results by call id so each renders joined to its call."""
    index: dict[str, dict[str, Any]] = {}
    for event in events:
        if str(event.get("type") or "") != "tool_result":
            continue
        call_id = str(event.get("tool_call_id") or "")
        if call_id:
            index.setdefault(call_id, event)
    return index


def transcript_event_renderables(
    event: dict[str, Any],
    seen_call_ids: set[str],
    pending_results: dict[str, dict[str, Any]],
) -> list[Any]:
    """Map one timeline event to its transcript renderables, if any.

    Tool calls appear both embedded in assistant messages and as separate
    events depending on the projection path; ``seen_call_ids`` keeps each
    call rendered once. ``pending_results`` joins each result to its call;
    a consumed result event renders nothing at its own position.
    """
    event_type = str(event.get("type") or "")
    if event_type in TRANSCRIPT_SKIP_EVENT_TYPES:
        return []
    if event_type == "user_message":
        return transcript_message_panel("you", "cyan", event)
    if event_type == "assistant_message":
        return transcript_assistant_block(event, seen_call_ids, pending_results)
    if event_type == "tool_call":
        call_id = str(event.get("id") or event.get("tool_call_id") or "")
        if call_id and call_id in seen_call_ids:
            return []
        result_event = pending_results.pop(call_id, None) if call_id else None
        name = str(event.get("name") or "")
        return [transcript_tool_exchange(name, event.get("input"), result_event)]
    if event_type == "tool_result":
        return transcript_unmatched_result(event, pending_results)
    if event_type == "turn_aborted":
        content = str(event.get("content") or "(turn aborted)")
        return [Text(content, style="yellow")]
    role = str(event.get("role") or "")
    if role:
        label = "you" if role == "user" else role
        style = "cyan" if role == "user" else "white"
        return transcript_message_panel(label, style, event)
    return []


def transcript_unmatched_result(
    event: dict[str, Any],
    pending_results: dict[str, dict[str, Any]],
) -> list[Any]:
    """Render a result only when no call consumed it from the index."""
    call_id = str(event.get("tool_call_id") or "")
    if call_id:
        indexed = pending_results.get(call_id)
        if indexed is None:
            return []
        if indexed is event:
            pending_results.pop(call_id, None)
    return transcript_tool_result_lines(event)


USER_SCAFFOLD_LABELS = (
    "cwd:",
    "Recent shell activity:",
    "Last failed command context:",
)


def user_message_text(content: str) -> Text:
    """Dim the runtime scaffolding so the user's own words stand out."""
    rendered = Text()
    for index, section in enumerate(content.split("\n\n")):
        if index:
            rendered.append("\n\n")
        first_line = section.split("\n", 1)[0]
        if first_line in USER_SCAFFOLD_LABELS:
            rendered.append(section, style="dim")
        elif first_line == "Question:":
            rendered.append("Question:", style="dim")
            rendered.append(section[len("Question:") :])
        else:
            rendered.append(section)
    return rendered


def transcript_message_panel(
    label: str,
    border_style: str,
    event: dict[str, Any],
) -> list[Any]:
    content = str(event.get("content") or "")
    if not content:
        return []
    body = user_message_text(content) if label == "you" else Text(content)
    return [
        Panel(
            body,
            title=Text(label, style=f"bold {border_style}"),
            title_align="left",
            border_style=border_style,
        )
    ]


def transcript_assistant_block(
    event: dict[str, Any],
    seen_call_ids: set[str],
    pending_results: dict[str, dict[str, Any]],
) -> list[Any]:
    renderables: list[Any] = []
    reasoning = str(event.get("reasoning") or "")
    if reasoning:
        renderables.append(Markdown(reasoning, style="italic magenta"))
    content = str(event.get("content") or "")
    if content:
        prompt_id = transcript_prompt_id(event)
        renderables.append(
            Panel(
                Markdown(content),
                title=Text("sigil", style="bold magenta"),
                title_align="left",
                subtitle=Text(prompt_id, style="dim") if prompt_id else None,
                subtitle_align="right",
                border_style="magenta",
            )
        )
    renderables.extend(
        transcript_embedded_tool_calls(event, seen_call_ids, pending_results)
    )
    return renderables


def transcript_embedded_tool_calls(
    event: dict[str, Any],
    seen_call_ids: set[str],
    pending_results: dict[str, dict[str, Any]],
) -> list[Any]:
    tool_calls = event.get("tool_calls")
    if not isinstance(tool_calls, list):
        return []
    lines = []
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if not isinstance(function, dict):
            continue
        call_id = str(call.get("id") or "")
        if call_id:
            seen_call_ids.add(call_id)
        result_event = pending_results.pop(call_id, None) if call_id else None
        name = str(function.get("name") or "")
        lines.append(
            transcript_tool_exchange(
                name,
                parse_arguments(function.get("arguments")),
                result_event,
            )
        )
    return lines


def parse_arguments(arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, str):
        return {}
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def transcript_tool_exchange(
    name: str,
    args: Any,
    result_event: dict[str, Any] | None,
) -> Text:
    """Render one call joined with its result: inline when it fits one line."""
    call_text = f"→ {name} {summarize(name, args)}".rstrip()
    lines, failed = transcript_result_lines(name, result_event)
    if not lines:
        return Text(call_text, style="dim")
    if len(lines) == 1 and not failed:
        return Text(f"{call_text} — {lines[0]}", style="dim")
    exchange = Text(call_text, style="dim")
    for index, line in enumerate(lines):
        prefix = "  ✗ " if failed and index == 0 else "    " if failed else "  "
        exchange.append(f"\n{prefix}{line}", style="yellow" if failed else "dim")
    return exchange


def transcript_result_lines(
    name: str,
    result_event: dict[str, Any] | None,
) -> tuple[list[str], bool]:
    if result_event is None:
        return [], False
    result = result_event.get("result")
    if not isinstance(result, dict):
        return [], False
    lines = tool_result_summary(name or str(result_event.get("name") or ""), result)
    failed = result.get("ok") is False
    if failed and lines and name:
        # The call line above names the tool and the ✗ marks the failure.
        lines = [lines[0].removeprefix(f"{name}-failed: "), *lines[1:]]
    return lines, failed


def transcript_tool_result_lines(event: dict[str, Any]) -> list[Any]:
    result = event.get("result")
    if not isinstance(result, dict):
        return []
    lines = tool_result_summary(str(event.get("name") or ""), result)
    if not lines:
        return []
    return [Text("\n".join(f"  {line}" for line in lines), style="dim")]


def transcript_prompt_id(event: dict[str, Any]) -> str:
    prompt_trace = event.get("prompt_trace")
    if not isinstance(prompt_trace, dict):
        return ""
    object_id = str(prompt_trace.get("prompt_object_id") or "")
    return short_trace_id(object_id) if object_id else ""
