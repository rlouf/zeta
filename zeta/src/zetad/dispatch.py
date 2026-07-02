"""Append events, publish them, and route matching agents."""

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from zeta.records.events import DraftEvent, Event
from zeta.records.stores.event_store import (
    EventReader,
    EventStoreProtocol,
    EventWriter,
    Filter,
)

from zetad.agents import (
    AgentDefinition,
    AgentInvocation,
    AgentRoute,
    EventPattern,
    ExecutableAgent,
    agent_run_id,
    agent_session_id,
)
from zetad.attempts import (
    Attempt,
    AttemptStatus,
    attempt_event_payload,
    attempt_idempotency_key,
)
from zetad.queue import (
    TERMINAL_QUEUE_ITEM_EVENT_TYPES,
    QueueItem,
    QueueItemStatus,
    RoutedQueueItem,
    queue_item_event_payload,
    queue_item_from_record,
    queue_item_id_for_event,
    queue_item_idempotency_key,
    routed_queue_item_from_event,
    unhandled_queue_item_idempotency_key,
)
from zetad.retry import RetryPolicy, error_code_for_exception

__all__ = [
    "AgentDefinition",
    "AgentInvocation",
    "AgentRoute",
    "EventDispatcher",
    "ExecutableAgent",
    "DispatchOutcome",
    "EventPattern",
    "ReservedRuntimeEventError",
    "RouteOutcome",
    "RetryPolicy",
    "TerminalQueueItemError",
]

RESERVED_RUNTIME_EVENT_PREFIXES = ("runtime.queue_item.", "runtime.attempt.")


@runtime_checkable
class QueueItemRecordReader(Protocol):
    """Operational queue index used by daemon-style workers."""

    def queue_item(self, queue_item_id: str) -> Mapping[str, Any] | None:
        """Return one queue item row by id."""


@runtime_checkable
class AttemptHeartbeatStore(Protocol):
    """Operational attempt index used to keep worker leases alive."""

    def heartbeat_attempt(
        self,
        attempt_id: str,
        queue_item_id: str,
        worker_name: str,
        *,
        claim_token: str,
        lease_ms: int,
        now_ms: int,
    ) -> bool:
        """Refresh a running attempt heartbeat and its queue lease."""


@runtime_checkable
class QueueClaimOwnershipStore(Protocol):
    """Operational queue index used to fence lifecycle writes."""

    def queue_claim_is_current(
        self,
        queue_item_id: str,
        worker_name: str,
        claim_token: str,
    ) -> bool:
        """Return whether the queue claim token still owns the item."""


@dataclass(frozen=True)
class DispatchOutcome:
    """Result of accepting and routing one incoming event."""

    event: Event
    inserted: bool
    lifecycle_events: list[Event]


@dataclass(frozen=True)
class RouteOutcome:
    """Result of routing one durable event to available queue items."""

    event: Event
    lifecycle_events: list[Event]
    queue_items: list[RoutedQueueItem]


@dataclass(frozen=True)
class ReservedRuntimeEventError(ValueError):
    """Raised when external ingress tries to write runtime-owned lifecycle."""

    event_type: str

    def __post_init__(self) -> None:
        super().__init__(f"external event ingress cannot accept {self.event_type!r}")


@dataclass(frozen=True)
class TerminalQueueItemError(RuntimeError):
    """Raised when execution is requested for already terminal work."""

    queue_item_id: str
    event_type: str

    def __post_init__(self) -> None:
        super().__init__(
            f"queue item {self.queue_item_id!r} is already terminal "
            f"at {self.event_type!r}"
        )


