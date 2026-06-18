"""Typed runtime events used by the Zeta turn loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from zeta.events import DraftEvent, Event
from zeta.substrate import trace_object_id

EVENT_IDEMPOTENT_TYPES = frozenset(
    {
        "zeta.model_call.completed",
        "zeta.tool_call.started",
        "zeta.tool_call.completed",
        "zeta.tool_call.failed",
    }
)
RUNTIME_DURABLE_EXCLUDED_KEYS = {
    "id",
    "type",
    "time",
    "session",
    "source",
    "caused_by",
    "prompt_trace",
    "tool_call_object_id",
    "tool_call_object_ids",
    "tool_result_object_id",
}


@dataclass(frozen=True)
class ModelRuntimeEvent:
    content: str = ""
    reasoning: str = ""
    tool_calls: tuple[dict[str, Any], ...] = ()
    _event: dict[str, Any] | None = field(default=None, repr=False, compare=False)

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

    @classmethod
    def from_event(cls, event: dict[str, Any]) -> ModelRuntimeEvent:
        return cls(_event=dict(event))

    def to_event(self) -> dict[str, Any]:
        if self._event is not None:
            return dict(self._event)
        event: dict[str, Any] = {"type": "model"}
        if self.reasoning:
            event["reasoning"] = self.reasoning
        if self.content:
            event["content"] = self.content
        if self.tool_calls:
            event["tool_calls"] = list(self.tool_calls)
        return event

    def to_durable(
        self,
        *,
        session_id: str | None,
        turn_id: str | None,
    ) -> DraftEvent:
        return runtime_event_draft(
            self.to_event(),
            session_id=session_id,
            turn_id=turn_id,
        )


@dataclass(frozen=True)
class ToolCallRuntimeEvent:
    tool_call: Any
    caused_by: str | None = None
    _event: dict[str, Any] | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_event(cls, event: dict[str, Any]) -> ToolCallRuntimeEvent:
        return cls(tool_call=None, _event=dict(event))

    def to_event(self) -> dict[str, Any]:
        if self._event is not None:
            return dict(self._event)
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

    def to_durable(
        self,
        *,
        session_id: str | None,
        turn_id: str | None,
    ) -> DraftEvent:
        return runtime_event_draft(
            self.to_event(),
            session_id=session_id,
            turn_id=turn_id,
        )


@dataclass(frozen=True)
class ToolResultRuntimeEvent:
    call_id: str
    name: str
    result: dict[str, Any]
    event_id: str | None = None
    capability_id: str = ""
    model_telemetry: dict[str, Any] | None = None
    prompt_trace: dict[str, Any] | None = None
    _event: dict[str, Any] | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_event(cls, event: dict[str, Any]) -> ToolResultRuntimeEvent:
        return cls(call_id="", name="", result={}, _event=dict(event))

    def to_event(self) -> dict[str, Any]:
        from zeta.loop import (
            ensure_event_id,
            normalized_tool_result,
            tool_result_status,
        )

        if self._event is not None:
            return dict(self._event)
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

    def to_durable(
        self,
        *,
        session_id: str | None,
        turn_id: str | None,
    ) -> DraftEvent:
        return runtime_event_draft(
            self.to_event(),
            session_id=session_id,
            turn_id=turn_id,
        )


@dataclass(frozen=True)
class TurnAbortedRuntimeEvent:
    event_id: str
    reason: str
    caused_by: str | None = None
    _event: dict[str, Any] | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_event(cls, event: dict[str, Any]) -> TurnAbortedRuntimeEvent:
        return cls(
            event_id=str(event.get("id") or ""),
            reason=str(event.get("reason") or "aborted"),
            _event=dict(event),
        )

    def to_event(self) -> dict[str, Any]:
        if self._event is not None:
            return dict(self._event)
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

    def to_durable(
        self,
        *,
        session_id: str | None,
        turn_id: str | None,
    ) -> DraftEvent:
        return runtime_event_draft(
            self.to_event(),
            session_id=session_id,
            turn_id=turn_id,
        )


RuntimeEvent = (
    ModelRuntimeEvent
    | ToolCallRuntimeEvent
    | ToolResultRuntimeEvent
    | TurnAbortedRuntimeEvent
)


def runtime_event_from_event(event: dict[str, Any]) -> RuntimeEvent | None:
    event_type = str(event.get("type") or "")
    if event_type == "model":
        return ModelRuntimeEvent.from_event(event)
    if event_type == "tool_call":
        return ToolCallRuntimeEvent.from_event(event)
    if event_type == "tool_result":
        return ToolResultRuntimeEvent.from_event(event)
    if event_type == "turn_aborted":
        return TurnAbortedRuntimeEvent.from_event(event)
    return None


def runtime_event_from_durable(event: Event) -> RuntimeEvent | None:
    from zeta.timeline import timeline_event_from_durable_event

    projected = timeline_event_from_durable_event(event)
    if not projected:
        return None
    return runtime_event_from_event(projected)


def runtime_event_draft(
    event: dict[str, Any],
    *,
    session_id: str | None,
    turn_id: str | None,
) -> DraftEvent:
    event_type = str(event.get("type") or "")
    caused_by = (
        event.get("caused_by") if isinstance(event.get("caused_by"), str) else None
    )
    event_id = event.get("id") if isinstance(event.get("id"), str) else None
    if event_type == "model":
        return model_called_draft(
            payload=model_durable_payload(event),
            turn_id=turn_id,
            session_id=session_id,
            caused_by=caused_by,
            event_id=event_id,
        )
    if event_type in {"tool_call", "tool_result"}:
        return tool_called_draft(
            payload=tool_durable_payload(event),
            turn_id=turn_id,
            session_id=session_id,
            caused_by=caused_by,
            event_id=event_id,
        )
    if event_type == "turn_aborted":
        payload = durable_payload(event)
        payload["_timeline_type"] = "turn_aborted"
        payload.setdefault("reason", "aborted")
        return DraftEvent(
            event_type="zeta.turn.failed",
            source="zeta",
            payload=payload,
            idempotency_key=None,
            caused_by=caused_by,
            session_id=session_id,
            turn_id=turn_id,
        )
    return DraftEvent(
        event_type=event_type,
        source="zeta",
        payload=durable_payload(event),
        idempotency_key=None,
        caused_by=caused_by,
        session_id=session_id,
        turn_id=turn_id,
    )


def model_called_draft(
    *,
    payload: dict[str, Any],
    turn_id: str | None,
    session_id: str | None,
    caused_by: str | None = None,
    event_id: str | None = None,
) -> DraftEvent:
    return durable_event_draft(
        "zeta.model_call.completed",
        payload=payload,
        turn_id=turn_id,
        session_id=session_id,
        caused_by=caused_by,
        event_id=event_id,
    )


def tool_called_draft(
    *,
    payload: dict[str, Any],
    turn_id: str | None,
    session_id: str | None,
    caused_by: str | None = None,
    event_id: str | None = None,
) -> DraftEvent:
    return durable_event_draft(
        tool_call_event_type(payload),
        payload=payload,
        turn_id=turn_id,
        session_id=session_id,
        caused_by=caused_by,
        event_id=event_id,
    )


def tool_call_event_type(payload: dict[str, Any]) -> str:
    if payload.get("_timeline_type") == "tool_call":
        return "zeta.tool_call.started"
    if tool_call_failed(payload):
        return "zeta.tool_call.failed"
    return "zeta.tool_call.completed"


def tool_call_failed(payload: dict[str, Any]) -> bool:
    result = payload.get("result")
    return isinstance(result, dict) and result.get("ok") is False


def durable_event_draft(
    event_type: str,
    *,
    payload: dict[str, Any],
    turn_id: str | None,
    session_id: str | None,
    caused_by: str | None,
    event_id: str | None,
) -> DraftEvent:
    return DraftEvent(
        event_type=event_type,
        source="zeta",
        payload=payload,
        idempotency_key=event_idempotency_key(event_type, event_id),
        caused_by=caused_by,
        session_id=session_id,
        turn_id=turn_id,
    )


def event_idempotency_key(event_type: str, event_id: str | None) -> str | None:
    if event_type not in EVENT_IDEMPOTENT_TYPES or not event_id:
        return None
    return f"{event_type}:{event_id}"


def model_durable_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = durable_payload(event)
    payload["_timeline_type"] = "model"
    used_objects, returned_objects = model_durable_object_links(event)
    add_link_payload(payload, used_objects, returned_objects)
    return payload


def tool_durable_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = durable_payload(event)
    payload["_timeline_type"] = str(event.get("type") or "")
    used_objects, returned_objects = tool_durable_object_links(event)
    add_link_payload(payload, used_objects, returned_objects)
    return payload


def durable_payload(event: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in event.items()
        if key not in RUNTIME_DURABLE_EXCLUDED_KEYS
    }


def add_link_payload(
    payload: dict[str, Any],
    used_objects: list[dict[str, str]],
    returned_objects: list[dict[str, str]],
) -> None:
    if used_objects:
        payload["used_objects"] = used_objects
    if returned_objects:
        payload["returned_objects"] = returned_objects


def model_durable_object_links(
    event: dict[str, Any],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    used_objects: list[dict[str, str]] = []
    returned_objects: list[dict[str, str]] = []
    prompt_trace = event.get("prompt_trace")
    if isinstance(prompt_trace, dict):
        add_durable_object_link(
            used_objects,
            "prompt",
            trace_object_id(prompt_trace, "prompt_object_id"),
        )
        add_durable_object_link(
            returned_objects,
            "assistant_message",
            trace_object_id(prompt_trace, "assistant_message_object_id"),
        )
    add_durable_object_links(
        returned_objects,
        "tool_call",
        event.get("tool_call_object_ids"),
    )
    add_durable_object_link(
        returned_objects,
        "tool_call",
        trace_object_id(event, "tool_call_object_id"),
    )
    return used_objects, returned_objects


def tool_durable_object_links(
    event: dict[str, Any],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    event_type = str(event.get("type") or "")
    if event_type == "tool_result":
        return tool_result_durable_object_links(event)
    if event_type != "tool_call":
        return [], []
    returned_objects: list[dict[str, str]] = []
    add_durable_object_link(
        returned_objects,
        "tool_call",
        trace_object_id(event, "tool_call_object_id"),
    )
    return [], returned_objects


def tool_result_durable_object_links(
    event: dict[str, Any],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    used_objects: list[dict[str, str]] = []
    returned_objects: list[dict[str, str]] = []
    add_durable_object_link(
        used_objects,
        "tool_call",
        trace_object_id(event, "tool_call_object_id"),
    )
    add_durable_object_link(
        returned_objects,
        "tool_result",
        trace_object_id(event, "tool_result_object_id"),
    )
    return used_objects, returned_objects


def add_durable_object_links(
    links: list[dict[str, str]],
    kind: str,
    object_ids: Any,
) -> None:
    if not isinstance(object_ids, (list, tuple)):
        return
    for object_id in object_ids:
        add_durable_object_link(
            links,
            kind,
            object_id if isinstance(object_id, str) else None,
        )


def add_durable_object_link(
    links: list[dict[str, str]],
    kind: str,
    object_id: str | None,
) -> None:
    if not object_id:
        return
    link = {"kind": kind, "id": object_id}
    if link not in links:
        links.append(link)
