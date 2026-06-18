"""Typed runtime events used by the Zeta turn loop."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ModelRuntimeEvent:
    content: str = ""
    reasoning: str = ""
    tool_calls: tuple[dict[str, Any], ...] = ()

    @classmethod
    def from_assistant(cls, assistant: dict[str, Any]) -> ModelRuntimeEvent:
        from zeta.loop import assistant_tool_calls

        content = assistant.get("content")
        reasoning = assistant.get("reasoning_content")
        return cls(
            content=content if isinstance(content, str) else "",
            reasoning=reasoning if isinstance(reasoning, str) else "",
            tool_calls=tuple(assistant_tool_calls(assistant)),
        )

    def to_event(self) -> dict[str, Any]:
        event: dict[str, Any] = {"type": "model"}
        if self.reasoning:
            event["reasoning"] = self.reasoning
        if self.content:
            event["content"] = self.content
        if self.tool_calls:
            event["tool_calls"] = list(self.tool_calls)
        return event


@dataclass(frozen=True)
class ToolCallRuntimeEvent:
    tool_call: Any
    caused_by: str | None = None

    def to_event(self) -> dict[str, Any]:
        event: dict[str, Any] = {
            "type": "tool_call",
            "id": self.tool_call.call_id,
            "tool_call_id": self.tool_call.call_id,
            "status": "pending",
            "name": self.tool_call.name,
            "input": self.tool_call.params,
            "arguments": self.tool_call.raw_arguments,
        }
        if self.caused_by is not None:
            event["caused_by"] = self.caused_by
        return event


@dataclass(frozen=True)
class ToolResultRuntimeEvent:
    call_id: str
    name: str
    result: dict[str, Any]
    event_id: str | None = None
    capability_id: str = ""
    model_telemetry: dict[str, Any] | None = None
    prompt_trace: dict[str, Any] | None = None

    def to_event(self) -> dict[str, Any]:
        from zeta.loop import (
            ensure_event_id,
            normalized_tool_result,
            tool_result_status,
        )

        event: dict[str, Any] = {
            "type": "tool_result",
            "tool_call_id": self.call_id,
            "status": tool_result_status(self.result),
            "name": self.name,
            "result": normalized_tool_result(self.name, self.result),
        }
        if self.event_id is not None:
            event["id"] = self.event_id
        ensure_event_id(event)
        if self.capability_id:
            event["capability_id"] = self.capability_id
        if self.model_telemetry:
            event["model_telemetry"] = dict(self.model_telemetry)
        if self.prompt_trace is not None:
            event["prompt_trace"] = self.prompt_trace
        return event


@dataclass(frozen=True)
class TurnAbortedRuntimeEvent:
    event_id: str
    reason: str
    caused_by: str | None = None

    def to_event(self) -> dict[str, Any]:
        message = self.reason.replace("_", " ")
        event: dict[str, Any] = {
            "type": "turn_aborted",
            "id": self.event_id,
            "reason": self.reason,
            "content": f"(turn aborted: {message})",
        }
        if self.caused_by is not None:
            event["caused_by"] = self.caused_by
        return event
