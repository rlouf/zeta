"""Return schema derivation for authored agents."""

from typing import Any

from agents.events import EventRegistry
from agents.spec import AgentSpec


def derive_returns_schema(
    spec: AgentSpec,
    events: EventRegistry | None = None,
) -> dict[str, Any] | None:
    """Derive the per-event schema for events this spec may return."""
    if not spec.returns:
        return None
    branches = []
    for event_type in spec.returns:
        payload_schema = events.schema(event_type) if events is not None else None
        branches.append(
            {
                "type": "object",
                "required": ["type", "payload"],
                "properties": {
                    "type": {"const": event_type},
                    "payload": payload_schema or {},
                },
                "additionalProperties": False,
            }
        )
    return {"type": "object", "anyOf": branches}
