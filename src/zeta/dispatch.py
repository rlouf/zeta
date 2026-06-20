"""Append events, publish them, and route matching agents."""

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
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
    "terminal_agent_result",
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
    lifecycle_events: list[Event]
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
        lifecycle_events: list[Event] = []
        agent_results: list[dict[str, Any]] = []
        matching_agents = self.matching_agents(outcome.event)
        if not matching_agents:
            lifecycle_events.append(
                self._append_unhandled_queue_item_event(outcome.event)
            )
            return DispatchOutcome(outcome.event, True, lifecycle_events, [])
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
            lifecycle_events.extend(events)
            if result is not None:
                agent_results.append(result)
        return DispatchOutcome(outcome.event, True, lifecycle_events, agent_results)

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
        queue_item_id = queue_item_id_for_event(agent, triggering_event)
        created = self._append_lifecycle_event(
            "runtime.queue_item.created",
            triggering_event,
            queue_item_payload(agent, triggering_event, queue_item_id, "available"),
            idempotency_key=f"runtime.queue_item.created:{queue_item_id}",
        )
        events = [created]
        if agent.run is None:
            return None, events
        attempt_number = 1
        attempt_id = attempt_id_for_queue_item(queue_item_id, attempt_number)
        claimed = self._append_lifecycle_event(
            "runtime.queue_item.claimed",
            triggering_event,
            queue_item_payload(agent, triggering_event, queue_item_id, "claimed"),
            idempotency_key=f"runtime.queue_item.claimed:{queue_item_id}",
        )
        events.append(claimed)
        started = self._append_lifecycle_event(
            "runtime.attempt.started",
            triggering_event,
            attempt_payload(
                agent,
                triggering_event,
                queue_item_id,
                attempt_id,
                attempt_number,
                "running",
                started_at=event_timestamp(),
            ),
            idempotency_key=f"runtime.attempt.started:{attempt_id}",
        )
        events.append(started)
        try:
            result = await maybe_await(
                agent.run(AgentInvocation(agent.definition, triggering_event))
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            failed_attempt = self._append_lifecycle_event(
                "runtime.attempt.failed",
                triggering_event,
                attempt_payload(
                    agent,
                    triggering_event,
                    queue_item_id,
                    attempt_id,
                    attempt_number,
                    "failed",
                    finished_at=event_timestamp(),
                    error=error,
                ),
                idempotency_key=f"runtime.attempt.failed:{attempt_id}",
            )
            events.append(failed_attempt)
            failed_queue_item = self._append_lifecycle_event(
                "runtime.queue_item.failed",
                triggering_event,
                queue_item_payload(
                    agent,
                    triggering_event,
                    queue_item_id,
                    "failed",
                    error=error,
                ),
                idempotency_key=f"runtime.queue_item.failed:{queue_item_id}",
            )
            events.append(failed_queue_item)
            return {
                "outcome": "failed",
                "error": str(exc),
                "final_event_cursor": str(failed_queue_item.cursor),
            }, events
        attempt_terminal_type = terminal_attempt_event_type(result)
        attempt_status = attempt_terminal_type.rsplit(".", 1)[-1]
        completed_attempt = self._append_lifecycle_event(
            attempt_terminal_type,
            triggering_event,
            attempt_payload(
                agent,
                triggering_event,
                queue_item_id,
                attempt_id,
                attempt_number,
                attempt_status,
                finished_at=event_timestamp(),
                result=result,
            ),
            idempotency_key=f"{attempt_terminal_type}:{attempt_id}",
        )
        events.append(completed_attempt)
        queue_terminal_type = terminal_queue_item_event_type(result)
        queue_status = queue_terminal_type.rsplit(".", 1)[-1]
        completed_queue_item = self._append_lifecycle_event(
            queue_terminal_type,
            triggering_event,
            queue_item_payload(
                agent,
                triggering_event,
                queue_item_id,
                queue_status,
                result=result,
            ),
            idempotency_key=f"{queue_terminal_type}:{queue_item_id}",
        )
        events.append(completed_queue_item)
        result = {
            **result,
            "final_event_cursor": str(completed_queue_item.cursor),
        }
        return result, events

    def _append_lifecycle_event(
        self,
        event_type: str,
        triggering_event: Event,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> Event:
        draft = DraftEvent(
            event_type,
            "zeta",
            payload,
            idempotency_key=idempotency_key,
            caused_by=triggering_event.id,
            session_id=triggering_event.session_id,
            turn_id=triggering_event.turn_id,
        )
        event = self.event_sink.accept(draft).event
        self._publish(event)
        return event

    def _append_unhandled_queue_item_event(self, triggering_event: Event) -> Event:
        queue_item_id = f"qi_{triggering_event.id}_unhandled"
        return self._append_lifecycle_event(
            "runtime.queue_item.unhandled",
            triggering_event,
            {
                "queue_item_id": queue_item_id,
                "event_id": triggering_event.id,
                "target_agent": "",
                "status": "unhandled",
            },
            idempotency_key=f"runtime.queue_item.unhandled:{queue_item_id}",
        )

    def _publish(self, event: Event) -> None:
        if self.publish_event is not None:
            self.publish_event(event)


async def maybe_await(result: AgentResult) -> dict[str, Any]:
    if inspect.isawaitable(result):
        return await cast(Awaitable[dict[str, Any]], result)
    return result


def queue_item_id_for_event(agent: RegisteredAgent, event: Event) -> str:
    agent_id = agent.definition.agent_id.replace(":", "_").replace(".", "_")
    return f"qi_{event.id}_{agent_id}"


def attempt_id_for_queue_item(queue_item_id: str, attempt_number: int) -> str:
    return f"att_{queue_item_id}_{attempt_number}"


def queue_item_payload(
    agent: RegisteredAgent,
    event: Event,
    queue_item_id: str,
    status: str,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "queue_item_id": queue_item_id,
        "event_id": event.id,
        "target_agent": agent.definition.agent_id,
        "status": status,
        **extra,
    }


def attempt_payload(
    agent: RegisteredAgent,
    event: Event,
    queue_item_id: str,
    attempt_id: str,
    attempt_number: int,
    status: str,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "attempt_id": attempt_id,
        "queue_item_id": queue_item_id,
        "event_id": event.id,
        "attempt_number": attempt_number,
        "target_agent": agent.definition.agent_id,
        "status": status,
        **extra,
    }


def terminal_attempt_event_type(result: dict[str, Any]) -> str:
    outcome = result.get("outcome")
    if outcome in {"aborted", "cancelled"}:
        return "runtime.attempt.cancelled"
    return "runtime.attempt.completed"


def terminal_queue_item_event_type(result: dict[str, Any]) -> str:
    outcome = result.get("outcome")
    if outcome in {"aborted", "cancelled"}:
        return "runtime.queue_item.cancelled"
    return "runtime.queue_item.completed"


TERMINAL_QUEUE_ITEM_EVENT_TYPES = {
    "runtime.queue_item.completed",
    "runtime.queue_item.failed",
    "runtime.queue_item.cancelled",
}


def terminal_agent_result(lifecycle_events: Iterable[Event]) -> dict[str, Any] | None:
    for event in reversed(tuple(lifecycle_events)):
        result = terminal_event_result(event)
        if result is not None:
            return result
    return None


def terminal_event_result(event: Event) -> dict[str, Any] | None:
    if event.event_type not in TERMINAL_QUEUE_ITEM_EVENT_TYPES:
        return None
    result = event.payload.get("result")
    if isinstance(result, dict):
        return result_with_final_cursor(result, event)
    return result_with_final_cursor(terminal_fallback_result(event), event)


def terminal_fallback_result(event: Event) -> dict[str, Any]:
    status = event.payload.get("status")
    fallback: dict[str, Any] = {
        "outcome": status if isinstance(status, str) and status else "unknown",
    }
    error = event.payload.get("error")
    if isinstance(error, str) and error:
        fallback["error"] = error
    return fallback


def result_with_final_cursor(result: dict[str, Any], event: Event) -> dict[str, Any]:
    if event.cursor is None:
        return dict(result)
    return {**result, "final_event_cursor": str(event.cursor)}


def event_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
