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
from typing import TextIO

from .ansi import MUTED, RESET
from .security import normalize_capability, normalize_integrity
from .state import append_event, append_jsonl


def clear_status(stderr: TextIO) -> None:
    """Erase the transient spinner/status line before printing durable output."""
    stderr.write("\r\033[K")
    stderr.flush()


def summarize(tool: str, args: object) -> str:
    """Extract a short human-readable label for a tool call."""
    if not isinstance(args, dict):
        return ""
    if tool == "read":
        return str(args.get("path") or args.get("file_path") or "")
    if tool == "web_search":
        return str(args.get("query") or args.get("q") or "")
    return " ".join(
        f"{k}={v}" for k, v in args.items() if isinstance(v, (str, int, float, bool))
    )


def env_security() -> dict[str, object]:
    """Recover trust metadata passed from the parent `sigil question` process."""
    taint = [
        item
        for item in os.environ.get("SIGIL_SECURITY_TAINT", "").split(",")
        if item
    ]
    inputs = [
        item
        for item in os.environ.get("SIGIL_SECURITY_INPUTS", "").split(",")
        if item
    ]
    return {
        "glyph": os.environ.get("SIGIL_SECURITY_GLYPH", "?"),
        "inputs": inputs,
        "integrity": normalize_integrity(os.environ.get("SIGIL_SECURITY_INTEGRITY")),
        "capability": normalize_capability(os.environ.get("SIGIL_SECURITY_CAPABILITY")),
        "taint": taint or ["web"],
        "provisional": os.environ.get("SIGIL_SECURITY_PROVISIONAL") == "1",
    }


def stream_events(stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout, stderr: TextIO = sys.stderr) -> int:
    """Filter Pi's event stream into terminal output and Sigil state files."""
    started_text = False
    answer_chunks: list[str] = []
    security = env_security()
    spinner_running = True
    spinner_paused = False
    spinner_lock = threading.Lock()

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
                stderr.write(f"\r\033[K{MUTED}❯ {frames[i % len(frames)]}{RESET}")
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
        with spinner_lock:
            spinner_running = False
            spinner_paused = False
        spinner_thread.join()

    spinner_thread = threading.Thread(target=spinner, daemon=True)
    spinner_thread.start()

    try:
        for raw_line in stdin:
            try:
                event = json.loads(raw_line)
            except Exception:
                continue

            if event.get("type") == "tool_execution_start":
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
                if os.environ.get("SIGIL_CAPTURE_TRACE") == "1":
                    append_jsonl("last-tools.jsonl", trace_event)
                append_event(trace_event)
                if detail:
                    print(f"{MUTED}❯ {tool}  {detail}{RESET}", file=stderr, flush=True)
                else:
                    print(f"{MUTED}❯ {tool}{RESET}", file=stderr, flush=True)
                continue

            if event.get("type") == "tool_execution_end":
                trace_event = {"type": "tool_end", "tool": event.get("toolName", ""), **security}
                if os.environ.get("SIGIL_CAPTURE_TRACE") == "1":
                    append_jsonl("last-tools.jsonl", trace_event)
                append_event(trace_event)
                resume_spinner()
                continue

            if event.get("type") != "message_update":
                continue

            update = event.get("assistantMessageEvent") or {}
            if update.get("type") == "text_delta":
                if not started_text:
                    stop_spinner()
                    stdout.write("\n")
                    started_text = True
                delta = update.get("delta", "")
                answer_chunks.append(delta)
                stdout.write(delta)
                stdout.flush()
    finally:
        if spinner_running:
            stop_spinner()
        answer = "".join(answer_chunks)
        if answer:
            answer_event = append_event(
                {"type": "answer_done", "bytes": len(answer.encode("utf-8")), **security}
            )
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
    return 0
