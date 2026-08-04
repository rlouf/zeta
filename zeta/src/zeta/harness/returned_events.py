"""Generate and durably publish authored agent return events."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from jsonschema import Draft202012Validator

from zeta.authoring.returns import derive_returns_schema
from zeta.authoring.schemas import EventRegistry
from zeta.authoring.spec import AgentSpec
from zeta.events import DraftEvent, Event
from zeta.journal.views import draft_event_view, event_view
from zeta.loop.config import AgentConfig
from zeta.loop.outcomes import AgentRunResult, agent_run_result_payload
from zeta.models.types import ModelRequest

StructuredOutputRunner = Callable[..., Awaitable[dict[str, Any]]]
AGENT_RETURN_RESPONSE_NAME = "zeta_agent_return"


class ReturnedEventInvocation(Protocol):
    """The durable event publication boundary for a completed agent turn."""

    triggering_event: Event

    async def publish(self, draft: DraftEvent) -> Event: ...


class ReturnedEventPublisher:
    """Own the final no-tools structured result step for authored agents."""

    def __init__(
        self,
        event_registry: EventRegistry,
        structured_output: StructuredOutputRunner,
    ) -> None:
        self.event_registry = event_registry
        self.structured_output = structured_output

    async def publish(
        self,
        spec: AgentSpec,
        result: AgentRunResult,
        invocation: ReturnedEventInvocation,
        *,
        objective: str,
        config: AgentConfig,
    ) -> dict[str, Any]:
        schema = derive_returns_schema(spec, self.event_registry)
        if schema is None:
            return agent_run_result_payload(result)
        data = await self.structured_output(
            structured_return_messages(
                spec,
                result,
                invocation.triggering_event,
                objective=objective,
            ),
            ModelRequest(
                api=config.model_api,
                model=config.model_name,
                url=config.model_url,
                thinking=config.thinking,
            ),
            schema=schema,
            response_name=AGENT_RETURN_RESPONSE_NAME,
        )
        Draft202012Validator(schema).validate(data)
        event_type = data.get("type")
        payload = data.get("payload")
        if not isinstance(event_type, str) or not isinstance(payload, dict):
            raise RuntimeError("structured agent return must include type and payload")
        published = await invocation.publish(
            DraftEvent(
                event_type,
                f"agent:{spec.slug}",
                payload,
                idempotency_key=agent_return_idempotency_key(
                    invocation.triggering_event,
                    spec,
                ),
                caused_by=invocation.triggering_event.id,
            )
        )
        return {
            **agent_run_result_payload(result),
            "returned_events": [event_view(published)],
        }


def structured_return_messages(
    spec: AgentSpec,
    result: AgentRunResult,
    triggering_event: Event,
    *,
    objective: str,
) -> list[dict[str, Any]]:
    payload = {
        "allowed_return_types": list(spec.returns),
        "triggering_event": event_view(triggering_event),
        "objective": objective,
        "agent_final_answer": result.final_answer,
        "agent_events": [draft_event_view(event) for event in result.events],
    }
    return [
        {
            "role": "system",
            "content": (
                "Convert the agent result into exactly one returned event. "
                "Return only JSON matching the provided schema. Do not call tools."
            ),
        },
        {"role": "user", "content": json.dumps(payload, sort_keys=True)},
    ]


def agent_return_idempotency_key(event: Event, spec: AgentSpec) -> str:
    """Keep a returned event stable across retries of one triggering event."""
    return f"agent.return:{event.id}:{spec.slug}"
