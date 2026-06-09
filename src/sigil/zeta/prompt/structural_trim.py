"""Structural tool-result trimming for prompt compaction."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from typing import Any

from .budget import render_stub
from .components import PromptComponent

STRUCTURAL_TRIM_TOOL_NAMES = frozenset({"read", "grep"})
DEFAULT_MAX_CONTENT_CHARS = 120_000


class StructuralTrimPromptTransform:
    """Replace bulky transcript mechanics with trace-linked compact messages."""

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
        if component.object_id is None or component.message is None:
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
    assert component.object_id is not None
    assert component.message is not None
    trimmed_message = trimmed_message_projection(
        component,
        source_object_id=component.object_id,
    )
    return replace(
        component,
        kind="compacted_context",
        representation="stub",
        source_object_id=component.object_id,
        data={
            "method": "structural_trim",
            "source_kind": component.kind,
            "source_object_id": component.object_id,
            "message": trimmed_message,
        },
        message=trimmed_message,
        links=(component.object_id,),
        object_id=None,
        ref_name=None,
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


def trimmed_message_projection(
    component: PromptComponent,
    *,
    source_object_id: str,
) -> dict[str, Any]:
    assert component.message is not None
    message = component.message
    if message.get("role") == "tool":
        return trim_tool_role_message(component, source_object_id=source_object_id)
    return trim_user_tool_json_message(component, source_object_id=source_object_id)


def trim_tool_role_message(
    component: PromptComponent,
    *,
    source_object_id: str,
) -> dict[str, Any]:
    assert component.message is not None
    message = component.message
    content = str(message.get("content") or "")
    stub = render_stub(component)
    structural_trim_payload(
        content,
        source_object_id=source_object_id,
        tool_call_id=tool_call_id(component),
        parsed_result=tool_result(component) or parse_json_object(content),
    )
    return {
        "role": "tool",
        "tool_call_id": tool_call_id(component),
        "content": stub,
    }


def trim_user_tool_json_message(
    component: PromptComponent,
    *,
    source_object_id: str,
) -> dict[str, Any]:
    assert component.message is not None
    message = component.message
    content = str(message.get("content") or "")
    raw_json = content.removeprefix("Tool result JSON:\n")
    event = parse_json_object(raw_json)
    event_payload = event or {}
    result = (
        event_payload.get("result")
        if isinstance(event_payload.get("result"), dict)
        else None
    )
    structural_trim_payload(
        content,
        source_object_id=source_object_id,
        tool_call_id=tool_call_id(component)
        or str(event_payload.get("tool_call_id") or ""),
        parsed_result=tool_result(component) or result,
    )
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


def structural_trim_payload(
    raw_content: str,
    *,
    source_object_id: str,
    tool_call_id: str,
    parsed_result: dict[str, Any] | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "trimmed": True,
        "trim_method": "structural",
        "source_object_id": source_object_id,
        "raw_content_sha256": sha256_text(raw_content),
        "raw_content_chars": len(raw_content),
        "raw_content_bytes": len(raw_content.encode("utf-8")),
    }
    if tool_call_id:
        payload["tool_call_id"] = tool_call_id
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


def compact_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def line_count(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)
