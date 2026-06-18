"""Object-link helpers for durable event payloads and prompt components."""

from __future__ import annotations

from typing import Any

from .object import ObjectId


def durable_event_object_links(
    event: dict[str, Any],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    event_type = str(event.get("type") or "")
    used_objects: list[dict[str, str]] = []
    returned_objects: list[dict[str, str]] = []
    if event_type == "model":
        prompt_trace = event.get("prompt_trace")
        if isinstance(prompt_trace, dict):
            add_object_link(
                used_objects,
                "prompt",
                trace_object_id(prompt_trace, "prompt_object_id"),
            )
            add_object_link(
                returned_objects,
                "assistant_message",
                trace_object_id(prompt_trace, "assistant_message_object_id"),
            )
        add_object_links(
            returned_objects,
            "tool_call",
            event.get("tool_call_object_ids"),
        )
        add_object_link(
            returned_objects,
            "tool_call",
            trace_object_id(event, "tool_call_object_id"),
        )
    if event_type == "tool_result":
        add_object_link(
            used_objects,
            "tool_call",
            trace_object_id(event, "tool_call_object_id"),
        )
        add_object_link(
            returned_objects,
            "tool_result",
            trace_object_id(event, "tool_result_object_id"),
        )
    return used_objects, returned_objects


def add_object_links(
    links: list[dict[str, str]],
    kind: str,
    object_ids: Any,
) -> None:
    if not isinstance(object_ids, (list, tuple)):
        return
    for object_id in object_ids:
        add_object_link(links, kind, object_id if isinstance(object_id, str) else None)


def add_object_link(
    links: list[dict[str, str]],
    kind: str,
    object_id: str | None,
) -> None:
    if not object_id:
        return
    link = {"kind": kind, "id": object_id}
    if link not in links:
        links.append(link)


def trace_object_id(event: dict[str, Any], field: str) -> ObjectId | None:
    value = event.get(field)
    if isinstance(value, str) and value.startswith("sha256:"):
        return value
    return None


def add_event_link(links: list[ObjectId], object_id: ObjectId | None) -> None:
    if object_id and object_id not in links:
        links.append(object_id)
