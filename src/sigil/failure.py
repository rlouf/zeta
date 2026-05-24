"""Failure recovery for the caret glyph.

`^` turns the last failed shell command into repair candidates. It deliberately
stops at proposal: selected fixes are placed on the prompt for review, never
executed automatically.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

from .ansi import LOVE, MUTED, RESET
from .commands import COMMAND_SCHEMA, select
from .qwen import chat_json, ensure_server
from .security import inherit_security, make_security, normalize_security
from .state import append_event, read_json, write_json

FIX_SYSTEM = (
    "You fix failed macOS zsh commands with the default BSD userland. "
    "Return 2-4 corrected candidate commands, best first, each with a terse "
    "one-line note. Do not invent hidden context. Preserve user intent. "
    "Commands must be directly runnable, but they will be reviewed by a human "
    "before execution."
)


def record_failure(command: str, status: int, cwd: str | None = None) -> None:
    """Persist the last nonzero shell command for the current session."""
    security = make_security(
        glyph="^",
        integrity="human",
        capability="propose",
        taint=[],
        fresh_human=True,
    )
    event = append_event(
        {
            "type": "failure_recorded",
            "command": command,
            "status": status,
            "failure_cwd": cwd or os.getcwd(),
            **security,
        }
    )
    write_json(
        "last-failure.json",
        {
            "command": command,
            "status": status,
            "cwd": cwd or os.getcwd(),
            "time": time.time(),
            "event_id": event["id"],
            **security,
        },
    )


def last_failure() -> dict[str, Any]:
    """Load the last recorded failure or exit with a terminal-friendly error."""
    failure = read_json("last-failure.json")
    if not isinstance(failure, dict) or not failure.get("command"):
        print(f"{LOVE}✗ no failed command recorded{RESET}", file=sys.stderr)
        raise SystemExit(1)
    return failure


def generate_fixes() -> tuple[str, list[dict[str, str]], dict[str, Any]]:
    """Generate repair candidates for the current session's last failure."""
    failure = normalize_security(last_failure())
    if not ensure_server():
        raise SystemExit(1)

    print(f"{MUTED}❯ sigil ^  · repair · model-authored{RESET}", file=sys.stderr)
    print(f"{MUTED}⟳ thinking…{RESET}", end="", file=sys.stderr, flush=True)
    user = "\n".join(
        [
            f"Failed command: {failure['command']}",
            f"Exit status: {failure.get('status', 'unknown')}",
            f"Working directory: {failure.get('cwd', '')}",
        ]
    )
    try:
        data = chat_json(FIX_SYSTEM, user, COMMAND_SCHEMA)
    except RuntimeError as exc:
        print("\r\033[K", end="", file=sys.stderr)
        print(f"{LOVE}✗ qwen request failed{RESET}", file=sys.stderr)
        print(f"  {exc}", file=sys.stderr)
        print("  Check that the local model server is still running.", file=sys.stderr)
        raise SystemExit(1) from exc
    except Exception:
        print("\r\033[K", end="", file=sys.stderr)
        print(f"{LOVE}✗ could not generate fix candidates{RESET}", file=sys.stderr)
        raise SystemExit(1)
    print("\r\033[K", end="", file=sys.stderr)

    candidates = [
        {"command": str(item.get("command", "")), "note": str(item.get("note", ""))}
        for item in data.get("commands", [])
        if item.get("command")
    ]
    if not candidates:
        print(f"{LOVE}✗ no fix candidates{RESET}", file=sys.stderr)
        raise SystemExit(1)

    security = make_security(
        glyph="^",
        integrity="local_model",
        capability="propose",
        taint=["model"],
        inputs=[str(failure.get("event_id") or failure.get("id") or "")],
        input_records=[failure],
        fresh_human=True,
    )
    event = append_event(
        {
            "type": "fix_generated",
            "failure": failure,
            "commands": candidates,
            **security,
        }
    )
    write_json(
        "last-fix.json",
        {
            "prompt": str(failure["command"]),
            "failure": failure,
            "commands": candidates,
            "event_id": event["id"],
            **security,
        },
    )
    return str(failure["command"]), candidates, security


def previous_fix() -> tuple[str, list[dict[str, str]], dict[str, Any]]:
    """Load the previous repair candidates for `^^`."""
    data = read_json("last-fix.json")
    if not isinstance(data, dict) or not data.get("commands"):
        print(f"{LOVE}✗ no previous fix suggestions{RESET}", file=sys.stderr)
        raise SystemExit(1)
    security = inherit_security(
        glyph="^^", input_records=[normalize_security(data)], capability="propose"
    )
    return str(data.get("prompt", "")), list(data["commands"]), security


def select_fix() -> str | None:
    """Generate fixes and return the user's selected repair command."""
    prompt, candidates, security = generate_fixes()
    return select(prompt, candidates, security)


def select_previous_fix() -> str | None:
    """Reopen previous repair candidates and return the selected command."""
    prompt, candidates, security = previous_fix()
    continued = append_event({"type": "fix_continued", "prompt": prompt, **security})
    security = {**security, "inputs": [continued["id"]]}
    print(f"{MUTED}❯ sigil ^^ · inherited repair{RESET}", file=sys.stderr)
    return select(prompt, candidates, security)
