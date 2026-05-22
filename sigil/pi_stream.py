from __future__ import annotations

import json
import sys
import threading
import time
from typing import TextIO

from .ansi import MUTED, RESET
from .state import append_event


def clear_status(stderr: TextIO) -> None:
    stderr.write("\r\033[K")
    stderr.flush()


def summarize(tool: str, args: object) -> str:
    if not isinstance(args, dict):
        return ""
    if tool == "read":
        return str(args.get("path") or args.get("file_path") or "")
    if tool == "web_search":
        return str(args.get("query") or args.get("q") or "")
    return " ".join(
        f"{k}={v}" for k, v in args.items() if isinstance(v, (str, int, float, bool))
    )


def stream_events(stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout, stderr: TextIO = sys.stderr) -> int:
    started_text = False
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
                append_event({"type": "tool_start", "tool": tool, "detail": detail})
                if detail:
                    print(f"{MUTED}❯ {tool}  {detail}{RESET}", file=stderr, flush=True)
                else:
                    print(f"{MUTED}❯ {tool}{RESET}", file=stderr, flush=True)
                continue

            if event.get("type") == "tool_execution_end":
                append_event({"type": "tool_end", "tool": event.get("toolName", "")})
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
                append_event({"type": "answer_delta", "text": delta})
                stdout.write(delta)
                stdout.flush()
    finally:
        if spinner_running:
            stop_spinner()
    return 0

