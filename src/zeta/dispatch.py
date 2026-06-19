"""Append events, publish them, and route matching agents."""

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, cast

from zeta.kernel.agents import AgentDefinition, AgentInvocation, EventPattern
from zeta.kernel.events import DraftEvent, Event
from zeta.store.events import EventWriter

AgentResult = dict[str, Any] | Awaitable[dict[str, Any]]
AgentRunner = Callable[["AgentInvocation"], AgentResult]

__all__ = [
    "AgentDefinition",
    "AgentInvocation",
    "EventDispatcher",
    "DispatchOutcome",
    "EventPattern",
    "RegisteredAgent",
]


@dataclass(frozen=True)
class RegisteredAgent:
    """Dispatch registration for an agent definition plus executable runner."""

    definition: AgentDefinition
    run: AgentRunner | None = None


@dataclass(frozen=True)
class DispatchOutcome:
    """Result of accepting and routing one incoming event."""

    event: Event
    inserted: bool
    work_events: list[Event]
    agent_results: list[dict[str, Any]]


class EventDispatcher:
    """Async event dispatcher that routes matching agents in a task group."""

    def __init__(
        self,
        event_sink: EventWriter,
        *,
        agents: Iterable[RegisteredAgent] = (),
        publish_event: Callable[[Event], None] | None = None,
    ) -> None:
        self.event_sink = event_sink
        self.agents = tuple(agents)
        self.publish_event = publish_event

    async def dispatch(self, draft: DraftEvent) -> DispatchOutcome:
        outcome = self.event_sink.accept(draft)
        if not outcome.inserted:
            return DispatchOutcome(outcome.event, False, [], [])
        self._publish(outcome.event)
        work_events: list[Event] = []
        agent_results: list[dict[str, Any]] = []
        matching_agents = self.matching_agents(outcome.event)
        task_results: list[tuple[dict[str, Any] | None, list[Event]] | None] = [
            None
        ] * len(matching_agents)
        async with asyncio.TaskGroup() as task_group:
            for index, agent in enumerate(matching_agents):
                task_group.create_task(
                    self._run_agent_into(task_results, index, agent, outcome.event)
                )
        for task_result in task_results:
            if task_result is None:
                continue
            result, events = task_result
            work_events.extend(events)
            if result is not None:
                agent_results.append(result)
        return DispatchOutcome(outcome.event, True, work_events, agent_results)

    def matching_agents(self, event: Event) -> list[RegisteredAgent]:
        return [agent for agent in self.agents if agent.definition.accepts(event)]

    async def _run_agent_into(
        self,
        results: list[tuple[dict[str, Any] | None, list[Event]] | None],
        index: int,
        agent: RegisteredAgent,
        triggering_event: Event,
    ) -> None:
        results[index] = await self._run_agent(agent, triggering_event)

    async def _run_agent(
        self,
        agent: RegisteredAgent,
        triggering_event: Event,
    ) -> tuple[dict[str, Any] | None, list[Event]]:
        work_id = work_id_for_event(agent, triggering_event)
        pending = self._append_work_event(
            "runtime.work.pending",
            agent,
            triggering_event,
            work_id,
            {"status": "pending"},
        )
        events = [pending]
        if agent.run is None:
            return None, events
        claimed = self._append_work_event(
            "runtime.work.claimed",
            agent,
            triggering_event,
            work_id,
            {"status": "claimed"},
        )
        events.append(claimed)
        try:
            result = await maybe_await(
                agent.run(AgentInvocation(agent.definition, triggering_event))
            )
        except Exception as exc:
            failed = self._append_work_event(
                "runtime.work.failed",
                agent,
                triggering_event,
                work_id,
                {
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            events.append(failed)
            return {
                "outcome": "failed",
                "error": str(exc),
                "final_event_cursor": str(failed.cursor),
            }, events
        terminal_type = terminal_work_event_type(result)
        completed = self._append_work_event(
            terminal_type,
            agent,
            triggering_event,
            work_id,
            {"status": terminal_type.rsplit(".", 1)[-1], "result": result},
        )
        events.append(completed)
        result = {
            **result,
            "final_event_cursor": str(completed.cursor),
        }
        return result, events

    def _append_work_event(
        self,
        event_type: str,
        agent: RegisteredAgent,
        triggering_event: Event,
        work_id: str,
        payload: dict[str, Any],
    ) -> Event:
        draft = DraftEvent(
            event_type,
            "zeta",
            {
                "work_id": work_id,
                "agent_id": agent.definition.agent_id,
                "triggering_event_id": triggering_event.id,
                "triggering_event_type": triggering_event.event_type,
                **payload,
            },
            idempotency_key=f"{event_type}:{work_id}",
            caused_by=triggering_event.id,
            session_id=triggering_event.session_id,
            turn_id=triggering_event.turn_id,
        )
        event = self.event_sink.accept(draft).event
        self._publish(event)
        return event

    def _publish(self, event: Event) -> None:
        if self.publish_event is not None:
            self.publish_event(event)


async def maybe_await(result: AgentResult) -> dict[str, Any]:
    if inspect.isawaitable(result):
        return await cast(Awaitable[dict[str, Any]], result)
    return result


def work_id_for_event(agent: RegisteredAgent, event: Event) -> str:
    agent_id = agent.definition.agent_id.replace(":", "_").replace(".", "_")
    return f"work_{event.id}_{agent_id}"


def terminal_work_event_type(result: dict[str, Any]) -> str:
    outcome = result.get("outcome")
    if outcome in {"aborted", "cancelled"}:
        return "runtime.work.cancelled"
    return "runtime.work.completed"
