"""Render Zeta JSON events while preserving structured state.

Zeta emits machine-readable events. This filter turns tool calls into live grey
status lines, streams answer text to stdout for `glow`, and writes only the
right pieces into session state: assistant turns to the question transcript and
tool calls to the tool trace.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import TextIO, cast

from ..state import append_event, append_jsonl
from ..tty import MUTED, RESET

DEFAULT_GLOW_STYLE = "notty"
DEFAULT_GLOW_WIDTH = "88"
TRACE_LABEL_WIDTH = 5


def renderer_command() -> list[str]:
    """Return the Markdown renderer command for interactive Zeta answers."""
    if not shutil.which("glow"):
        return ["cat"]
    style = os.environ.get("ZETA_GLOW_STYLE") or DEFAULT_GLOW_STYLE
    width = os.environ.get("ZETA_GLOW_WIDTH") or DEFAULT_GLOW_WIDTH
    return ["glow", "--style", style, "--width", width, "-"]


def run_zeta_stream(
    zeta_cmd: list[str],
    *,
    zeta_env: dict[str, str] | None = None,
    question: str = "",
    prompt: str = "",
    follow_up: bool = False,
    capture_answer: bool = True,
    capture_trace: bool = True,
    json_output: bool = False,
    compact: bool = False,
    tool_output_stdout: bool = False,
) -> int:
    """Run Zeta and render its JSON event stream in-process; return Zeta's exit code."""
    zeta_proc = subprocess.Popen(
        zeta_cmd,
        stdout=subprocess.PIPE,
        env=zeta_env,
        pass_fds=inherited_terminal_fds(zeta_env),
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert zeta_proc.stdout is not None
    try:
        stream_events(
            cast(TextIO, zeta_proc.stdout),
            question=question,
            prompt=prompt,
            follow_up=follow_up,
            capture_answer=capture_answer,
            capture_trace=capture_trace,
            json_output=json_output,
            compact=compact,
            tool_output_stdout=tool_output_stdout,
        )
    finally:
        zeta_proc.stdout.close()
    return zeta_proc.wait()


def inherited_terminal_fds(env: dict[str, str] | None = None) -> tuple[int, ...]:
    """Return terminal fds that Zeta extensions need Python to keep open."""
    raw = (env or os.environ).get("ZETA_TTY_FD")
    if not raw:
        return ()
    try:
        fd = int(raw)
    except ValueError:
        return ()
    if fd < 0:
        return ()
    try:
        os.fstat(fd)
    except OSError:
        return ()
    return (fd,)


def render_answer(answer: str, stdout: TextIO) -> None:
    """Render the finished answer once, through the Markdown renderer when on a tty."""
    if not answer:
        return
    cmd = renderer_command()
    if cmd[0] != "cat" and is_interactive(stdout):
        try:
            stdout.write("\n")
            stdout.flush()
            subprocess.run(cmd, input=answer, text=True, stdout=stdout, check=False)
            return
        except OSError:
            pass
    stdout.write(f"\n{answer}\n")
    stdout.flush()


TOOL_START_EVENT_TYPES = {
    "tool_execution_start",
    "tool_call",
    "toolcall_end",
    "function_call",
}
TOOL_END_EVENT_TYPES = {
    "tool_execution_end",
    "tool_result",
    "tool_call_result",
    "function_call_result",
}


def is_interactive(stream: TextIO) -> bool:
    """Return whether a stream is attached to an interactive terminal."""
    return bool(getattr(stream, "isatty", lambda: False)())


def should_color(stream: TextIO) -> bool:
    """Return whether terminal color should be emitted to a stream."""
    return is_interactive(stream) and "NO_COLOR" not in os.environ


def open_terminal_output() -> TextIO | None:
    """Open the controlling terminal for live-only output when available."""
    try:
        return open("/dev/tty", "w", encoding="utf-8", errors="replace")
    except OSError:
        return None


def muted(text: str, *, enabled: bool) -> str:
    """Apply muted terminal styling when color is enabled."""
    if not enabled:
        return text
    return f"{MUTED}{text}{RESET}"


def clear_status(stderr: TextIO) -> None:
    """Erase the transient spinner/status line before printing durable output."""
    stderr.write("\r\033[K")
    stderr.flush()


def summarize(tool: str, args: object) -> str:
    """Extract a short human-readable label for a tool call."""
    if not isinstance(args, dict):
        return ""
    tool_args = cast(dict[str, object], args)
    if tool == "read":
        return str(tool_args.get("path") or tool_args.get("file_path") or "")
    if tool in {"edit", "write"}:
        return str(tool_args.get("path") or tool_args.get("file_path") or "")
    if tool == "bash":
        return str(tool_args.get("command") or tool_args.get("cmd") or "")
    if tool in {"grep", "find", "ls"}:
        return str(
            tool_args.get("pattern")
            or tool_args.get("query")
            or tool_args.get("path")
            or tool_args.get("glob")
            or ""
        )
    if tool == "web_search":
        return str(tool_args.get("query") or tool_args.get("q") or "")
    return " ".join(
        f"{k}={v}"
        for k, v in tool_args.items()
        if isinstance(v, (str, int, float, bool))
    )


def event_payload(event: dict[str, object]) -> dict[str, object]:
    """Return the event object that carries Zeta payload fields."""
    update = event.get("assistantMessageEvent")
    if event.get("type") == "message_update" and isinstance(update, dict):
        return cast(dict[str, object], update)
    return event


def event_kind(event: dict[str, object]) -> str:
    """Return the concrete Zeta event kind, including nested message updates."""
    payload = event_payload(event)
    return str(payload.get("type") or "")


def tool_name(payload: dict[str, object]) -> str:
    """Extract a tool/function name from known Zeta event shapes."""
    for key in ("toolName", "functionName", "name", "tool"):
        value = payload.get(key)
        if value:
            return str(value)
    tool_call = payload.get("toolCall")
    if isinstance(tool_call, dict):
        tool_call_payload = cast(dict[str, object], tool_call)
        name = tool_call_payload.get("name")
        if name:
            return str(name)
    indexed_tool_call = tool_call_from_partial(payload)
    if indexed_tool_call is not None:
        name = indexed_tool_call.get("name")
        if name:
            return str(name)
    function = payload.get("function")
    if isinstance(function, dict):
        function_payload = cast(dict[str, object], function)
        name = function_payload.get("name")
        if name:
            return str(name)
    return ""


def tool_args(payload: dict[str, object]) -> object:
    """Extract tool/function arguments from known Zeta event shapes."""
    for key in ("args", "input", "arguments"):
        if key in payload:
            return decoded_args(payload.get(key))
    tool_call = payload.get("toolCall")
    if isinstance(tool_call, dict):
        tool_call_payload = cast(dict[str, object], tool_call)
        return decoded_args(tool_call_payload.get("arguments"))
    indexed_tool_call = tool_call_from_partial(payload)
    if indexed_tool_call is not None:
        return decoded_args(indexed_tool_call.get("arguments"))
    function = payload.get("function")
    if isinstance(function, dict):
        function_payload = cast(dict[str, object], function)
        return decoded_args(function_payload.get("arguments"))
    return None


def tool_call_id(payload: dict[str, object]) -> str:
    """Extract a stable tool-call id from known Zeta event shapes."""
    for key in ("toolCallId", "tool_call_id", "id"):
        value = payload.get(key)
        if value:
            return str(value)
    tool_call = payload.get("toolCall")
    if isinstance(tool_call, dict):
        tool_call_payload = cast(dict[str, object], tool_call)
        value = tool_call_payload.get("id")
        if value:
            return str(value)
    indexed_tool_call = tool_call_from_partial(payload)
    if indexed_tool_call is not None:
        value = indexed_tool_call.get("id")
        if value:
            return str(value)
    return ""


def tool_call_from_partial(payload: dict[str, object]) -> dict[str, object] | None:
    """Return the indexed toolCall block from a partial assistant message."""
    content_index = payload.get("contentIndex")
    if not isinstance(content_index, int):
        return None
    partial = payload.get("partial")
    if not isinstance(partial, dict):
        return None
    partial_payload = cast(dict[str, object], partial)
    content = partial_payload.get("content")
    if (
        not isinstance(content, list)
        or content_index < 0
        or content_index >= len(content)
    ):
        return None
    block = content[content_index]
    if not isinstance(block, dict):
        return None
    block_payload = cast(dict[str, object], block)
    if block_payload.get("type") != "toolCall":
        return None
    return block_payload


def decoded_args(value: object) -> object:
    """Decode JSON argument strings used by function-call events."""
    if not isinstance(value, str):
        return value
    try:
        decoded = json.loads(value)
    except Exception:
        return value
    return decoded


def tool_start_event(event: dict[str, object]) -> tuple[str, object, str] | None:
    """Return normalized tool start data when an event begins a call."""
    payload = event_payload(event)
    if event_kind(event) not in TOOL_START_EVENT_TYPES:
        return None
    name = tool_name(payload)
    if not name:
        return None
    return name, tool_args(payload), tool_call_id(payload)


def tool_end_event(event: dict[str, object]) -> str | None:
    """Return a normalized tool name when an event ends a call."""
    payload = event_payload(event)
    if event_kind(event) not in TOOL_END_EVENT_TYPES:
        return None
    return tool_name(payload)


def compact_tool_label(tool: object) -> str:
    """Return the short label used in compact act traces."""
    if tool == "bash":
        return "check"
    if tool == "grep":
        return "search"
    if tool == "ls":
        return "list"
    return str(tool or "tool")


def compact_detail(detail: str, *, limit: int = 120) -> str:
    """Shorten paths and commands for compact terminal display."""
    text = " ".join(detail.split())
    if not text:
        return ""
    try:
        path = os.path.relpath(text, os.getcwd())
    except ValueError:
        path = text
    if not path.startswith(".."):
        text = path
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def compact_answer_summary(answer: str, *, limit: int = 180) -> str:
    """Return a one-line completion summary from Zeta's final answer."""
    lines = []
    in_fence = False
    for raw_line in answer.splitlines():
        line = raw_line.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not line:
            continue
        line = line.strip("*`- ")
        if line.lower().startswith("verification command"):
            break
        lines.append(line)
    start = None
    for index in range(len(lines) - 1, -1, -1):
        lower = lines[index].lower()
        if "all tests pass" in lower or "what changed" in lower:
            start = index
            break
    if start is None:
        for index in range(len(lines) - 1, -1, -1):
            lower = lines[index].lower()
            if lower.startswith(("updated", "changed", "done")):
                start = index
                break
    if start is None:
        selected = lines[-2:]
    else:
        selected = lines[start : start + 3]
    text = " ".join(selected) or "completed"
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


class Spinner:
    """Transient `thinking` status line driven by a background thread."""

    def __init__(self, stderr: TextIO, *, enabled: bool, color: bool) -> None:
        self._stderr = stderr
        self._color = color
        self._running = enabled
        self._paused = False
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        """Return whether the spinner is still active."""
        return self._running

    def start(self) -> None:
        """Start the background thread when the spinner is enabled."""
        if not self._running:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def pause(self) -> None:
        """Pause animation and clear the current status line."""
        with self._lock:
            self._paused = True
        clear_status(self._stderr)

    def resume(self) -> None:
        """Resume animation if the spinner is still running."""
        with self._lock:
            if self._running:
                self._paused = False

    def stop(self) -> None:
        """Stop the background thread and clear the status line."""
        if self._thread is None:
            return
        with self._lock:
            self._running = False
            self._paused = False
        self._thread.join()

    def _run(self) -> None:
        frames = ["thinking", "thinking.", "thinking..", "thinking..."]
        i = 0
        while True:
            with self._lock:
                if not self._running:
                    clear_status(self._stderr)
                    return
                paused = self._paused
            if not paused:
                self._stderr.write(
                    f"\r\033[K{muted(f'❯ {frames[i % len(frames)]}', enabled=self._color)}"
                )
                self._stderr.flush()
                i += 1
            time.sleep(0.35)


@dataclass
class _StreamContext:
    """Output destinations and rendering flags for one Zeta stream."""

    stdout: TextIO
    stderr: TextIO
    compact: bool
    json_output: bool
    color_enabled: bool
    tool_output_stdout: bool = False
    tool_output_terminal: TextIO | None = None
    question: str = ""
    prompt: str = ""
    follow_up: bool = False
    capture_answer: bool = False
    capture_trace: bool = False


def _record_tool_trace(ctx: _StreamContext, trace_event: dict[str, object]) -> None:
    """Persist a tool trace event to the trace log and global event log."""
    if ctx.capture_trace:
        append_jsonl("last-tools.jsonl", trace_event)
    append_event(trace_event)


def _render_tool_start(ctx: _StreamContext, tool: str, detail: str) -> None:
    """Print a tool-start status line to stderr."""
    output = ctx.stderr
    if ctx.tool_output_stdout:
        output = ctx.tool_output_terminal if ctx.tool_output_terminal else ctx.stdout
    if ctx.compact:
        label = compact_tool_label(tool)
        short_detail = compact_detail(detail)
        status = f"  {label:<6} {short_detail}" if short_detail else f"  {label}"
        print(status, file=output, flush=True)
        return
    status = f"❯ {tool:<{TRACE_LABEL_WIDTH}}  {detail}" if detail else f"❯ {tool}"
    print(muted(status, enabled=ctx.color_enabled), file=output, flush=True)


def _handle_tool_start(
    event: dict[str, object],
    ctx: _StreamContext,
    spinner: Spinner,
    tool_events: list[dict[str, object]],
    seen_tool_calls: dict[str, str],
) -> bool:
    """Handle a tool-start event; return True when the event was consumed."""
    tool_start = tool_start_event(event)
    if tool_start is None:
        return False
    tool, args, call_id = tool_start
    detail = summarize(tool, args)
    if call_id:
        previous_detail = seen_tool_calls.get(call_id)
        if previous_detail or not detail:
            return True
        seen_tool_calls[call_id] = detail
    if spinner.running:
        spinner.pause()
    trace_event: dict[str, object] = {
        "type": "tool_start",
        "tool": tool,
        "detail": detail,
        "args": args,
        "tool_call_id": call_id,
    }
    tool_events.append(trace_event)
    _record_tool_trace(ctx, trace_event)
    if not ctx.json_output:
        _render_tool_start(ctx, tool, detail)
    return True


def _handle_tool_end(
    event: dict[str, object],
    ctx: _StreamContext,
    spinner: Spinner,
    tool_events: list[dict[str, object]],
) -> bool:
    """Handle a tool-end event; return True when the event was consumed."""
    tool_end = tool_end_event(event)
    if tool_end is None:
        return False
    trace_event: dict[str, object] = {"type": "tool_end", "tool": tool_end}
    tool_events.append(trace_event)
    _record_tool_trace(ctx, trace_event)
    if spinner.running:
        spinner.resume()
    return True


def _handle_text_delta(
    event: dict[str, object],
    ctx: _StreamContext,
    spinner: Spinner,
    answer_chunks: list[str],
    started_text: bool,
) -> bool:
    """Stream an assistant text delta; return the updated started_text flag."""
    if event.get("type") != "message_update":
        return started_text
    raw_update = event.get("assistantMessageEvent")
    if not isinstance(raw_update, dict):
        return started_text
    update = cast(dict[str, object], raw_update)
    if update.get("type") != "text_delta":
        return started_text
    delta = str(update.get("delta", ""))
    if not ctx.json_output and not ctx.compact and not started_text:
        spinner.stop()
        started_text = True
    answer_chunks.append(delta)
    return started_text


def _record_answer(ctx: _StreamContext, answer: str) -> str | None:
    """Record the finished answer to the event log and transcript."""
    if not answer:
        return None
    answer_event = append_event(
        {"type": "answer_done", "bytes": len(answer.encode("utf-8"))}
    )
    if ctx.capture_answer:
        append_jsonl(
            "last-question.jsonl",
            {
                "role": "assistant",
                "content": answer,
                "event_id": answer_event["id"],
            },
        )
    return answer_event["id"]


def _write_json_result(
    ctx: _StreamContext,
    answer: str,
    answer_event_id: str | None,
    tool_events: list[dict[str, object]],
    malformed_events: int,
) -> None:
    """Write the machine-readable answer envelope to stdout."""
    ctx.stdout.write(
        json.dumps(
            {
                "ok": True,
                "type": "answer",
                "question": ctx.question,
                "prompt": ctx.prompt,
                "follow_up": ctx.follow_up,
                "answer": answer,
                "answer_event_id": answer_event_id,
                "tools": tool_events,
                "malformed_events": malformed_events,
            },
            ensure_ascii=False,
        )
        + "\n"
    )
    ctx.stdout.flush()


def _finalize(
    ctx: _StreamContext,
    answer_chunks: list[str],
    tool_events: list[dict[str, object]],
    malformed_events: int,
) -> None:
    """Emit the final answer output once the stream is drained."""
    answer = "".join(answer_chunks)
    answer_event_id = _record_answer(ctx, answer)
    if ctx.json_output:
        _write_json_result(ctx, answer, answer_event_id, tool_events, malformed_events)
    elif ctx.compact:
        ctx.stdout.write(f"done: {compact_answer_summary(answer)}\n")
        ctx.stdout.flush()
    else:
        render_answer(answer, ctx.stdout)
        if malformed_events:
            noun = "event" if malformed_events == 1 else "events"
            print(
                f"zeta: ignored {malformed_events} malformed Zeta {noun}",
                file=ctx.stderr,
            )


def stream_events(
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    *,
    question: str = "",
    prompt: str = "",
    follow_up: bool = False,
    capture_answer: bool = False,
    capture_trace: bool = False,
    json_output: bool = False,
    compact: bool = False,
    tool_output_stdout: bool = False,
) -> int:
    """Filter Zeta's event stream into terminal output and session state files."""
    started_text = False
    answer_chunks: list[str] = []
    tool_events: list[dict[str, object]] = []
    seen_tool_calls: dict[str, str] = {}
    malformed_events = 0
    tool_output_terminal = (
        open_terminal_output()
        if tool_output_stdout and not is_interactive(stdout)
        else None
    )
    tool_color_stream = (
        (tool_output_terminal if tool_output_terminal else stdout)
        if tool_output_stdout
        else stderr
    )
    ctx = _StreamContext(
        stdout=stdout,
        stderr=stderr,
        compact=compact,
        json_output=json_output,
        color_enabled=should_color(tool_color_stream),
        tool_output_stdout=tool_output_stdout,
        tool_output_terminal=tool_output_terminal,
        question=question,
        prompt=prompt,
        follow_up=follow_up,
        capture_answer=capture_answer,
        capture_trace=capture_trace,
    )
    spinner_active = not json_output and not compact and is_interactive(stderr)
    spinner = Spinner(stderr, enabled=spinner_active, color=ctx.color_enabled)
    spinner.start()

    try:
        for raw_line in stdin:
            try:
                event = json.loads(raw_line)
            except Exception:
                malformed_events += 1
                continue
            if _handle_tool_start(event, ctx, spinner, tool_events, seen_tool_calls):
                continue
            if _handle_tool_end(event, ctx, spinner, tool_events):
                continue
            started_text = _handle_text_delta(
                event, ctx, spinner, answer_chunks, started_text
            )
    finally:
        spinner.stop()
        _finalize(ctx, answer_chunks, tool_events, malformed_events)
        if tool_output_terminal is not None:
            tool_output_terminal.close()
    return 0
