"""Return schema derivation for authored agents."""

from copy import deepcopy
from typing import Any

from zeta.authoring.schemas import EventRegistry
from zeta.authoring.spec import AgentSpec

MAX_RETURNED_EVENTS = 100


def _hoist_local_definitions(
    schema: dict[str, Any],
    *,
    branch_index: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Move payload-local definitions to the generated document root."""
    payload = deepcopy(schema)
    local_definitions = payload.pop("$defs", None)
    if not isinstance(local_definitions, dict):
        return payload, {}

    renamed = {name: f"event_{branch_index}_{name}" for name in local_definitions}

    def rewrite(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: (
                    f"#/$defs/{renamed[ref_name]}"
                    if key == "$ref"
                    and isinstance(item, str)
                    and item.startswith("#/$defs/")
                    and (ref_name := item.removeprefix("#/$defs/")) in renamed
                    else rewrite(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        return value

    return rewrite(payload), {
        renamed[name]: rewrite(definition)
        for name, definition in local_definitions.items()
    }


def derive_returns_schema(
    spec: AgentSpec,
    events: EventRegistry | None = None,
) -> dict[str, Any] | None:
    """Derive the ordered zero-to-many event schema for an agent return."""
    if not spec.returns:
        return None
    branches = []
    definitions: dict[str, Any] = {}
    for branch_index, event_type in enumerate(spec.returns):
        payload_schema = events.schema(event_type) if events is not None else None
        payload, payload_definitions = _hoist_local_definitions(
            payload_schema or {},
            branch_index=branch_index,
        )
        definitions.update(payload_definitions)
        branches.append(
            {
                "type": "object",
                "required": ["type", "payload"],
                "properties": {
                    "type": {"type": "string", "const": event_type},
                    "payload": payload,
                },
                "additionalProperties": False,
            }
        )
    schema = {
        "type": "object",
        "required": ["events"],
        "properties": {
            "events": {
                "type": "array",
                "items": {"anyOf": branches},
                "maxItems": MAX_RETURNED_EVENTS,
            }
        },
        "additionalProperties": False,
    }
    if definitions:
        schema["$defs"] = definitions
    return schema
