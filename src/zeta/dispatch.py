"""Append events, publish them, and route matching agents."""

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from zeta.kernel.agents import AgentDefinition, AgentInvocation, EventPattern
from zeta.kernel.dispatch import Attempt, AttemptStatus, QueueItem, QueueItemStatus
from zeta.kernel.events import DraftEvent, Event
from zeta.store.events import EventWriter

AgentRunner = Callable[["AgentInvocation"], Awaitable[dict[str, Any]]]

__all__ = [
    "AgentDefinition",
    "AgentInvocation",
    "EventDispatcher",
    "DispatchOutcome",
    "EventPattern",
    "RegisteredAgent",
    "ReservedRuntimeEventError",
    "terminal_queue_item_result",
]

RESERVED_RUNTIME_EVENT_PREFIXES = ("runtime.queue_item.", "runtime.attempt.")


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


@dataclass(frozen=True)
class ReservedRuntimeEventError(ValueError):
    """Raised when external ingress tries to write runtime-owned lifecycle."""

    event_type: str

    def __post_init__(self) -> None:
        super().__init__(f"external event ingress cannot accept {self.event_type!r}")


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
        self.publish_callback = publish_event

    async def publish_event(
        self,
        draft: DraftEvent,
        *,
        route: bool = True,
    ) -> DispatchOutcome:
        reject_reserved_runtime_event(draft)
        outcome = self.event_sink.accept(draft)
        if not outcome.inserted:
            return DispatchOutcome(outcome.event, False, [])
        self._publish(outcome.event)
        lifecycle_events = await self.route(outcome.event) if route else []
        return DispatchOutcome(outcome.event, True, lifecycle_events)

    async def route(self, event: Event) -> list[Event]:
        lifecycle_events: list[Event] = []
        matching_agents = self.matching_agents(event)
        if not matching_agents:
            return [self._append_unhandled_queue_item_event(event)]
        task_results: list[list[Event] | None] = [None] * len(matching_agents)
        async with asyncio.TaskGroup() as task_group:
            for index, agent in enumerate(matching_agents):
                task_group.create_task(
                    self._run_agent_into(task_results, index, agent, event)
                )
        for task_result in task_results:
            if task_result is None:
                continue
            lifecycle_events.extend(task_result)
        return lifecycle_events

    def matching_agents(self, event: Event) -> list[RegisteredAgent]:
        return [agent for agent in self.agents if agent.definition.accepts(event)]

    async def _run_agent_into(
        self,
        results: list[list[Event] | None],
        index: int,
        agent: RegisteredAgent,
        triggering_event: Event,
    ) -> None:
        results[index] = await self._run_agent(agent, triggering_event)

    async def _run_agent(
        self,
        agent: RegisteredAgent,
        triggering_event: Event,
    ) -> list[Event]:
        queue_item_id = queue_item_id_for_event(agent, triggering_event)
        events = [
            self._append_queue_item_event(
                triggering_event,
                agent,
                queue_item_id,
                event_suffix="created",
                status="available",
            )
        ]
        if agent.run is None:
            return events

        attempt_number = 1
        attempt_id = attempt_id_for_queue_item(queue_item_id, attempt_number)
        events.append(
            self._append_queue_item_event(
                triggering_event,
                agent,
                queue_item_id,
                event_suffix="claimed",
                status="claimed",
                attempt_number=attempt_number,
            )
        )
        started_at = event_timestamp()
        events.append(
            self._append_attempt_event(
                triggering_event,
                agent,
                queue_item_id,
                attempt_id,
                attempt_number,
                event_suffix="started",
                status="running",
                started_at=started_at,
            )
        )
        try:
            result = await agent.run(
                AgentInvocation(
                    agent.definition,
                    triggering_event,
                    publish_event=self._agent_event_publisher(
                        agent,
                        triggering_event,
                        queue_item_id,
                        attempt_id,
                    ),
                    queue_item_id=queue_item_id,
                    attempt_id=attempt_id,
                    run_id=triggering_event.run_id,
                )
            )
        except Exception as exc:
            events.extend(
                self._failed_agent_events(
                    exc,
                    triggering_event,
                    agent,
                    queue_item_id,
                    attempt_id,
                    attempt_number,
                    started_at,
                )
            )
            return events

        events.extend(
            self._terminal_agent_events(
                result,
                triggering_event,
                agent,
                queue_item_id,
                attempt_id,
                attempt_number,
                started_at,
            )
        )
        return events

    def _append_queue_item_event(
        self,
        triggering_event: Event,
        agent: RegisteredAgent,
        queue_item_id: str,
        *,
        event_suffix: str,
        status: QueueItemStatus,
        attempt_number: int | None = None,
        **payload_extra: Any,
    ) -> Event:
        queue_item = QueueItem(
            queue_item_id=queue_item_id,
            event_id=triggering_event.id,
            target_agent=agent.definition.agent_id,
            status=status,
        )
        return self._append_lifecycle_event(
            f"runtime.queue_item.{event_suffix}",
            triggering_event,
            queue_item_payload(queue_item, **payload_extra),
            idempotency_key=queue_item_idempotency_key(
                triggering_event,
                agent.definition.agent_id,
                event_suffix,
                attempt_number=attempt_number,
            ),
        )

    def _append_attempt_event(
        self,
        triggering_event: Event,
        agent: RegisteredAgent,
        queue_item_id: str,
        attempt_id: str,
        attempt_number: int,
        *,
        event_suffix: str,
        status: AttemptStatus,
        started_at: str,
        finished_at: str | None = None,
        error: str | None = None,
        **payload_extra: Any,
    ) -> Event:
        attempt = Attempt(
            attempt_id=attempt_id,
            queue_item_id=queue_item_id,
            event_id=triggering_event.id,
            attempt_number=attempt_number,
            target_agent=agent.definition.agent_id,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            error=error,
            session_id=triggering_event.session_id,
            run_id=triggering_event.run_id,
        )
        return self._append_lifecycle_event(
            f"runtime.attempt.{event_suffix}",
            triggering_event,
            attempt_payload(attempt, **payload_extra),
            idempotency_key=attempt_idempotency_key(
                queue_item_id,
                attempt_number,
                event_suffix,
            ),
        )

    def _failed_agent_events(
        self,
        exc: Exception,
        triggering_event: Event,
        agent: RegisteredAgent,
        queue_item_id: str,
        attempt_id: str,
        attempt_number: int,
        started_at: str,
    ) -> list[Event]:
        error = f"{type(exc).__name__}: {exc}"
        return [
            self._append_attempt_event(
                triggering_event,
                agent,
                queue_item_id,
                attempt_id,
                attempt_number,
                event_suffix="failed",
                status="failed",
                started_at=started_at,
                finished_at=event_timestamp(),
                error=error,
            ),
            self._append_queue_item_event(
                triggering_event,
                agent,
                queue_item_id,
                event_suffix="failed",
                status="failed",
                error=error,
            ),
        ]

    def _terminal_agent_events(
        self,
        result: dict[str, Any],
        triggering_event: Event,
        agent: RegisteredAgent,
        queue_item_id: str,
        attempt_id: str,
        attempt_number: int,
        started_at: str,
    ) -> list[Event]:
        attempt_status = terminal_attempt_status(result)
        queue_status = terminal_queue_item_status(result)
        return [
            self._append_attempt_event(
                triggering_event,
                agent,
                queue_item_id,
                attempt_id,
                attempt_number,
                event_suffix=attempt_status,
                status=attempt_status,
                started_at=started_at,
                finished_at=event_timestamp(),
                result=result,
            ),
            self._append_queue_item_event(
                triggering_event,
                agent,
                queue_item_id,
                event_suffix=queue_status,
                status=queue_status,
                result=result,
            ),
        ]

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
            run_id=triggering_event.run_id,
            turn_id=triggering_event.turn_id,
        )
        event = self.event_sink.accept(draft).event
        self._publish(event)
        return event

    def _append_unhandled_queue_item_event(self, triggering_event: Event) -> Event:
        queue_item_id = f"qi_{triggering_event.id}_unhandled"
        queue_item = QueueItem(
            queue_item_id=queue_item_id,
            event_id=triggering_event.id,
            target_agent="",
            status="unhandled",
        )
        return self._append_lifecycle_event(
            "runtime.queue_item.unhandled",
            triggering_event,
            queue_item_payload(queue_item),
            idempotency_key=unhandled_queue_item_idempotency_key(triggering_event),
        )

    def _publish(self, event: Event) -> None:
        if self.publish_callback is not None:
            self.publish_callback(event)

    def _agent_event_publisher(
        self,
        agent: RegisteredAgent,
        triggering_event: Event,
        queue_item_id: str,
        attempt_id: str,
    ) -> Callable[[DraftEvent], Awaitable[Event]]:
        async def publish(draft: DraftEvent) -> Event:
            tagged = DraftEvent(
                draft.event_type,
                draft.source,
                {
                    **draft.payload,
                    "_zeta_queue_item_id": queue_item_id,
                    "_zeta_attempt_id": attempt_id,
                    "_zeta_target_agent": agent.definition.agent_id,
                    "_zeta_triggering_event_id": triggering_event.id,
                },
                idempotency_key=draft.idempotency_key,
                caused_by=draft.caused_by or triggering_event.id,
                session_id=draft.session_id or triggering_event.session_id,
                run_id=draft.run_id or triggering_event.run_id,
                turn_id=draft.turn_id or triggering_event.turn_id,
            )
            outcome = await self.publish_event(tagged)
            return outcome.event

        return publish


