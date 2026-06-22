"""Runtime compilation for authored agents."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from agents.prompts import render_prompt
from agents.spec import AgentSpec
from zeta.agents.capabilities import AgentConfig
from zeta.dispatch import RegisteredAgent
from zeta.kernel.agents import AgentDefinition, AgentInvocation, EventPattern

if TYPE_CHECKING:
    from zeta.loop import AgentRunResult

AgentRunRunner = Callable[..., Awaitable["AgentRunResult"]]
TimelineFactory = Callable[[AgentInvocation], list[dict[str, Any]]]
ContextFactory = Callable[[AgentInvocation], str]


def compile_agent_definition(
    spec: AgentSpec,
    *,
    config: AgentConfig | None = None,
    context: str | ContextFactory = "",
    timeline: Sequence[dict[str, Any]] | TimelineFactory = (),
    run_turn: AgentRunRunner | None = None,
) -> RegisteredAgent:
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
    )[0]


def compile_agent_definitions(
    spec: AgentSpec,
    *,
    config: AgentConfig | None = None,
    context: str | ContextFactory = "",
    timeline: Sequence[dict[str, Any]] | TimelineFactory = (),
    run_turn: AgentRunRunner | None = None,
) -> list[RegisteredAgent]:
    """Compile one authored spec into runtime definitions for each accepted event."""
    if not spec.enabled or not spec.accepts:
        return []
    return [
        RegisteredAgent(
            AgentDefinition(
                agent_id=spec.slug,
                triggers=(EventPattern(event_type),),
                allowed_capabilities=spec.tools,
                system_prompt=spec.description,
                max_turns=config.max_turns if config is not None else None,
                dispatch_mode="session_scoped" if spec.resumable else "one_shot",
                returns=tuple(spec.returns),
                lock_keys=runtime_lock_keys(spec),
            ),
            run=agent_runner(
                spec,
                config,
                context,
                timeline,
                run_turn or default_agent_run_runner(),
            ),
        )
        for event_type in spec.accepts
    ]


def runtime_lock_keys(spec: AgentSpec) -> tuple[str, ...]:
    value = spec.extension("locks")
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
        )
        return agent_run_result_mapping(result)

    return run


def config_for_spec(spec: AgentSpec, config: AgentConfig | None) -> AgentConfig:
    if config is None:
        return AgentConfig(
            system_prompt=spec.description,
            allowed_capabilities=spec.tools,
        )
    return replace(
        config,
        system_prompt=config.system_prompt or spec.description,
        allowed_capabilities=config.allowed_capabilities or spec.tools,
    )


def agent_run_result_mapping(result: AgentRunResult) -> dict[str, Any]:
    payload: dict[str, Any] = {"final_answer": result.final_answer}
    if result.events:
        payload["events"] = result.events
    if result.staged_effect is not None:
        payload["staged_effect"] = result.staged_effect
    return payload


def default_agent_run_runner() -> AgentRunRunner:
    from zeta.loop import run_agent

    return run_agent
