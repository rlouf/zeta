"""Structural tool-result trimming for prompt compaction."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

from ..budget import render_stub
from ..components import PromptComponent

STRUCTURAL_TRIM_TOOL_NAMES = frozenset({"read", "grep"})
DEFAULT_MAX_CONTENT_CHARS = 120_000


class StructuralTrimPromptTransform:
    """Replace bulky timeline mechanics with trace-linked compact messages."""

    producer = "PromptStructuralTrim:v1"

    def __init__(
        self,
        *,
        max_content_chars: int = DEFAULT_MAX_CONTENT_CHARS,
        preserve_current_tool_results: bool = True,
    ) -> None:
        self.max_content_chars = max_content_chars
        self.preserve_current_tool_results = preserve_current_tool_results

    def apply(self, components: list[PromptComponent]) -> list[PromptComponent]:
        return [self.transform_component(component) for component in components]

    def transform_component(self, component: PromptComponent) -> PromptComponent:
        if not self.should_trim(component):
            return component
        return trimmed_component(component)

    def should_trim(self, component: PromptComponent) -> bool:
        if component.message is None:
            return False
        if self.preserve_current_tool_results and component.kind == "tool_result":
            return False
        if not is_tool_result_component(component):
            return False
        # Limit default structural trimming to reproducible read/search outputs.
        # Trimming arbitrary tools can hide non-recoverable evidence from the model.
        if tool_name(component) not in STRUCTURAL_TRIM_TOOL_NAMES:
            return False
        return message_content_length(component.message) > self.max_content_chars


def trimmed_component(component: PromptComponent) -> PromptComponent:
    assert component.message is not None
    source_id = component.object_id
    trimmed_message = trimmed_message_projection(component)
    data: dict[str, Any] = {
        "method": "structural_trim",
        "source_kind": component.kind,
        "trim": structural_trim_payload(component),
        "message": trimmed_message,
    }
    if source_id is not None:
        data["source_object_id"] = source_id
    return replace(
        component,
        kind="compacted_context",
        representation="stub",
        source_object_id=source_id,
        data=data,
        message=trimmed_message,
        links=(source_id,) if source_id is not None else (),
        object_id=None,
    )


def is_tool_result_component(component: PromptComponent) -> bool:
    source_event_value = source_event(component)
    if source_event_value is not None:
        return str(source_event_value.get("type") or "") == "tool_result"
    assert component.message is not None
    return is_tool_result_projection(component.message)


def is_tool_result_projection(message: dict[str, Any]) -> bool:
    if message.get("role") == "tool":
        return True
    content = message.get("content")
    return isinstance(content, str) and content.startswith("Tool result JSON:\n")


def message_content_length(message: dict[str, Any]) -> int:
    content = message.get("content")
    if not isinstance(content, str):
        return 0
    return len(content)


def trimmed_message_projection(component: PromptComponent) -> dict[str, Any]:
    assert component.message is not None
    if component.message.get("role") == "tool":
        return {
            "role": "tool",
            "tool_call_id": tool_call_id(component),
            "content": render_stub(component),
        }
    return {
        "role": "user",
        "content": render_stub(component),
    }


def source_event(component: PromptComponent) -> dict[str, Any] | None:
    value = component.data.get("source_event")
    return value if isinstance(value, dict) else None


def tool_call_id(component: PromptComponent) -> str:
    event = source_event(component)
    if event is not None:
        tool_call_id_value = str(event.get("tool_call_id") or event.get("id") or "")
        if tool_call_id_value:
            return tool_call_id_value
    if component.message is None:
        return ""
    return str(component.message.get("tool_call_id") or "")


def tool_name(component: PromptComponent) -> str:
    event = source_event(component)
    if event is not None:
        name = str(event.get("tool_name") or event.get("name") or "")
        if name:
            return name
    return str(component.data.get("source_tool_name") or "")


def tool_result(component: PromptComponent) -> dict[str, Any] | None:
    event = source_event(component)
    if event is None:
        return None
    result = event.get("result")
    return result if isinstance(result, dict) else None


def structural_trim_payload(component: PromptComponent) -> dict[str, Any]:
    """Describe the trimmed content so the trace records what was elided."""
    assert component.message is not None
    content = str(component.message.get("content") or "")
    call_id = tool_call_id(component)
    parsed_result = tool_result(component)
    if component.message.get("role") == "tool":
        parsed_result = parsed_result or parse_json_object(content)
    else:
        event_payload = parse_json_object(content.removeprefix("Tool result JSON:\n"))
        event_payload = event_payload or {}
        call_id = call_id or str(event_payload.get("tool_call_id") or "")
        event_result = event_payload.get("result")
        if parsed_result is None and isinstance(event_result, dict):
            parsed_result = event_result
    payload: dict[str, Any] = {
        "trimmed": True,
        "trim_method": "structural",
        "raw_content_sha256": sha256_text(content),
        "raw_content_chars": len(content),
        "raw_content_bytes": len(content.encode("utf-8")),
    }
    if component.object_id is not None:
        payload["source_object_id"] = component.object_id
    if call_id:
        payload["tool_call_id"] = call_id
    if parsed_result is not None:
        payload["tool_result"] = trimmed_tool_result(parsed_result)
    return payload


def trimmed_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    trimmed: dict[str, Any] = {}
    for key in ("ok", "metadata", "error"):
        value = result.get(key)
        if value is not None:
            trimmed[key] = value
    content_items = trimmed_content_items(result.get("content"))
    if content_items:
        trimmed["content"] = content_items
    return trimmed


def trimmed_content_items(content: Any) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        return []
    items = []
    for item in content:
        if isinstance(item, dict):
            items.append(trimmed_content_item(item))
    return items


def trimmed_content_item(item: dict[str, Any]) -> dict[str, Any]:
    text = item.get("text")
    if isinstance(text, str):
        return {
            "type": str(item.get("type") or "text"),
            "text_sha256": sha256_text(text),
            "text_chars": len(text),
            "text_lines": line_count(text),
        }
    return {"type": str(item.get("type") or "unknown")}


def parse_json_object(raw: str) -> dict[str, Any] | None:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def line_count(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)