def reject_reserved_runtime_event(draft: DraftEvent) -> None:
    if draft.event_type.startswith(RESERVED_RUNTIME_EVENT_PREFIXES):
        raise ReservedRuntimeEventError(draft.event_type)


def required_payload_string(event: Event, key: str) -> str | None:
    value = event.payload.get(key)
    if isinstance(value, str):
        return value
    return None


def optional_payload_string(event: Event, key: str) -> str | None:
    value = event.payload.get(key)
    if isinstance(value, str):
        return value
    return None


def queue_item_result(event: Event) -> dict[str, Any] | None:
    result = event.payload.get("result")
    if isinstance(result, dict):
        return result
    return None


def queue_item_id_for_event(agent: RegisteredAgent, event: Event) -> str:
    agent_id = agent.definition.agent_id.replace(":", "_").replace(".", "_")
    return f"qi_{event.id}_{agent_id}"


def attempt_id_for_queue_item(queue_item_id: str, attempt_number: int) -> str:
    return f"att_{queue_item_id}_{attempt_number}"


def queue_item_idempotency_key(
    event: Event,
    target_agent: str,
    status: str,
    *,
    attempt_number: int | None = None,
) -> str:
    key = f"queue_item:{event.id}:{target_agent}:{status}"
    if attempt_number is None:
        return key
    return f"{key}:{attempt_number}"


