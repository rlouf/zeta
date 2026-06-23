"""Terminal output and transcript rendering helpers."""

import json
from typing import Any, TextIO

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from sigil.display.state import TraceRenderState
from sigil.display.summarize import short_trace_id, summarize, tool_result_summary
from sigil.display.tty import muted, should_color

TRACE_LABEL_WIDTH = 5


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
    if event_type == "model" and not str(event.get("content") or ""):
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
    if event_type == "model":
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
        prompt_id = ""
        prompt_trace = event.get("prompt_trace")
        if isinstance(prompt_trace, dict):
            object_id = str(prompt_trace.get("prompt_object_id") or "")
            if object_id:
                prompt_id = short_trace_id(object_id)
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
