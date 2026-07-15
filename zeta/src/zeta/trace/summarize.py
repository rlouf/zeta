"""Compact summaries for prompt trace objects."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any, cast

from zeta.context.budget import estimated_tokens_for_text
from zeta.substrate import Object

SUMMARY_FIELDS_BY_TOOL = {
    "read": ("path", "file_path"),
    "edit": ("location", "path", "file_path"),
    "write": ("path", "file_path"),
    "bash": ("command", "cmd"),
    "grep": ("pattern", "query", "path", "glob"),
    "find": ("pattern", "query", "path", "glob"),
    "ls": ("pattern", "query", "path", "glob"),
    "query_log": ("turn_id", "touched", "since", "workflow"),
}


def short_trace_id(object_id: str) -> str:
    """Return the short display prefix of a content-addressed id."""

    return object_id.split(":", 1)[-1][:8]


def summarize(tool: str, args: object) -> str:
    """Extract a short human-readable label for a tool call."""

    if not isinstance(args, dict):
        return ""
    tool_args = cast(dict[str, object], args)
    if tool == "web_search":
        return web_search_call_summary(tool_args)
    for field in SUMMARY_FIELDS_BY_TOOL.get(tool, ()):
        value = tool_args.get(field)
        if value:
            return display_path(str(value))
    return " ".join(
        f"{key}={value}"
        for key, value in tool_args.items()
        if isinstance(value, str | int | float | bool)
    )


def display_path(value: str) -> str:
    """Render an absolute path inside the cwd as a relative one."""

    if not value.startswith("/"):
        return value
    cwd = os.getcwd().rstrip("/")
    if value == cwd:
        return "."
    if value.startswith(cwd + "/"):
        return value[len(cwd) + 1 :]
    return value


def web_search_call_summary(args: dict[str, object]) -> str:
    query = args.get("query")
    if isinstance(query, str) and query.strip():
        return quoted_summary(query.strip())
    queries = args.get("search_queries")
    if isinstance(queries, list):
        for query in queries:
            if isinstance(query, str) and query.strip():
                return quoted_summary(query.strip())
    objective = args.get("objective")
    if isinstance(objective, str) and objective.strip():
        return quoted_summary(objective.strip())
    return ""


def quoted_summary(text: str) -> str:
    escaped = text.replace('"', '\\"')
    return f'"{truncate(escaped, 48)}"'


def trace_object_summary(
    obj: Object,
    *,
    get_object: Callable[[str], Object | None] | None = None,
) -> str:
    """Return a one-line human summary for a trace object."""

    if obj.kind == "prompt":
        return prompt_trace_summary(obj, get_object)
    if obj.kind == "assistant_message":
        return assistant_trace_summary(obj.data)
    if obj.kind == "tool_call":
        name = str(obj.data.get("name") or "")
        label = summarize(name, obj.data.get("input"))
        return truncate(" ".join(part for part in (name, label) if part))
    if obj.kind == "tool_result":
        return tool_result_trace_summary(obj.data)
    if obj.kind == "run_event":
        event = obj.data.get("event")
        return str(event.get("type") or "") if isinstance(event, dict) else ""
    message = obj.data.get("message")
    if isinstance(message, dict):
        head = first_line(str(message.get("content") or ""))
        if head:
            return head
    return obj.schema


def prompt_trace_summary(
    obj: Object,
    get_object: Callable[[str], Object | None] | None,
) -> str:
    """Summarize a prompt object as component count plus token estimate."""

    count = len(obj.links)
    label = f"{count} component" + ("" if count == 1 else "s")
    if get_object is None:
        return label
    tokens = estimated_prompt_tokens(obj.links, get_object)
    return f"{label} · ~{tokens} tok"


def estimated_prompt_tokens(
    links: tuple[str, ...],
    get_object: Callable[[str], Object | None],
) -> int:
    """Estimate token usage from a prompt's linked component objects."""

    tokens = 0
    for link in links:
        component = get_object(link)
        if component is None:
            continue
        tokens += estimated_tokens_for_text(
            json.dumps(
                component.data,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    return tokens


def assistant_trace_summary(data: dict[str, Any]) -> str:
    """Summarize an assistant message as its text head or tool-call names."""

    message = assistant_trace_message(data)
    if message is None:
        return ""
    head = first_line(str(message.get("content") or ""))
    if head:
        return head
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        names = [
            str(call.get("function", {}).get("name") or "")
            for call in tool_calls
            if isinstance(call, dict)
        ]
        names = [name for name in names if name]
        if names:
            return "→ " + ", ".join(names)
    return ""


def assistant_trace_message(data: dict[str, Any]) -> dict[str, Any] | None:
    """Return the assistant message projection from old or neutral trace data."""

    message = data.get("message")
    if isinstance(message, dict):
        return message
    model_output = data.get("model_output")
    if isinstance(model_output, dict):
        message = model_output.get("message")
        if isinstance(message, dict):
            return message
    return None


def tool_result_trace_summary(data: dict[str, Any]) -> str:
    """Summarize a tool result object as name, status, and content head."""

    name = str(data.get("name") or "")
    result = data.get("result")
    if not isinstance(result, dict):
        return name
    if result.get("ok") is False:
        detail = failed_tool_result_message(result)
        parts = (name, "failed", detail)
    else:
        status = "ok" if result.get("ok") is True else ""
        parts = (name, status, first_line(text_content(result)))
    return truncate(" · ".join(part for part in parts if part))


def failed_tool_result_message(result: dict[str, Any]) -> str:
    error = result.get("error")
    if isinstance(error, dict):
        return format_tool_error(error)
    return str(result.get("message") or "").strip()


def format_tool_error(error: dict[str, Any]) -> str:
    code = str(error.get("code") or "").strip()
    message = str(error.get("message") or "").strip()
    return ": ".join(part for part in (code, message) if part)


def first_line(text: str) -> str:
    """Return the first non-empty display line of a text block."""

    stripped = text.strip()
    if not stripped:
        return ""
    return truncate(stripped.splitlines()[0])


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


def truncate(value: object, limit: int = 96) -> str:
    """Return a single display line bounded to a fixed width."""

    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


__all__ = [
    "assistant_trace_message",
    "assistant_trace_summary",
    "estimated_prompt_tokens",
    "short_trace_id",
    "summarize",
    "text_content",
    "trace_object_summary",
    "truncate",
]