class EventDispatcher:
    """Async event dispatcher that routes matching agents in a task group."""

    def __init__(
        self,
        event_sink: EventWriter,
        *,
        routes: Iterable[AgentRoute] = (),
        executors: Iterable[ExecutableAgent] = (),
        publish_event: Callable[[Event], None] | None = None,
        worker_name: str | None = None,
        heartbeat_interval_seconds: float | None = None,
        lease_ms: int = 60_000,
        claim_token: str | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.event_sink = event_sink
        self.executors = tuple(executors)
        route_by_agent = {route.agent_id: route for route in routes}
        for executor in self.executors:
            route_by_agent[executor.agent_id] = executor.route
        self.routes = tuple(route_by_agent.values())
        self.publish_callback = publish_event
        self.worker_name = worker_name
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.lease_ms = lease_ms
        self.claim_token = claim_token
        self.retry_policy = retry_policy or RetryPolicy()

    async def publish_event(
        self,
        draft: DraftEvent,
    ) -> DispatchOutcome:
        reject_reserved_runtime_event(draft)
        outcome = self.event_sink.accept(draft)
        if not outcome.inserted:
            return DispatchOutcome(outcome.event, False, [])
        self._publish(outcome.event)
        return DispatchOutcome(outcome.event, True, [])

    async def publish_and_run(self, draft: DraftEvent) -> DispatchOutcome:
        outcome = await self.publish_event(draft)
        if not outcome.inserted:
            return outcome
        route_outcome = await self.route(outcome.event)
        lifecycle_events = [
            *route_outcome.lifecycle_events,
            *await self.run_queue_items(route_outcome.queue_items),
        ]
        return DispatchOutcome(outcome.event, True, lifecycle_events)

    async def route(self, event: Event) -> RouteOutcome:
        lifecycle_events: list[Event] = []
        queue_items: list[RoutedQueueItem] = []
        matching_routes = self.matching_routes(event)
        if not matching_routes:
            return RouteOutcome(
                event,
                [self._append_unhandled_queue_item_event(event)],
                [],
            )
        for route in matching_routes:
            queue_item_id = queue_item_id_for_event(route, event)
            lifecycle_events.append(
                self._append_queue_item_event(
                    event,
                    route,
                    queue_item_id,
                    event_suffix="available",
                    status="available",
                )
            )
            queue_items.append(
                RoutedQueueItem(
                    queue_item_id=queue_item_id,
                    event_id=event.id,
                    target_agent=route.agent_id,
                )
            )
        return RouteOutcome(event, lifecycle_events, queue_items)

    async def run_queue_items(
        self,
        queue_items: Iterable[RoutedQueueItem],
    ) -> list[Event]:
        lifecycle_events: list[Event] = []
        runnable_items = list(queue_items)
        task_results: list[list[Event] | None] = [None] * len(runnable_items)
        async with asyncio.TaskGroup() as task_group:
            for index, queue_item in enumerate(runnable_items):
                task_group.create_task(
                    self._run_queue_item_into(task_results, index, queue_item)
                )
        for task_result in task_results:
            if task_result is None:
                continue
            lifecycle_events.extend(task_result)
        return lifecycle_events

    async def run_queue_item(
        self,
        queue_item: RoutedQueueItem | str,
    ) -> list[Event]:
        routed_queue_item = self._resolve_queue_item(queue_item)
        terminal_event = self._terminal_queue_item_event(
            routed_queue_item.queue_item_id
        )
        if terminal_event is not None:
            raise TerminalQueueItemError(
                routed_queue_item.queue_item_id,
                terminal_event.event_type,
            )
        triggering_event = self._stored_event(routed_queue_item.event_id)
        if routed_queue_item.target_agent == "":
            return await self._route_claimed_queue_item(
                triggering_event,
                routed_queue_item,
            )
        executor = self._executor_for_id(routed_queue_item.target_agent)
        if executor is None:
            return self._missing_executor_events(triggering_event, routed_queue_item)
        return await self._run_agent(executor, triggering_event, routed_queue_item)

    def schedule_retry(
        self,
        queue_item: RoutedQueueItem | str,
        *,
        attempt_number: int | None = None,
        policy: RetryPolicy | None = None,
    ) -> Event:
        routed_queue_item = self._resolve_queue_item(queue_item)
        triggering_event = self._stored_event(routed_queue_item.event_id)
        next_attempt_number = (
            attempt_number
            if attempt_number is not None
            else self._next_attempt_number(routed_queue_item.queue_item_id)
        )
        retry_policy = policy or self._retry_policy_for_agent(
            routed_queue_item.target_agent
        )
        previous_attempt_number = max(next_attempt_number - 1, 1)
        not_before = current_time_ms() + retry_policy.delay_ms(
            previous_attempt_number
        )
        return self._append_queue_item_event_for_target(
            triggering_event,
            routed_queue_item.queue_item_id,
            routed_queue_item.target_agent,
            event_suffix="available",
            status="available",
            attempt_number=next_attempt_number,
            not_before=not_before,
        )

    def matching_routes(self, event: Event) -> list[AgentRoute]:
        return [route for route in self.routes if route.matches(event)]

    async def _run_queue_item_into(
        self,
        results: list[list[Event] | None],
        index: int,
        queue_item: RoutedQueueItem,
    ) -> None:
        results[index] = await self.run_queue_item(queue_item)

    async def _run_agent(
        self,
        agent: ExecutableAgent,
        triggering_event: Event,
        queue_item: RoutedQueueItem,
    ) -> list[Event]:
        queue_item_id = queue_item.queue_item_id
        events: list[Event] = []
        attempt_number = self._next_attempt_number(queue_item_id)
        attempt_id = f"att_{queue_item_id}_{attempt_number}"
        run_id = triggering_event.run_id or agent_run_id(attempt_id)
        session_id = invocation_session_id(agent.definition, triggering_event)
        if not self._queue_claim_is_current(queue_item_id):
            return events
        events.append(
            self._append_queue_item_event(
                triggering_event,
                agent.route,
                queue_item_id,
                event_suffix="claimed",
                status="claimed",
                attempt_number=attempt_number,
                session_id=session_id,
                run_id=run_id,
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
                session_id=session_id,
                run_id=run_id,
            )
        )
        heartbeat_task = self._start_attempt_heartbeat(attempt_id, queue_item_id)
        try:
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
                            session_id,
                            run_id,
                        ),
                        queue_item_id=queue_item_id,
                        attempt_id=attempt_id,
                        run_id=run_id,
                    )
                )
            except Exception as exc:
                if not self._queue_claim_is_current(queue_item_id):
                    return events
                events.extend(
                    self._failed_agent_events(
                        exc,
                        triggering_event,
                        agent,
                        queue_item_id,
                        attempt_id,
                        attempt_number,
                        started_at,
                        session_id,
                        run_id,
                    )
                )
                return events
        finally:
            await self._stop_attempt_heartbeat(heartbeat_task)

        if not self._queue_claim_is_current(queue_item_id):
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
                session_id,
                run_id,
            )
        )
        return events

    def _queue_claim_is_current(self, queue_item_id: str) -> bool:
        if self.worker_name is None or self.claim_token is None:
            return True
        if not isinstance(self.event_sink, QueueClaimOwnershipStore):
            return True
        return self.event_sink.queue_claim_is_current(
            queue_item_id,
            self.worker_name,
            self.claim_token,
        )

    def _start_attempt_heartbeat(
        self,
        attempt_id: str,
        queue_item_id: str,
    ) -> asyncio.Task[None] | None:
        if (
            self.worker_name is None
            or self.claim_token is None
            or self.heartbeat_interval_seconds is None
            or self.heartbeat_interval_seconds <= 0
            or not isinstance(self.event_sink, AttemptHeartbeatStore)
        ):
            return None
        return asyncio.create_task(
            self._heartbeat_attempt(self.event_sink, attempt_id, queue_item_id)
        )

    async def _stop_attempt_heartbeat(
        self,
        heartbeat_task: asyncio.Task[None] | None,
    ) -> None:
        if heartbeat_task is None:
            return
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task

    async def _heartbeat_attempt(
        self,
        store: AttemptHeartbeatStore,
        attempt_id: str,
        queue_item_id: str,
    ) -> None:
        if self.worker_name is None or self.heartbeat_interval_seconds is None:
            return
        if self.claim_token is None:
            return
        while True:
            await asyncio.sleep(self.heartbeat_interval_seconds)
            store.heartbeat_attempt(
                attempt_id,
                queue_item_id,
                self.worker_name,
                claim_token=self.claim_token,
                lease_ms=self.lease_ms,
                now_ms=current_time_ms(),
            )

    def _executor_for_id(self, agent_id: str) -> ExecutableAgent | None:
        for executor in self.executors:
            if executor.agent_id == agent_id:
                return executor
        return None

    def _retry_policy_for_agent(self, agent_id: str) -> RetryPolicy:
        executor = self._executor_for_id(agent_id)
        if executor is None or executor.definition.retry_policy is None:
            return self.retry_policy
        return executor.definition.retry_policy

    def _resolve_queue_item(self, queue_item: RoutedQueueItem | str) -> RoutedQueueItem:
        if isinstance(queue_item, RoutedQueueItem):
            return queue_item
        return self._stored_queue_item(queue_item)

    def _stored_queue_item(self, queue_item_id: str) -> RoutedQueueItem:
        if isinstance(self.event_sink, QueueItemRecordReader):
            record = self.event_sink.queue_item(queue_item_id)
            if record is not None:
                return queue_item_from_record(record)
        reader = self._event_reader()
        for event in reversed(
            reader.list_events(Filter(event_type="runtime.queue_item.available"))
        ):
            if event.payload.get("queue_item_id") == queue_item_id:
                return routed_queue_item_from_event(event)
        raise LookupError(f"queue item {queue_item_id!r} is not available")

    async def _route_claimed_queue_item(
        self,
        triggering_event: Event,
        queue_item: RoutedQueueItem,
    ) -> list[Event]:
        matching_routes = self.matching_routes(triggering_event)
        if not matching_routes:
            return [
                self._append_queue_item_event_for_target(
                    triggering_event,
                    queue_item.queue_item_id,
                    "",
                    event_suffix="unhandled",
                    status="unhandled",
                )
            ]
        if len(matching_routes) == 1:
            route = matching_routes[0]
            bound_item = RoutedQueueItem(
                queue_item_id=queue_item.queue_item_id,
                event_id=queue_item.event_id,
                target_agent=route.agent_id,
            )
            executor = self._executor_for_id(route.agent_id)
            if executor is None:
                return self._missing_executor_events(triggering_event, bound_item)
            return await self._run_agent(executor, triggering_event, bound_item)

        lifecycle_events = [
            self._append_queue_item_event_for_target(
                triggering_event,
                queue_item.queue_item_id,
                "",
                event_suffix="completed",
                status="completed",
            )
        ]
        for route in matching_routes:
            queue_item_id = queue_item_id_for_event(route, triggering_event)
            lifecycle_events.append(
                self._append_queue_item_event(
                    triggering_event,
                    route,
                    queue_item_id,
                    event_suffix="available",
                    status="available",
                )
            )
        return lifecycle_events

    def _stored_event(self, event_id: str) -> Event:
        if isinstance(self.event_sink, EventStoreProtocol):
            event = self.event_sink.get(event_id)
            if event is not None:
                return event
        reader = self._event_reader()
        for event in reader.list_events(Filter()):
            if event.id == event_id:
                return event
        raise LookupError(f"event {event_id!r} was not found")

    def _terminal_queue_item_event(self, queue_item_id: str) -> Event | None:
        reader = self._event_reader()
        for event in reversed(
            reader.list_events(Filter(event_type_prefix="runtime.queue_item."))
        ):
            if event.payload.get("queue_item_id") == queue_item_id:
                if event.event_type in TERMINAL_QUEUE_ITEM_EVENT_TYPES:
                    return event
                return None
        return None

    def _event_reader(self) -> EventReader:
        if isinstance(self.event_sink, EventReader):
            return self.event_sink
        raise RuntimeError("queue item execution requires a readable event store")

    def _next_attempt_number(self, queue_item_id: str) -> int:
        attempt_numbers: list[int] = []
        for event in self._event_reader().list_events(
            Filter(event_type_prefix="runtime.attempt.")
        ):
            if event.payload.get("queue_item_id") != queue_item_id:
                continue
            attempt_number = event.payload.get("attempt_number")
            if isinstance(attempt_number, int):
                attempt_numbers.append(attempt_number)
        return max(attempt_numbers, default=0) + 1

    def _missing_executor_events(
        self,
        triggering_event: Event,
        queue_item: RoutedQueueItem,
    ) -> list[Event]:
        error = f"no executor registered for {queue_item.target_agent!r}"
        return [
            self._append_queue_item_event_for_target(
                triggering_event,
                queue_item.queue_item_id,
                queue_item.target_agent,
                event_suffix="unhandled",
                status="unhandled",
                error=error,
            )
        ]

    def _append_queue_item_event(
        self,
        triggering_event: Event,
        route: AgentRoute,
        queue_item_id: str,
        *,
        event_suffix: str,
        status: QueueItemStatus,
        attempt_number: int | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        **payload_extra: Any,
    ) -> Event:
        return self._append_queue_item_event_for_target(
            triggering_event,
            queue_item_id,
            route.agent_id,
            event_suffix=event_suffix,
            status=status,
            attempt_number=attempt_number,
            session_id=session_id,
            run_id=run_id,
            **payload_extra,
        )

    def _append_queue_item_event_for_target(
        self,
        triggering_event: Event,
        queue_item_id: str,
        target_agent: str,
        *,
        event_suffix: str,
        status: QueueItemStatus,
        attempt_number: int | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        **payload_extra: Any,
    ) -> Event:
        queue_item = QueueItem(
            queue_item_id=queue_item_id,
            event_id=triggering_event.id,
            target_agent=target_agent,
            status=status,
        )
        return self._append_lifecycle_event(
            f"runtime.queue_item.{event_suffix}",
            triggering_event,
            queue_item_event_payload(queue_item, **payload_extra),
            idempotency_key=queue_item_idempotency_key(
                triggering_event,
                target_agent,
                event_suffix,
                attempt_number=attempt_number,
            ),
            session_id=session_id,
            run_id=run_id,
        )

    def _append_attempt_event(
        self,
        triggering_event: Event,
        agent: ExecutableAgent,
        queue_item_id: str,
        attempt_id: str,
        attempt_number: int,
        *,
        event_suffix: str,
        status: AttemptStatus,
        started_at: str,
        finished_at: str | None = None,
        error: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
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
            session_id=session_id
            if session_id is not None
            else triggering_event.session_id,
            run_id=run_id if run_id is not None else triggering_event.run_id,
        )
        if self.worker_name is not None:
            payload_extra = {"worker_name": self.worker_name, **payload_extra}
        return self._append_lifecycle_event(
            f"runtime.attempt.{event_suffix}",
            triggering_event,
            attempt_event_payload(attempt, **payload_extra),
            idempotency_key=attempt_idempotency_key(
                queue_item_id,
                attempt_number,
                event_suffix,
            ),
            session_id=session_id,
            run_id=run_id,
        )

    def _failed_agent_events(
        self,
        exc: Exception,
        triggering_event: Event,
        agent: ExecutableAgent,
        queue_item_id: str,
        attempt_id: str,
        attempt_number: int,
        started_at: str,
        session_id: str | None,
        run_id: str | None,
    ) -> list[Event]:
        error = f"{type(exc).__name__}: {exc}"
        error_code = error_code_for_exception(exc)
        failed_attempt = self._append_attempt_event(
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
            error_code=error_code,
            session_id=session_id,
            run_id=run_id,
        )
        retry_policy = self._retry_policy_for_agent(agent.definition.agent_id)
        failure_class = retry_policy.classify(error_code)
        if failure_class == "permanent" or attempt_number >= retry_policy.max_attempts:
            reason = "permanent" if failure_class == "permanent" else "exhausted"
            return [
                failed_attempt,
                self._append_dead_lettered_queue_item_event(
                    triggering_event,
                    agent,
                    queue_item_id,
                    attempt_number,
                    attempt_id,
                    error_code=error_code,
                    error=error,
                    reason=reason,
                    session_id=session_id,
                    run_id=run_id,
                ),
            ]
        return [
            failed_attempt,
            self.schedule_retry(
                RoutedQueueItem(
                    queue_item_id=queue_item_id,
                    event_id=triggering_event.id,
                    target_agent=agent.definition.agent_id,
                ),
                attempt_number=attempt_number + 1,
                policy=retry_policy,
            ),
        ]

    def _append_dead_lettered_queue_item_event(
        self,
        triggering_event: Event,
        agent: ExecutableAgent,
        queue_item_id: str,
        attempt_count: int,
        last_attempt_id: str,
        *,
        error_code: str,
        error: str,
        reason: str,
        session_id: str | None,
        run_id: str | None,
    ) -> Event:
        return self._append_queue_item_event(
            triggering_event,
            agent.route,
            queue_item_id,
            event_suffix="dead_lettered",
            status="dead_lettered",
            attempt_number=attempt_count,
            reason=reason,
            attempt_count=attempt_count,
            last_error={"code": error_code, "message": error},
            last_attempt_id=last_attempt_id,
            dead_lettered_at=event_timestamp(),
            session_id=session_id,
            run_id=run_id,
        )

    def _terminal_agent_events(
        self,
        result: dict[str, Any],
        triggering_event: Event,
        agent: ExecutableAgent,
        queue_item_id: str,
        attempt_id: str,
        attempt_number: int,
        started_at: str,
        session_id: str | None,
        run_id: str | None,
    ) -> list[Event]:
        cancelled = result.get("outcome") in {"aborted", "cancelled"}
        attempt_status: AttemptStatus = "cancelled" if cancelled else "completed"
        queue_status: QueueItemStatus = "cancelled" if cancelled else "completed"
        attempt_payload_extra: dict[str, Any] = {"result": result}
        summary = result.get("summary")
        if not isinstance(summary, str):
            summary = result.get("final_answer")
        if isinstance(summary, str):
            attempt_payload_extra["summary"] = summary
        for key in ("events", "tool_calls", "usage"):
            value = result.get(key)
            if value is not None:
                attempt_payload_extra[key] = value
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
                session_id=session_id,
                run_id=run_id,
                **attempt_payload_extra,
            ),
            self._append_queue_item_event(
                triggering_event,
                agent.route,
                queue_item_id,
                event_suffix=queue_status,
                status=queue_status,
                result=result,
                session_id=session_id,
                run_id=run_id,
            ),
        ]

    def _append_lifecycle_event(
        self,
        event_type: str,
        triggering_event: Event,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        session_id: str | None = None,
        run_id: str | None = None,
    ) -> Event:
        draft = DraftEvent(
            event_type,
            "zeta",
            payload,
            idempotency_key=idempotency_key,
            caused_by=triggering_event.id,
            session_id=(
                session_id if session_id is not None else triggering_event.session_id
            ),
            run_id=run_id if run_id is not None else triggering_event.run_id,
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
            queue_item_event_payload(queue_item),
            idempotency_key=unhandled_queue_item_idempotency_key(triggering_event),
        )

    def _publish(self, event: Event) -> None:
        if self.publish_callback is not None:
            self.publish_callback(event)

    def _agent_event_publisher(
        self,
        agent: ExecutableAgent,
        triggering_event: Event,
        queue_item_id: str,
        attempt_id: str,
        session_id: str | None,
        run_id: str | None,
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
                session_id=draft.session_id or session_id,
                run_id=draft.run_id or run_id,
                turn_id=draft.turn_id or triggering_event.turn_id,
            )
            if tagged.event_type.startswith("runtime.egress."):
                outcome = await self.publish_event(tagged)
            else:
                outcome = await self.publish_and_run(tagged)
            return outcome.event

        return publish


def invocation_session_id(definition: AgentDefinition, event: Event) -> str | None:
    if event.event_type == "session.turn.requested" and event.session_id is not None:
        return event.session_id
    return agent_session_id(definition, event)


def reject_reserved_runtime_event(draft: DraftEvent) -> None:
    if draft.event_type.startswith(RESERVED_RUNTIME_EVENT_PREFIXES):
        raise ReservedRuntimeEventError(draft.event_type)


def event_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def current_time_ms() -> int:
    return time.time_ns() // 1_000_000
