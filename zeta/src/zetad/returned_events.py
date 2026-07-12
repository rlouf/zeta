"""Validate and publish structured events returned by authored agents."""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, cast

from jsonschema import Draft202012Validator
from zeta.agents.events import EventRegistry
from zeta.agents.returns import derive_returns_schema
from zeta.agents.spec import AgentSpec
from zeta.records.events import DraftEvent, Event, draft_event_view, event_view
from zeta.run.config import AgentConfig
from zeta.run.outcomes import AgentRunResult, agent_run_result_payload

StructuredOutputRunner = Callable[..., dict[str, Any] | Awaitable[dict[str, Any]]]
AGENT_RETURN_RESPONSE_NAME = "zeta_agent_return"


class ReturnedEventInvocation(Protocol):
    triggering_event: Event

    async def publish(self, draft: DraftEvent) -> Event: ...


class ReturnedEventPublisher:
    """Own the final structured generation and returned-event contract."""

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
        returned = self.structured_output(
            structured_return_messages(
                spec,
                result,
                invocation.triggering_event,
                objective=objective,
            ),
            schema=schema,
            response_name=AGENT_RETURN_RESPONSE_NAME,
            selected_model=config.model_name,
            selected_url=config.model_url,
            session_id=config.model_session_id,
            api=config.model_api,
        )
        data = cast(
            dict[str, Any],
            await returned if inspect.isawaitable(returned) else returned,
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
        {
            "role": "user",
            "content": json.dumps(payload, sort_keys=True),
        },
    ]


def agent_return_idempotency_key(event: Event, spec: AgentSpec) -> str:
    return f"agent.return:{event.id}:{spec.slug}"
