"""Small terminal rendering helpers for Sigil routes."""

from __future__ import annotations

import os
from typing import Any, TextIO, cast

from .tty import MUTED, RESET

TRACE_LABEL_WIDTH = 5


def render_tool_start(name: str, params: dict[str, Any], *, output: TextIO) -> None:
    """Print a visible tool-start line using the same shape as the stream renderer."""
    detail = summarize(name, params)
    status = f"❯ {name:<{TRACE_LABEL_WIDTH}}  {detail}" if detail else f"❯ {name}"
    print(muted(status, enabled=should_color(output)), file=output, flush=True)


def should_color(stream: TextIO) -> bool:
    """Return whether terminal color should be emitted to a stream."""
    return (
        bool(getattr(stream, "isatty", lambda: False)())
        and "NO_COLOR" not in os.environ
    )


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
        "edit": ("path", "file_path"),
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
    text = text_content(result)
    if name == "read":
        return [f"{count_lines(text)} lines · {len(text.encode())} bytes"]
    if name == "ls":
        entries = metadata.get("entries")
        if isinstance(entries, int):
            return [f"{entries} entries"]
        return [f"{count_lines(text)} entries"]
    if name == "grep":
        matches = [line for line in text.splitlines() if line]
        files = {line.split(":", 1)[0] for line in matches if ":" in line}
        if files:
            return [f"{len(matches)} matches · {len(files)} files"]
        return [f"{len(matches)} matches"]
    if result.get("ok") is False:
        return [str(result.get("message") or result.get("error") or "failed")]
    if result.get("ok") is True:
        return ["ok"]
    return []


def shell_result_summary(event: dict[str, Any]) -> list[str]:
    """Return compact user-facing lines for a shell handoff result event."""
    result = event.get("result")
    if not isinstance(result, dict):
        return []
    outcome = str(result.get("outcome") or "")
    if outcome == "executed":
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
    if outcome == "cancelled":
        expected = result.get("expected_command") or ""
        actual = result.get("actual_command") or ""
        lines = [
            "❯ shell  changed" if actual else "❯ shell  cancelled",
            f"  expected: {truncate(expected)}",
        ]
        if actual:
            lines.append(f"  ran:      {truncate(actual)}")
        return lines
    if outcome == "no_pending_handoff":
        return ["❯ shell  no handoff"]
    return []


def handoff_summary(name: str, handoff: dict[str, Any]) -> list[str]:
    """Return compact lines for a tool result that stages shell work."""
    artifact = str(handoff.get("artifact") or "")
    if name == "bash":
        return ["staged in prompt"]
    if name == "edit":
        return [f"staged patch · {artifact}" if artifact else "staged patch"]
    if name == "write":
        return [f"staged write · {artifact}" if artifact else "staged write"]
    if artifact:
        return [f"staged in prompt · {artifact}"]
    return ["staged in prompt"]


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
