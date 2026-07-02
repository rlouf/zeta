"""Agent declaration domain shapes."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any, Literal, cast

from jsonschema import Draft202012Validator
from zeta.agents.prompts import render_prompt
from zeta.agents.returns import derive_returns_schema
from zeta.agents.spec import AgentSpec
from zeta.records.events import DraftEvent, Event, draft_event_view, event_view
from zeta.run.config import AgentConfig
from zeta.run.outcomes import agent_run_result_payload

from zetad.retry import RetryPolicy

if TYPE_CHECKING:
    from zeta.agents.events import EventRegistry
    from zeta.run.outcomes import AgentRunResult

DispatchMode = Literal["one_shot", "session_scoped"]
AgentEventPublisher = Callable[[DraftEvent], Awaitable[Event]]
AgentRunner = Callable[["AgentInvocation"], Awaitable[dict[str, Any]]]
AgentRunRunner = Callable[..., Awaitable["AgentRunResult"]]
StructuredOutputRunner = Callable[..., dict[str, Any] | Awaitable[dict[str, Any]]]
TimelineFactory = Callable[["AgentInvocation"], list[dict[str, Any]]]
ContextFactory = Callable[["AgentInvocation"], str]
AGENT_RETURN_RESPONSE_NAME = "zeta_agent_return"


@dataclass(frozen=True)
class EventPattern:
    """A glob pattern that matches event types which can start an agent.

    The pattern uses normal shell-style glob matching. `github.issue.*` matches
    any issue event, while `session.turn.requested` matches exactly that event
    type.
    """

    event_type: str

    def matches(self, event: Event) -> bool:
        return fnmatchcase(event.event_type, self.event_type)


@dataclass(frozen=True)
class AgentDefinition:
    """A declarative description of an event-triggered agent.

    Agent definitions describe what an agent is, which events can start it, and
    which capabilities and prompt constraints shape its turn. Dispatch
    registration attaches executable runner code separately.
    """

    agent_id: str
    triggers: tuple[EventPattern, ...]
    allowed_capabilities: tuple[str, ...] = ()
    system_prompt: str | None = None
    max_turns: int | None = None
    dispatch_mode: DispatchMode = "one_shot"
    returns: tuple[str, ...] = ()
    lock_keys: tuple[str, ...] = ()
    retry_policy: RetryPolicy | None = None

    def accepts(self, event: Event) -> bool:
        return any(trigger.matches(event) for trigger in self.triggers)


@dataclass(frozen=True)
class AgentInvocation:
    """The dispatch context passed to one event-triggered agent invocation.

    Runners receive the matched definition, the durable triggering event, and
    optional queue/attempt/run ids. The explicit context keeps routing metadata
    out of domain payloads while still letting agents publish correlated events.
    """

    agent: AgentDefinition
    triggering_event: Event
    publish_event: AgentEventPublisher | None = None
    queue_item_id: str | None = None
    attempt_id: str | None = None
    run_id: str | None = None

    async def publish(self, draft: DraftEvent) -> Event:
        if self.publish_event is None:
            raise RuntimeError("agent invocation cannot publish events")
        return await self.publish_event(draft)


@dataclass(frozen=True)
class AgentRoute:
    """Deterministic event route for one agent."""

    agent_id: str
    accepts: tuple[EventPattern, ...]
    lock_keys: tuple[str, ...] = ()

    @classmethod
    def from_definition(cls, definition: AgentDefinition) -> AgentRoute:
        return cls(
            agent_id=definition.agent_id,
            accepts=definition.triggers,
            lock_keys=definition.lock_keys,
        )

    def matches(self, event: Event) -> bool:
        return any(pattern.matches(event) for pattern in self.accepts)


@dataclass(frozen=True)
class ExecutableAgent:
    """Local executable bound to an agent definition."""

    definition: AgentDefinition
    run: AgentRunner

    @property
    def agent_id(self) -> str:
        return self.definition.agent_id

    @property
    def route(self) -> AgentRoute:
        return AgentRoute.from_definition(self.definition)


def agent_session_id(definition: AgentDefinition, event: Event) -> str:
    """Return the durable runtime session id for an authored agent invocation."""
    if definition.dispatch_mode == "session_scoped":
        return f"agent/{definition.agent_id}"
    return f"agent/{definition.agent_id}/{event.id}"


def agent_run_id(attempt_id: str) -> str:
    return f"run_{attempt_id}"


def compile_agent_definition(
    spec: AgentSpec,
    *,
    config: AgentConfig | None = None,
    context: str | ContextFactory = "",
    timeline: Sequence[dict[str, Any]] | TimelineFactory = (),
    run_turn: AgentRunRunner | None = None,
    event_registry: EventRegistry | None = None,
    structured_output: StructuredOutputRunner | None = None,
) -> ExecutableAgent:
    """Compile a single-accept spec into an in-process runtime agent."""
    if not spec.enabled:
        raise ValueError("compile_agent_definition requires an enabled agent")
    if len(spec.accepts) != 1:
        raise ValueError("compile_agent_definition requires exactly one accepted event")
    return compile_agent_definitions(
        spec,
        config=config,
        context=context,
        timeline=timeline,
        run_turn=run_turn,
        event_registry=event_registry,
        structured_output=structured_output,
    )[0]


def compile_agent_definitions(
    spec: AgentSpec,
    *,
    config: AgentConfig | None = None,
    context: str | ContextFactory = "",
    timeline: Sequence[dict[str, Any]] | TimelineFactory = (),
    run_turn: AgentRunRunner | None = None,
    event_registry: EventRegistry | None = None,
    structured_output: StructuredOutputRunner | None = None,
) -> list[ExecutableAgent]:
    """Compile one authored spec into runtime definitions for each accepted event."""
    if not spec.enabled or not spec.accepts:
        return []
    if spec.returns and event_registry is None:
        raise ValueError("agent returns require an event registry")
    return [
        ExecutableAgent(
            AgentDefinition(
                agent_id=spec.slug,
                triggers=(EventPattern(event_type),),
                allowed_capabilities=spec.tools,
                system_prompt=spec.description,
                max_turns=config.max_turns if config is not None else None,
                dispatch_mode="session_scoped" if spec.resumable else "one_shot",
                returns=tuple(spec.returns),
                lock_keys=runtime_lock_keys(spec),
                retry_policy=retry_policy_for_spec(spec),
            ),
            run=agent_runner(
                spec,
                config,
                context,
                timeline,
                run_turn or default_agent_run_runner(),
                event_registry,
                structured_output or default_structured_output_runner(),
            ),
        )
        for event_type in spec.accepts
    ]


def runtime_lock_keys(spec: AgentSpec) -> tuple[str, ...]:
    value = spec.manifest.get("locks")
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, Sequence):
        raise ValueError("locks extension must be a string or list of strings")
    if not all(isinstance(item, str) for item in value):
        raise ValueError("locks extension must be a string or list of strings")
    return tuple(value)


def agent_runner(
    spec: AgentSpec,
    config: AgentConfig | None,
    context: str | ContextFactory,
    timeline: Sequence[dict[str, Any]] | TimelineFactory,
    run_turn: AgentRunRunner,
    event_registry: EventRegistry | None,
    structured_output: StructuredOutputRunner,
) -> Callable[[AgentInvocation], Awaitable[dict[str, Any]]]:
    async def run(agent_run: AgentInvocation) -> dict[str, Any]:
        effective_config = config_for_spec(spec, config)
        event = agent_run.triggering_event
        objective = render_prompt(
            spec,
            {"event_type": event.event_type, "payload": dict(event.payload)},
        )
        if callable(timeline):
            run_timeline = cast(TimelineFactory, timeline)(agent_run)
        else:
            run_timeline = list(timeline)
        if callable(context):
            run_context = cast(ContextFactory, context)(agent_run)
        else:
            run_context = context
        result = await run_turn(
            objective,
            run_timeline,
            effective_config,
            context=run_context,
            caused_by=event.id,
            agent_invocation=agent_run,
        )
        if spec.returns and event_registry is not None:
            return await finalized_agent_run_result(
                spec,
                event_registry,
                result,
                agent_run,
                objective=objective,
                config=effective_config,
                structured_output=structured_output,
            )
        return agent_run_result_mapping(result)

    return run


async def finalized_agent_run_result(
    spec: AgentSpec,
    event_registry: EventRegistry,
    result: AgentRunResult,
    agent_run: AgentInvocation,
    *,
    objective: str,
    config: AgentConfig,
    structured_output: StructuredOutputRunner,
) -> dict[str, Any]:
    schema = derive_returns_schema(spec, event_registry)
    if schema is None:
        return agent_run_result_mapping(result)
    returned = structured_output(
        structured_return_messages(
            spec,
            result,
            agent_run.triggering_event,
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
        dict[str, Any], await returned if isinstance(returned, Awaitable) else returned
    )
    Draft202012Validator(schema).validate(data)
    event_type = data.get("type")
    payload = data.get("payload")
    if not isinstance(event_type, str) or not isinstance(payload, dict):
        raise RuntimeError("structured agent return must include type and payload")
    published = await agent_run.publish(
        DraftEvent(
            event_type,
            f"agent:{spec.slug}",
            payload,
            idempotency_key=agent_return_idempotency_key(
                agent_run.triggering_event,
                spec,
            ),
            caused_by=agent_run.triggering_event.id,
        )
    )
    base = agent_run_result_mapping(result)
    return {
        **base,
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


def config_for_spec(spec: AgentSpec, config: AgentConfig | None) -> AgentConfig:
    if config is None:
        return AgentConfig(
            system_prompt=spec.description,
            allowed_capabilities=spec.tools,
            execution_mode="direct",
            model_name=spec.model.name if spec.model is not None else None,
            model_url=spec.model.url if spec.model is not None else None,
        )
    return replace(
        config,
        system_prompt=config.system_prompt or spec.description,
        allowed_capabilities=config.allowed_capabilities or spec.tools,
        model_name=config.model_name
        or (spec.model.name if spec.model is not None else None),
        model_url=config.model_url
        or (spec.model.url if spec.model is not None else None),
    )


def retry_policy_for_spec(spec: AgentSpec) -> RetryPolicy | None:
    if spec.retry is None:
        return None
    policy = RetryPolicy()
    return RetryPolicy(
        max_attempts=spec.retry.max_attempts
        if spec.retry.max_attempts is not None
        else policy.max_attempts,
        backoff_base_seconds=spec.retry.backoff_seconds
        if spec.retry.backoff_seconds is not None
        else policy.backoff_base_seconds,
        backoff_factor=policy.backoff_factor,
        backoff_max_seconds=policy.backoff_max_seconds,
    )


def agent_run_result_mapping(result: AgentRunResult) -> dict[str, Any]:
    return agent_run_result_payload(result)


def default_agent_run_runner() -> AgentRunRunner:
    from zeta.run.runtime import run_agent_loop

    return run_agent_loop


def default_structured_output_runner() -> StructuredOutputRunner:
    from zeta.models import chat_structured_output

    return chat_structured_output
