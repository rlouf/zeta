"""Project runtime event drafts into prompt-trace-annotated views.

These helpers turn the durable event drafts of a run into the ``current_events``
views handed to the prompt builder, annotating each with the trace object ids
(prompt, assistant message, tool call, tool result) recorded for it. They are a
read-model over the event log and carry no run-loop state.
"""

from __future__ import annotations

from typing import Any

from zeta.context.builder import PromptBuilder
from zeta.records.events import DraftEvent, draft_event_id, draft_event_view
from zeta.records.provenance import (
    PromptTraceProjection,
    project_prompt_trace_projection,
)


def is_runtime_ui_event(draft: DraftEvent) -> bool:
    return draft.event_type in {"runtime.stream.chunk", "runtime.status.update"}


def draft_views_for_prompt(
    drafts: list[DraftEvent],
    builder: PromptBuilder,
) -> list[dict[str, Any]]:
    projection = project_prompt_trace_projection(drafts, builder.store())
    views = []
    for draft in drafts:
        if is_runtime_ui_event(draft):
            continue
        view = draft_event_view(draft)
        event_id = draft_event_id(draft)
        if event_id is not None:
            add_prompt_trace_fields(view, event_id, projection)
        views.append(view)
    return views


def add_prompt_trace_fields(
    view: dict[str, Any],
    event_id: str,
    projection: PromptTraceProjection,
) -> None:
    event_type = view.get("type")
    if event_type == "model":
        add_model_prompt_trace_fields(view, event_id, projection)
        return
    if event_type == "tool_call":
        call_id = projection.tool_call_object_ids.get(event_id)
        if call_id is not None:
            view["tool_call_object_id"] = call_id
        return
    if event_type == "tool_result":
        add_tool_result_trace_fields(view, event_id, projection)


def add_model_prompt_trace_fields(
    view: dict[str, Any],
    event_id: str,
    projection: PromptTraceProjection,
) -> None:
    prompt_id = projection.prompt_object_ids.get(event_id)
    assistant_id = projection.assistant_message_ids.get(event_id)
    if prompt_id is not None:
        view["prompt_trace"] = {"prompt_object_id": prompt_id}
        if assistant_id is not None:
            view["prompt_trace"]["assistant_message_object_id"] = assistant_id
    tool_call_ids = projected_tool_call_ids(view, projection)
    if tool_call_ids:
        view["tool_call_object_ids"] = tool_call_ids


def projected_tool_call_ids(
    view: dict[str, Any],
    projection: PromptTraceProjection,
) -> list[str]:
    tool_calls = view.get("tool_calls")
    if not isinstance(tool_calls, list):
        return []
    return [
        projection.tool_call_object_ids[tool_call["id"]]
        for tool_call in tool_calls
        if isinstance(tool_call, dict)
        and isinstance(tool_call.get("id"), str)
        and tool_call["id"] in projection.tool_call_object_ids
    ]


def add_tool_result_trace_fields(
    view: dict[str, Any],
    event_id: str,
    projection: PromptTraceProjection,
) -> None:
    tool_call_id = view.get("tool_call_id")
    call_id = (
        projection.tool_call_object_ids.get(tool_call_id)
        if isinstance(tool_call_id, str)
        else None
    )
    result_id = projection.tool_result_object_ids.get(event_id)
    if call_id is not None:
        view["tool_call_object_id"] = call_id
    if result_id is not None:
        view["tool_result_object_id"] = result_id
