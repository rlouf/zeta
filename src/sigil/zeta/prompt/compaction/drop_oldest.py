"""Last-resort compaction: drop the oldest historical messages."""

from __future__ import annotations

from typing import Any

from ..budget import measure
from ..components import PromptComponent


class DropOldestPromptTransform:
    """Drop the oldest historical messages until the prompt fits the budget."""

    producer = "PromptDropOldest:v1"

    def __init__(self, *, max_tokens: int) -> None:
        self.max_tokens = max_tokens

    def apply(self, components: list[PromptComponent]) -> list[PromptComponent]:
        output = list(components)
        while measure(output).total_tokens > self.max_tokens:
            remaining = without_oldest_historical_message(output)
            if remaining is None:
                break
            output = remaining
        return output


def without_oldest_historical_message(
    components: list[PromptComponent],
) -> list[PromptComponent] | None:
    """Drop the oldest historical message, taking its tool results along.

    A dangling tool-role message without its assistant tool_calls makes the
    request invalid, so a dropped call drops its results too.
    """
    for index, component in enumerate(components):
        if not component.data.get("historical") or component.message is None:
            continue
        call_ids = message_tool_call_ids(component.message)
        return components[:index] + [
            candidate
            for candidate in components[index + 1 :]
            if not is_result_for_calls(candidate, call_ids)
        ]
    return None


def message_tool_call_ids(message: dict[str, Any]) -> set[str]:
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return set()
    return {
        str(call.get("id") or "")
        for call in tool_calls
        if isinstance(call, dict) and call.get("id")
    }


def is_result_for_calls(component: PromptComponent, call_ids: set[str]) -> bool:
    if not call_ids or component.message is None:
        return False
    if component.message.get("role") != "tool":
        return False
    return str(component.message.get("tool_call_id") or "") in call_ids
