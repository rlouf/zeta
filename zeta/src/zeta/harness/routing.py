"""Agent declaration domain shapes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any, cast

from zeta import ids
from zeta.authoring.prompts import render_prompt
from zeta.authoring.spec import AgentSpec, ExecutorSpec
from zeta.events import DraftEvent, Event
from zeta.harness.retry import RetryPolicy
from zeta.harness.templates import agent_session_id
from zeta.loop.config import AgentConfig
from zeta.loop.outcomes import agent_run_result_payload

if TYPE_CHECKING:
    from zeta.authoring.schemas import EventRegistry
    from zeta.loop.outcomes import AgentRunResult

AgentEventPublisher = Callable[[DraftEvent], Awaitable[Event]]
AgentRunner = Callable[["AgentInvocation"], Awaitable[dict[str, Any]]]
TimelineFactory = Callable[["AgentInvocation"], list[dict[str, Any]]]
ContextFactory = Callable[["AgentInvocation"], str]
AgentLoop = Callable[
    ["AgentInvocation", str, list[dict[str, Any]], str, AgentConfig, str, str],
    Awaitable["AgentRunResult"],
]


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
    session: str = "per-event"
    lock_keys: tuple[str, ...] = ()
    retry_policy: RetryPolicy | None = None
    project_generation: str | None = None
    execution_manifest: Mapping[str, Any] | None = None
    tool_executor: ExecutorSpec = field(default_factory=ExecutorSpec)
    publishable_events: Mapping[str, dict[str, Any] | None] = field(
        default_factory=dict
    )

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


async def in_process_agent_loop(
    invocation: AgentInvocation,
    objective: str,
    timeline: list[dict[str, Any]],
    context: str,
    config: AgentConfig,
    session_id: str,
    run_id: str,
) -> AgentRunResult:
    """Run the model loop locally when no runtime service is present."""

    del session_id, run_id
    from zeta.capabilities.executors import local_tool_executor
    from zeta.loop.runtime import run_agent_loop

    return await run_agent_loop(
        objective,
        timeline,
        config,
        context=context,
        caused_by=invocation.triggering_event.id,
        tool_executor=local_tool_executor(),
        publishable_events=dict(invocation.agent.publishable_events),
        source_queue_item_id=invocation.queue_item_id
        or ids.queue_item_id(
            invocation.triggering_event.id,
            invocation.agent.agent_id,
        ),
    )


@dataclass(frozen=True)
class AgentRoute:
    """Deterministic event route for one agent."""

    agent_id: str
    accepts: tuple[EventPattern, ...]
    lock_keys: tuple[str, ...] = ()
    project_generation: str | None = None

    @classmethod
    def from_definition(cls, definition: AgentDefinition) -> AgentRoute:
        return cls(
            agent_id=definition.agent_id,
            accepts=definition.triggers,
            lock_keys=definition.lock_keys,
            project_generation=definition.project_generation,
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


def compile_agent_definition(
    spec: AgentSpec,
    *,
    config: AgentConfig | None = None,
    context: str | ContextFactory = "",
    timeline: Sequence[dict[str, Any]] | TimelineFactory = (),
    agent_loop: AgentLoop | None = None,
    event_registry: EventRegistry | None = None,
    project_generation: str | None = None,
    execution_manifest: Mapping[str, Any] | None = None,
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
        agent_loop=agent_loop,
        event_registry=event_registry,
        project_generation=project_generation,
        execution_manifest=execution_manifest,
    )[0]


def compile_agent_definitions(
    spec: AgentSpec,
    *,
    config: AgentConfig | None = None,
    context: str | ContextFactory = "",
    timeline: Sequence[dict[str, Any]] | TimelineFactory = (),
    agent_loop: AgentLoop | None = None,
    event_registry: EventRegistry | None = None,
    project_generation: str | None = None,
    execution_manifest: Mapping[str, Any] | None = None,
) -> list[ExecutableAgent]:
    """Compile one authored spec into runtime definitions for each accepted event."""
    if not spec.enabled or not spec.accepts:
        return []
    if spec.publishes and event_registry is None:
        raise ValueError("agent publishes require an event registry")
    return [
        ExecutableAgent(
            AgentDefinition(
                agent_id=spec.slug,
                triggers=(EventPattern(event_type),),
                allowed_capabilities=spec.tools,
                system_prompt=spec.description,
                max_turns=config.max_turns if config is not None else None,
                session=spec.session,
                lock_keys=runtime_lock_keys(spec),
                retry_policy=retry_policy_for_spec(spec),
                project_generation=project_generation,
                execution_manifest=execution_manifest,
                tool_executor=spec.executor,
                publishable_events={
                    event_type: event_registry.schema(event_type)
                    for event_type in spec.publishes
                }
                if event_registry is not None
                else {},
            ),
            run=agent_runner(
                spec,
                config,
                context,
                timeline,
                agent_loop or in_process_agent_loop,
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
    agent_loop: AgentLoop,
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
        session_id = agent_session_id(
            agent_run.agent.agent_id,
            agent_run.agent.session,
            event,
        )
        run_id = ids.run_id_for_attempt(
            agent_run.run_id,
            agent_run.attempt_id or agent_run.triggering_event.id,
        )
        result = await agent_loop(
            agent_run,
            objective,
            run_timeline,
            run_context,
            effective_config,
            session_id,
            run_id,
        )
        return agent_run_result_mapping(result)

    return run


def config_for_spec(spec: AgentSpec, config: AgentConfig | None) -> AgentConfig:
    if config is None:
        return AgentConfig(
            system_prompt=spec.description,
            allowed_capabilities=spec.tools,
            model_name=spec.model.name if spec.model is not None else None,
            model_url=spec.model.url if spec.model is not None else None,
            base_dir=spec.base_dir,
        )
    return replace(
        config,
        system_prompt=config.system_prompt or spec.description,
        allowed_capabilities=config.allowed_capabilities or spec.tools,
        model_name=config.model_name
        or (spec.model.name if spec.model is not None else None),
        model_url=config.model_url
        or (spec.model.url if spec.model is not None else None),
        base_dir=config.base_dir or spec.base_dir,
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