def unhandled_queue_item_idempotency_key(event: Event) -> str:
    return f"queue_item:{event.id}:unhandled"


def attempt_idempotency_key(
    queue_item_id: str,
    attempt_number: int,
    status: str,
) -> str:
    return f"attempt:{queue_item_id}:{attempt_number}:{status}"


def queue_item_payload(
    queue_item: QueueItem,
    **extra: Any,
) -> dict[str, Any]:
    return {**asdict(queue_item), **extra}


def attempt_payload(
    attempt: Attempt,
    **extra: Any,
) -> dict[str, Any]:
    return {**asdict(attempt), **extra}


def terminal_attempt_status(result: dict[str, Any]) -> AttemptStatus:
    outcome = result.get("outcome")
    if outcome in {"aborted", "cancelled"}:
        return "cancelled"
    return "completed"


def terminal_queue_item_status(result: dict[str, Any]) -> QueueItemStatus:
    outcome = result.get("outcome")
    if outcome in {"aborted", "cancelled"}:
        return "cancelled"
    return "completed"


TERMINAL_QUEUE_ITEM_EVENT_TYPES = {
    "runtime.queue_item.completed",
    "runtime.queue_item.failed",
    "runtime.queue_item.cancelled",
}


def terminal_queue_item_result(
    lifecycle_events: Iterable[Event],
    *,
    event_id: str,
    target_agent: str,
) -> dict[str, Any] | None:
    for event in reversed(tuple(lifecycle_events)):
        if event.event_type not in TERMINAL_QUEUE_ITEM_EVENT_TYPES:
            continue
        if required_payload_string(event, "event_id") != event_id:
            continue
        if required_payload_string(event, "target_agent") != target_agent:
            continue
        return terminal_queue_item_event_result(event)
    return None


def terminal_queue_item_event_result(event: Event) -> dict[str, Any] | None:
    if event.event_type not in TERMINAL_QUEUE_ITEM_EVENT_TYPES:
        return None
    result = queue_item_result(event)
    if result is not None:
        return result_with_final_cursor(result, event)
    return result_with_final_cursor(terminal_fallback_result(event), event)


def terminal_fallback_result(event: Event) -> dict[str, Any]:
    fallback: dict[str, Any] = {
        "outcome": optional_payload_string(event, "status")
        or event.event_type.rsplit(".", 1)[-1]
    }
    error = optional_payload_string(event, "error")
    if error is not None:
        fallback["error"] = error
    return fallback


def result_with_final_cursor(result: dict[str, Any], event: Event) -> dict[str, Any]:
    if event.cursor is None:
        return dict(result)
    return {**result, "final_event_cursor": str(event.cursor)}


def event_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
