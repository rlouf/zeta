"""Compact user-facing summaries for tool, handoff, and shell results."""

import json
import os
import time
from collections.abc import Callable
from typing import Any, cast

from sigil.protocols import (
    SHELL_HANDOFF_OUTCOME_CANCELLED,
    SHELL_HANDOFF_OUTCOME_EXECUTED,
    SHELL_HANDOFF_OUTCOME_NO_PENDING,
)
from zeta.capabilities.execution import proposed_effect
from zeta.context.budget import estimated_tokens_for_text
from zeta.records.objects import Object

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
        if isinstance(value, (str, int, float, bool))
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


def tool_result_summary(name: str, result: dict[str, Any]) -> list[str]:
    """Return compact user-facing lines for a Zeta tool result."""
    effect = proposed_effect(result)
    if effect is not None:
        return proposed_effect_summary(name, effect)

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
    if name == "web_search":
        return web_result_summary(name, metadata)
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
            return [f"wrote · {display_path(path)}"]
        return ["wrote"]
    return []


def web_result_summary(name: str, metadata: dict[str, Any]) -> list[str]:
    if name == "web_search":
        return web_search_result_summary(metadata)
    return ["ok"]


def web_search_result_summary(metadata: dict[str, Any]) -> list[str]:
    result_count = metadata.get("result_count")
    if not isinstance(result_count, int):
        return ["ok"]
    noun = "result" if result_count == 1 else "results"
    suffix = " · truncated" if metadata.get("truncated") is True else ""
    return [f"{result_count} {noun}{suffix}"]


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
    return staged_command_summary(name, str(handoff.get("artifact") or ""))


def proposed_effect_summary(name: str, effect: dict[str, Any]) -> list[str]:
    """Return compact lines for a proposed tool effect."""
    return staged_command_summary(name, str(effect.get("artifact") or ""))


def staged_command_summary(name: str, artifact: str) -> list[str]:
    if name == "bash":
        return ["staged"]
    if name == "edit":
        return [f"staged patch · {artifact}" if artifact else "staged patch"]
    if name == "write":
        return [f"staged write · {artifact}" if artifact else "staged write"]
    if artifact:
        return [f"staged · {artifact}"]
    return ["staged"]


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


def short_trace_id(object_id: str) -> str:
    """Return the short display prefix of a content-addressed id."""
    return object_id.split(":", 1)[-1][:8]


def format_turn_line(
    turn: dict[str, Any],
    *,
    show_cost: bool,
    show_session: bool = False,
) -> str:
    """Format one turn history record as a log listing line."""
    turn_id = str(turn.get("turn_id") or "")[:8]
    when = format_turn_time(turn.get("time"))
    workflow = str(turn.get("workflow") or "?")
    outcome = str(turn.get("outcome") or "?")
    session = f"{str(turn.get('session') or '?'):<12} " if show_session else ""
    objective = truncate(first_line(str(turn.get("objective") or "")), 72)
    line = (
        f"{turn_id:<8}  {when}  {workflow:<7} {outcome:<9} "
        f"{session}{objective}".rstrip()
    )
    if show_cost:
        line += turn_cost_suffix(turn.get("cost"))
    return line


def format_turn_time(value: Any) -> str:
    """Render an epoch timestamp as a compact local time."""
    if not isinstance(value, (int, float)):
        return "?" * 11
    return time.strftime("%m-%d %H:%M", time.localtime(value))


def turn_cost_suffix(cost: Any) -> str:
    """Render a turn's cost block as a listing suffix."""
    if not isinstance(cost, dict):
        return ""
    tokens = int(cost.get("input_tokens") or 0) + int(cost.get("output_tokens") or 0)
    calls = int(cost.get("model_calls") or 0)
    if not tokens and not calls:
        return ""
    return f"  · {tokens} tok · {calls} calls"


def render_turn_record(
    turn: dict[str, Any],
    effects: list[dict[str, Any]],
) -> list[str]:
    """Render one turn record as human-readable lines."""
    lines = [
        f"turn     {turn.get('turn_id') or '?'}",
        f"time     {format_turn_time(turn.get('time'))}",
        f"session  {turn.get('session') or '?'}",
        f"workflow {turn.get('workflow') or '?'}",
        f"outcome  {turn.get('outcome') or '?'}",
    ]
    objective = str(turn.get("objective") or "").strip()
    if objective:
        lines.extend(["", "objective"])
        lines.extend(f"  {line}" for line in objective.splitlines()[:8])
    contract = turn.get("contract")
    if isinstance(contract, dict):
        tools = ", ".join(str(tool) for tool in contract.get("allowed_tools") or [])
        staged = " (staged)" if contract.get("staged") else ""
        lines.extend(["", "contract", f"  tools: {tools or 'none'}{staged}"])
    agent = turn.get("agent")
    if isinstance(agent, dict):
        endpoint = " @ ".join(
            part for part in (agent.get("model"), agent.get("url")) if part
        )
        if endpoint:
            lines.extend(["", "agent", f"  {endpoint}"])
    cost_line = format_cost_block(turn.get("cost"))
    if cost_line:
        lines.extend(["", "cost", f"  {cost_line}"])
    if effects:
        lines.extend(["", "effects"])
        lines.extend(f"  {format_effect_line(effect)}" for effect in effects)
    prompt_ids = turn.get("prompt_object_ids")
    if isinstance(prompt_ids, list) and prompt_ids:
        shorts = " ".join(short_trace_id(str(value)) for value in prompt_ids)
        lines.extend(["", "prompts", f"  {shorts}  (sigil trace show ID)"])
    return lines


def format_effect_line(effect: dict[str, Any]) -> str:
    """Render one effect record as a single listing line."""
    kind = str(effect.get("kind") or "?")
    parts = [f"{kind:<10}"]
    path = effect.get("path")
    if path:
        parts.append(str(path))
    command = effect.get("command")
    if command:
        parts.append(truncate(str(command), 60))
    before = effect.get("before_hash")
    after = effect.get("after_hash")
    if before or after:
        parts.append(
            f"{short_trace_id(str(before or '?'))}→{short_trace_id(str(after or '?'))}"
        )
    exit_status = effect.get("exit_status")
    if isinstance(exit_status, int):
        parts.append(f"exit {exit_status}")
    if effect.get("staged"):
        outcome = effect.get("resolved_outcome")
        parts.append(f"staged → {outcome}" if outcome else "staged")
    return " ".join(parts).rstrip()


def format_cost_block(cost: Any) -> str:
    """Render the turn cost block as one line."""
    if not isinstance(cost, dict):
        return ""
    tokens_in = int(cost.get("input_tokens") or 0)
    tokens_out = int(cost.get("output_tokens") or 0)
    parts = []
    if tokens_in or tokens_out:
        parts.append(f"{tokens_in + tokens_out} tok ({tokens_in} in, {tokens_out} out)")
    calls = int(cost.get("model_calls") or 0)
    if calls:
        parts.append(f"{calls} calls")
    wall_ms = cost.get("wall_ms")
    if isinstance(wall_ms, int):
        parts.append(f"{wall_ms} ms")
    return " · ".join(parts)


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
