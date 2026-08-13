"""Runtime tools that create durable event-control requests."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from jsonschema import Draft202012Validator

from zeta.capabilities.executors import CapabilityFunction
from zeta.capabilities.profiles import identity_arguments
from zeta.capabilities.registry import (
    CapabilityToolRoute,
    CapabilityToolSchema,
    model_descriptor,
)
from zeta.capabilities.types import Capability, CapabilityId
from zeta.ids import publish_event_handle, wait_handle

PUBLISH_EVENT_CAPABILITY_ID = "zeta.publish_event"
WAIT_FOR_CAPABILITY_ID = "zeta.wait_for"
CANCEL_CAPABILITY_ID = "zeta.cancel"

PUBLISH_EVENT_SPEC = Capability(
    CapabilityId("zeta", "publish_event"),
    "Request an event when this agent attempt completes successfully.",
    {
        "type": "object",
        "required": ["event_type", "payload"],
        "properties": {
            "event_type": {"type": "string"},
            "payload": {"type": "object"},
            "at": {"type": "string"},
        },
        "additionalProperties": False,
    },
)

WAIT_FOR_SPEC = Capability(
    CapabilityId("zeta", "wait_for"),
    "End this run and resume when a matching event arrives.",
    {
        "type": "object",
        "required": ["event_type"],
        "properties": {
            "event_type": {"type": "string", "minLength": 1},
            "fields": {"type": "object"},
            "deadline": {"type": "string"},
        },
        "additionalProperties": False,
    },
)

CANCEL_SPEC = Capability(
    CapabilityId("zeta", "cancel"),
    "Cancel an active wait or pending deferred publication from this session.",
    {
        "type": "object",
        "required": ["handle"],
        "properties": {
            "handle": {
                "type": "string",
                "pattern": "^(?:wait|pub)_.+$",
            },
            "reason": {"type": "string", "minLength": 1},
        },
        "additionalProperties": False,
    },
)


@dataclass(frozen=True)
class PublishEventRequest:
    """Keep a requested event provisional until its attempt succeeds."""

    handle: str
    event_type: str
    payload: dict[str, Any]
    at: str | None
    position: int


@dataclass(frozen=True)
class WaitRequest:
    """Keep a requested wait provisional until its attempt succeeds."""

    handle: str
    event_type: str
    fields: dict[str, Any]
    deadline: str | None
    position: int


@dataclass(frozen=True)
class CancelRequest:
    """Keep a cancellation provisional until its attempt succeeds."""

    handle: str
    reason: str | None
    source_agent_id: str
    source_session_id: str
    position: int


@dataclass(frozen=True)
class EventToolBindings:
    """Limit event tools to the provisional request lists for one attempt."""

    position: int
    publishable_events: Mapping[str, dict[str, Any] | None]
    source_queue_item_id: str | None
    source_agent_id: str | None
    source_session_id: str | None
    publish_event_requests: list[PublishEventRequest]
    wait_requests: list[WaitRequest]
    cancel_requests: list[CancelRequest]


def bind_event_tools(bindings: EventToolBindings) -> dict[str, CapabilityFunction]:
    """Bind attempt state so event tools cannot commit effects themselves."""
    return {
        PUBLISH_EVENT_CAPABILITY_ID: lambda params: request_published_event(
            params,
            bindings=bindings,
        ),
        WAIT_FOR_CAPABILITY_ID: lambda params: request_wait(
            params,
            bindings=bindings,
        ),
        CANCEL_CAPABILITY_ID: lambda params: request_cancellation(
            params,
            bindings=bindings,
        ),
    }


def publish_event_tool_schema(
    tool_schema: CapabilityToolSchema,
) -> CapabilityToolSchema:
    return _add_reserved_tool(tool_schema, PUBLISH_EVENT_SPEC)


def wait_for_tool_schema(
    tool_schema: CapabilityToolSchema,
) -> CapabilityToolSchema:
    return _add_reserved_tool(tool_schema, WAIT_FOR_SPEC)


def cancel_tool_schema(
    tool_schema: CapabilityToolSchema,
) -> CapabilityToolSchema:
    return _add_reserved_tool(tool_schema, CANCEL_SPEC)


def _add_reserved_tool(
    tool_schema: CapabilityToolSchema,
    spec: Capability,
) -> CapabilityToolSchema:
    name = spec.id.name
    existing = tool_schema.routes.get(name)
    if existing is not None:
        raise ValueError(
            f"reserved tool name {name!r} is already in use by "
            f"{existing.capability_id!r}"
        )
    return CapabilityToolSchema(
        routes={
            **tool_schema.routes,
            name: CapabilityToolRoute(
                capability_id=spec.id.canonical(),
                input_schema=spec.input_schema,
                adapt_arguments=identity_arguments,
            ),
        },
        descriptors=[
            *tool_schema.descriptors,
            model_descriptor(name, spec.description, spec.input_schema),
        ],
    )


def request_published_event(
    params: dict[str, Any],
    *,
    bindings: EventToolBindings,
) -> dict[str, Any]:
    event_type = params["event_type"]
    schema = bindings.publishable_events.get(event_type)
    if event_type not in bindings.publishable_events:
        return event_tool_error(
            "undeclared-event-type",
            f"the agent does not list event {event_type!r} in publishes",
        )
    payload = params["payload"]
    if schema is not None:
        errors = sorted(
            Draft202012Validator(schema).iter_errors(payload),
            key=lambda error: list(error.path),
        )
        if errors:
            return event_tool_error("invalid-event-payload", errors[0].message)
    at = normalized_publish_time(params.get("at"))
    if isinstance(at, dict):
        return at
    if bindings.source_queue_item_id is None:
        return event_tool_error(
            "missing-event-source",
            "the run does not have a source queue item",
        )
    handle = publish_event_handle(
        bindings.source_queue_item_id,
        bindings.position,
    )
    bindings.publish_event_requests.append(
        PublishEventRequest(
            handle=handle,
            event_type=event_type,
            payload=dict(payload),
            at=at,
            position=bindings.position,
        )
    )
    return {"ok": True, "handle": handle}


def request_wait(
    params: dict[str, Any],
    *,
    bindings: EventToolBindings,
) -> dict[str, Any]:
    if bindings.source_queue_item_id is None:
        return event_tool_error(
            "missing-wait-source",
            "the run does not have a source queue item",
        )
    deadline = normalized_wait_deadline(params.get("deadline"))
    if isinstance(deadline, dict):
        return deadline
    handle = wait_handle(bindings.source_queue_item_id, bindings.position)
    bindings.wait_requests.append(
        WaitRequest(
            handle=handle,
            event_type=params["event_type"],
            fields=dict(params.get("fields") or {}),
            deadline=deadline,
            position=bindings.position,
        )
    )
    return {"ok": True, "handle": handle, "stop": True}


def request_cancellation(
    params: dict[str, Any],
    *,
    bindings: EventToolBindings,
) -> dict[str, Any]:
    if bindings.source_agent_id is None or bindings.source_session_id is None:
        return event_tool_error(
            "missing-cancel-source",
            "the run does not have an authored agent session",
        )
    handle = params["handle"]
    bindings.cancel_requests.append(
        CancelRequest(
            handle=handle,
            reason=params.get("reason"),
            source_agent_id=bindings.source_agent_id,
            source_session_id=bindings.source_session_id,
            position=bindings.position,
        )
    )
    return {"ok": True, "handle": handle, "status": "requested"}


def normalized_wait_deadline(value: Any) -> str | None | dict[str, Any]:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return event_tool_error(
            "invalid-wait-deadline",
            "deadline must be an ISO 8601 date-time with a UTC offset",
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return event_tool_error(
            "invalid-wait-deadline",
            "deadline must include a UTC offset",
        )
    return parsed.astimezone(UTC).isoformat()


def normalized_publish_time(value: Any) -> str | None | dict[str, Any]:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return event_tool_error(
            "invalid-publish-time",
            "at must be an ISO 8601 date-time with a UTC offset",
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return event_tool_error(
            "invalid-publish-time",
            "at must include a UTC offset",
        )
    return parsed.astimezone(UTC).isoformat()


def event_tool_error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}
