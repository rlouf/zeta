"""Render Pi JSON events while preserving structured state.

Pi emits machine-readable events. This filter turns tool calls into live grey
status lines, streams answer text to stdout for `glow`, and writes only the
right pieces into session state: assistant turns to the question transcript and
tool calls to the tool trace.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import TextIO, cast

from .ansi import MUTED, RESET
from .security import normalize_capability, normalize_integrity
from .state import append_event, append_jsonl


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
    if tool == "web_search":
        return str(tool_args.get("query") or tool_args.get("q") or "")
    return " ".join(
        f"{k}={v}"
        for k, v in tool_args.items()
        if isinstance(v, (str, int, float, bool))
    )


def env_security() -> dict[str, object]:
    """Recover trust metadata passed from the parent `sigil question` process."""
    taint = [
        item for item in os.environ.get("SIGIL_SECURITY_TAINT", "").split(",") if item
    ]
    inputs = [
        item for item in os.environ.get("SIGIL_SECURITY_INPUTS", "").split(",") if item
    ]
    return {
        "glyph": os.environ.get("SIGIL_SECURITY_GLYPH", "?"),
        "inputs": inputs,
        "integrity": normalize_integrity(os.environ.get("SIGIL_SECURITY_INTEGRITY")),
        "capability": normalize_capability(os.environ.get("SIGIL_SECURITY_CAPABILITY")),
        "taint": taint or ["web"],
        "provisional": os.environ.get("SIGIL_SECURITY_PROVISIONAL") == "1",
    }


def stream_events(
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    *,
    json_output: bool = False,
) -> int:
    """Filter Pi's event stream into terminal output and Sigil state files."""
    started_text = False
    answer_chunks: list[str] = []
    tool_events: list[dict[str, object]] = []
    malformed_events = 0
    security = env_security()
    interactive_stderr = is_interactive(stderr)
    color_enabled = should_color(stderr)
    spinner_running = not json_output and interactive_stderr
    spinner_paused = False
    spinner_lock = threading.Lock()
    spinner_thread: threading.Thread | None = None

    def spinner() -> None:
        frames = ["thinking", "thinking.", "thinking..", "thinking..."]
        i = 0
        while True:
            with spinner_lock:
                if not spinner_running:
                    clear_status(stderr)
                    return
                paused = spinner_paused
            if not paused:
                stderr.write(
                    f"\r\033[K{muted(f'❯ {frames[i % len(frames)]}', enabled=color_enabled)}"
                )
                stderr.flush()
                i += 1
            time.sleep(0.35)

    def pause_spinner() -> None:
        nonlocal spinner_paused
        with spinner_lock:
            spinner_paused = True
        clear_status(stderr)

    def resume_spinner() -> None:
        nonlocal spinner_paused
        with spinner_lock:
            if spinner_running:
                spinner_paused = False

    def stop_spinner() -> None:
        nonlocal spinner_running, spinner_paused
        if spinner_thread is None:
            return
        with spinner_lock:
            spinner_running = False
            spinner_paused = False
        spinner_thread.join()

    if spinner_running:
        spinner_thread = threading.Thread(target=spinner, daemon=True)
        spinner_thread.start()

    try:
        for raw_line in stdin:
            try:
                event = json.loads(raw_line)
            except Exception:
                malformed_events += 1
                continue

            if event.get("type") == "tool_execution_start":
                if spinner_running:
                    pause_spinner()
                tool = event.get("toolName", "")
                detail = summarize(tool, event.get("args"))
                trace_event = {
                    "type": "tool_start",
                    "tool": tool,
                    "detail": detail,
                    "args": event.get("args"),
                    **security,
                }
                tool_events.append(trace_event)
                if os.environ.get("SIGIL_CAPTURE_TRACE") == "1":
                    append_jsonl("last-tools.jsonl", trace_event)
                append_event(trace_event)
                if not json_output:
                    status = f"❯ {tool}  {detail}" if detail else f"❯ {tool}"
                    if detail:
                        print(
                            muted(status, enabled=color_enabled),
                            file=stderr,
                            flush=True,
                        )
                    elif interactive_stderr:
                        print(
                            muted(status, enabled=color_enabled),
                            file=stderr,
                            flush=True,
                        )
                continue

            if event.get("type") == "tool_execution_end":
                trace_event = {
                    "type": "tool_end",
                    "tool": event.get("toolName", ""),
                    **security,
                }
                tool_events.append(trace_event)
                if os.environ.get("SIGIL_CAPTURE_TRACE") == "1":
                    append_jsonl("last-tools.jsonl", trace_event)
                append_event(trace_event)
                if spinner_running:
                    resume_spinner()
                continue

            if event.get("type") != "message_update":
                continue

            update = event.get("assistantMessageEvent") or {}
            if update.get("type") == "text_delta":
                if not json_output and not started_text:
                    stop_spinner()
                    stdout.write("\n")
                    started_text = True
                delta = update.get("delta", "")
                answer_chunks.append(delta)
                if not json_output:
                    stdout.write(delta)
                    stdout.flush()
    finally:
        if spinner_running:
            stop_spinner()
        answer = "".join(answer_chunks)
        answer_event_id = None
        if answer:
            answer_event = append_event(
                {
                    "type": "answer_done",
                    "bytes": len(answer.encode("utf-8")),
                    **security,
                }
            )
            answer_event_id = answer_event["id"]
            if os.environ.get("SIGIL_CAPTURE_ANSWER") == "1":
                append_jsonl(
                    "last-question.jsonl",
                    {
                        "role": "assistant",
                        "content": answer,
                        "event_id": answer_event["id"],
                        **security,
                    },
                )
        if json_output:
            stdout.write(
                json.dumps(
                    {
                        "ok": True,
                        "type": "answer",
                        "question": os.environ.get("SIGIL_QUESTION", ""),
                        "prompt": os.environ.get("SIGIL_PROMPT", ""),
                        "follow_up": os.environ.get("SIGIL_FOLLOW_UP") == "1",
                        "answer": answer,
                        "answer_event_id": answer_event_id,
                        "tools": tool_events,
                        "malformed_events": malformed_events,
                        "security": security,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            stdout.flush()
        elif malformed_events:
            noun = "event" if malformed_events == 1 else "events"
            print(
                f"sigil: ignored {malformed_events} malformed Pi {noun}",
                file=stderr,
            )
    return 0
