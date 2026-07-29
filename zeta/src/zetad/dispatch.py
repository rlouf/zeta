"""Append events, publish them, and route matching agents."""

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol

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
)
from zetad.coordinator import AttemptCoordinator
from zetad.lifecycle import LifecycleRecorder
from zetad.metrics import MetricAttribute
from zetad.queue import (
    TERMINAL_QUEUE_ITEM_EVENT_TYPES,
    QueueItemStatus,
    RoutedQueueItem,
    queue_item_from_record,
    queue_item_id_for_event,
    routed_queue_item_from_event,
)
from zetad.retry import RetryPolicy
from zetad.router import EventRouter

__all__ = [
    "AgentDefinition",
    "AgentInvocation",
    "AgentRoute",
    "EventDispatcher",
    "ExecutableAgent",
    "DispatchOutcome",
    "EventPattern",
    "QueueingDispatcher",
    "ReservedRuntimeEventError",
    "RouteOutcome",
    "RetryPolicy",
    "RuntimeQueueStore",
    "TerminalQueueItemError",
]

RESERVED_RUNTIME_EVENT_PREFIXES = (
    "runtime.queue_item.",
    "runtime.attempt.",
    "runtime.egress.",
    "runtime.effect.",
    "runtime.project_snapshot.",
)


class RuntimeQueueStore(Protocol):
    """Operational queue/attempt/lock index a daemon dispatcher requires.

    Backed by the runtime event store's SQLite projections. Unlike the bare
    event log, it supports claim fencing, attempt heartbeats, and lock renewal
    for durable, at-least-once queue execution; `QueueingDispatcher` requires
    one so those guarantees are explicit rather than feature-detected.
    """

    def queue_item(self, queue_item_id: str) -> Mapping[str, Any] | None:
        """Return one queue item row by id."""

    def queue_item_attempt_count(self, queue_item_id: str) -> int:
        """Return the highest attempt number recorded for a queue item."""

    def queue_claim_is_current(
        self,
        queue_item_id: str,
        worker_name: str,
        claim_token: str,
    ) -> bool:
        """Return whether the queue claim token still owns the item."""

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

    def renew_locks(
        self,
        keys: Iterable[str],
        owner: str,
        *,
        lease_ms: int,
        now_ms: int,
    ) -> bool:
        """Refresh held mutual-exclusion locks for a running queue item."""

    def observe_runtime_metric(
        self,
        name: str,
        value: float,
        **attributes: MetricAttribute,
    ) -> None:
        """Record one runtime health measurement."""

    def reconcile_expired_queue_claims(self, *, now_ms: int) -> int:
        """Release expired queue claims before claiming more work."""

    def reconcile_expired_locks(self, *, now_ms: int) -> int:
        """Release expired execution locks before claiming more work."""

    def claim_next_queue_item(
        self,
        worker_name: str,
        *,
        lease_ms: int,
        now_ms: int,
        exclude_queue_item_ids: set[str],
    ) -> Any:
        """Claim the next available queue item."""

    def acquire_locks(
        self,
        keys: Iterable[str],
        owner: str,
        *,
        lease_ms: int,
        now_ms: int,
    ) -> bool:
        """Acquire the locks needed for one attempt."""

    def release_locks(self, keys: Iterable[str], owner: str) -> int:
        """Release locks after one attempt."""

    def release_queue_claim(
        self,
        queue_item_id: str,
        worker_name: str,
        *,
        claim_token: str,
        now_ms: int,
    ) -> bool:
        """Release a queue claim that could not acquire its locks."""


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
    """Compatibility facade that routes and runs work immediately in-process.

    Production workers use `RuntimeCoordinator` and `QueueingDispatcher`. This
    facade retains recursive `publish_and_run` behavior for embedded callers
    and tests, without durable claim fencing or heartbeat renewal.
    """

    def __init__(
        self,
        event_sink: EventWriter,
        *,
        routes: Iterable[AgentRoute] = (),
        executors: Iterable[ExecutableAgent] = (),
        publish_event: Callable[[Event], None] | None = None,
        retry_policy: RetryPolicy | None = None,
        recursive_publication: bool = True,
    ) -> None:
        self.event_sink = event_sink
        self.executors = tuple(executors)
        route_by_identity = {route: route for route in routes}
        for executor in self.executors:
            route_by_identity[executor.route] = executor.route
        self.routes = tuple(route_by_identity.values())
        self.router = EventRouter(self.routes)
        self.publish_callback = publish_event
        self.worker_name: str | None = None
        self.retry_policy = retry_policy or RetryPolicy()
        self.recursive_publication = recursive_publication
        self.lifecycle = LifecycleRecorder(
            event_sink,
            publish_event=publish_event,
        )
        self.attempt_coordinator = AttemptCoordinator(
            self.lifecycle,
            claim_is_current=self._queue_claim_is_current,
            next_attempt_number=self._next_attempt_number,
            start_heartbeat=self._start_attempt_heartbeat,
            stop_heartbeat=self._stop_attempt_heartbeat,
            event_publisher=self._agent_event_publisher,
            retry_scheduler=self.schedule_retry,
            retry_policy=self.retry_policy,
            blocking_unsafe_effect=self._blocking_unsafe_effect,
        )

    async def publish_event(
        self,
        draft: DraftEvent,
    ) -> DispatchOutcome:
        reject_reserved_runtime_event(draft)
        return await self._accept_event(draft)

    async def _accept_event(self, draft: DraftEvent) -> DispatchOutcome:
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
        plan = self.router.plan(event)
        if not plan.handled:
            return RouteOutcome(
                event,
                [self._append_unhandled_queue_item_event(event)],
                [],
            )
        for decision in plan.decisions:
            route = decision.route
            queue_item_id = decision.queue_item.queue_item_id
            lifecycle_events.append(
                self._append_queue_item_event(
                    event,
                    route,
                    queue_item_id,
                    event_suffix="available",
                    status="available",
                )
            )
            queue_items.append(decision.queue_item)
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
        executor = self._executor_for_id(
            routed_queue_item.target_agent,
            project_generation=routed_queue_item.project_generation,
            triggering_event=triggering_event,
        )
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
            routed_queue_item.target_agent,
            project_generation=routed_queue_item.project_generation,
        )
        previous_attempt_number = max(next_attempt_number - 1, 1)
        not_before = current_time_ms() + retry_policy.delay_ms(previous_attempt_number)
        generation_payload = (
            {"project_generation": routed_queue_item.project_generation}
            if routed_queue_item.project_generation is not None
            else {}
        )
        available = self._append_queue_item_event_for_target(
            triggering_event,
            routed_queue_item.queue_item_id,
            routed_queue_item.target_agent,
            event_suffix="available",
            status="available",
            attempt_number=next_attempt_number,
            not_before=not_before,
            **generation_payload,
        )
        self._observe_runtime_metric(
            "runtime.retries_scheduled",
            1,
            target_agent=routed_queue_item.target_agent,
        )
        return available

    def _observe_runtime_metric(
        self,
        name: str,
        value: float,
        **attributes: MetricAttribute,
    ) -> None:
        del name, value, attributes

    def matching_routes(self, event: Event) -> list[AgentRoute]:
        return list(self.router.matching_routes(event))

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
        return await self.attempt_coordinator.run(agent, triggering_event, queue_item)

    def _queue_claim_is_current(self, queue_item_id: str) -> bool:
        """Whether this dispatcher still owns the claim; always true in-process."""
        return True

    def _blocking_unsafe_effect(self, queue_item_id: str) -> str | None:
        statuses: dict[str, str] = {}
        for event in self._event_reader().list_events(
            Filter(event_type_prefix="runtime.effect.")
        ):
            if event.payload.get("queue_item_id") != queue_item_id:
                continue
            if event.payload.get("semantics") != "unsafe_to_retry":
                continue
            effect_key = event.payload.get("effect_key")
            if not isinstance(effect_key, str):
                continue
            statuses[effect_key] = event.event_type.rsplit(".", 1)[-1]
        for effect_key, status in statuses.items():
            if status in {"started", "ambiguous"}:
                return effect_key
        return None

    def _start_attempt_heartbeat(
        self,
        attempt_id: str,
        queue_item_id: str,
        lock_keys: Iterable[str] = (),
    ) -> asyncio.Task[None] | None:
        """No durable lease to keep alive in-process."""
        return None

    async def _stop_attempt_heartbeat(
        self,
        heartbeat_task: asyncio.Task[None] | None,
    ) -> None:
        if heartbeat_task is None:
            return
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task

    def _executor_for_id(
        self,
        agent_id: str,
        *,
        project_generation: str | None = None,
        triggering_event: Event | None = None,
    ) -> ExecutableAgent | None:
        for executor in self.executors:
            if executor.agent_id != agent_id:
                continue
            if (
                project_generation is not None
                and executor.definition.project_generation != project_generation
            ):
                continue
            if triggering_event is not None and not executor.definition.accepts(
                triggering_event
            ):
                continue
            return executor
        return None

    def _retry_policy_for_agent(
        self,
        agent_id: str,
        *,
        project_generation: str | None = None,
    ) -> RetryPolicy:
        executor = self._executor_for_id(
            agent_id,
            project_generation=project_generation,
        )
        if executor is None or executor.definition.retry_policy is None:
            return self.retry_policy
        return executor.definition.retry_policy

    def _resolve_queue_item(self, queue_item: RoutedQueueItem | str) -> RoutedQueueItem:
        if isinstance(queue_item, RoutedQueueItem):
            return queue_item
        return self._stored_queue_item(queue_item)

    def _stored_queue_item(self, queue_item_id: str) -> RoutedQueueItem:
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
                project_generation=route.project_generation,
            )
            executor = self._executor_for_id(
                route.agent_id,
                project_generation=route.project_generation,
                triggering_event=triggering_event,
            )
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
        return self.lifecycle.queue_item(
            triggering_event,
            route,
            queue_item_id,
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
        return self.lifecycle.queue_item_for_target(
            triggering_event,
            queue_item_id,
            target_agent,
            event_suffix=event_suffix,
            status=status,
            attempt_number=attempt_number,
            session_id=session_id,
            run_id=run_id,
            **payload_extra,
        )

    def _append_unhandled_queue_item_event(self, triggering_event: Event) -> Event:
        return self.lifecycle.unhandled(triggering_event)

    def _publish(self, event: Event) -> None:
        self.lifecycle.publish(event)

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
            if tagged.event_type.startswith(("runtime.egress.", "runtime.effect.")):
                outcome = await self._accept_event(tagged)
            else:
                outcome = (
                    await self.publish_and_run(tagged)
                    if self.recursive_publication
                    else await self.publish_event(tagged)
                )
            return outcome.event

        return publish


class QueueingDispatcher(EventDispatcher):
    """Daemon dispatcher with durable claim fencing and attempt heartbeats.

    Requires a `RuntimeQueueStore` (the runtime event store's SQLite
    projections) so claim fencing and lease renewal are guaranteed by the type
    rather than silently skipped when a capability is absent.
    """

    def __init__(
        self,
        event_sink: EventWriter,
        queue_store: RuntimeQueueStore,
        *,
        routes: Iterable[AgentRoute] = (),
        executors: Iterable[ExecutableAgent] = (),
        publish_event: Callable[[Event], None] | None = None,
        retry_policy: RetryPolicy | None = None,
        worker_name: str | None = None,
        heartbeat_interval_seconds: float | None = None,
        lease_ms: int = 60_000,
        claim_token: str | None = None,
    ) -> None:
        super().__init__(
            event_sink,
            routes=routes,
            executors=executors,
            publish_event=publish_event,
            retry_policy=retry_policy,
            recursive_publication=False,
        )
        self.queue_store = queue_store
        self.worker_name = worker_name
        self.lifecycle.worker_name = worker_name
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.lease_ms = lease_ms
        self.claim_token = claim_token

    async def run_next(
        self,
        *,
        skipped_queue_items: set[str] | None = None,
    ) -> tuple[str, list[Event]] | None:
        """Claim, lock, and execute one queue item through the durable harness."""
        if self.worker_name is None:
            raise RuntimeError("queueing dispatch requires a worker name")
        skipped = skipped_queue_items or set()
        while True:
            now_ms = current_time_ms()
            self.queue_store.reconcile_expired_queue_claims(now_ms=now_ms)
            self.queue_store.reconcile_expired_locks(now_ms=now_ms)
            claim = self.queue_store.claim_next_queue_item(
                self.worker_name,
                lease_ms=self.lease_ms,
                now_ms=now_ms,
                exclude_queue_item_ids=skipped,
            )
            if claim is None:
                return None
            lock_keys = self._lock_keys_for_queue_item(claim.queue_item_id)
            if not self.queue_store.acquire_locks(
                lock_keys,
                claim.token,
                lease_ms=self.lease_ms,
                now_ms=current_time_ms(),
            ):
                self.queue_store.release_queue_claim(
                    claim.queue_item_id,
                    self.worker_name,
                    claim_token=claim.token,
                    now_ms=current_time_ms(),
                )
                skipped.add(claim.queue_item_id)
                continue
            self.claim_token = claim.token
            try:
                return claim.queue_item_id, await self.run_queue_item(
                    claim.queue_item_id
                )
            finally:
                self.claim_token = None
                self.queue_store.release_locks(lock_keys, claim.token)

    def _lock_keys_for_queue_item(self, queue_item_id: str) -> tuple[str, ...]:
        queue_item = self.queue_store.queue_item(queue_item_id)
        if queue_item is None:
            return ()
        target_agent = queue_item.get("target_agent")
        if isinstance(target_agent, str) and target_agent:
            executor = self._executor_for_id(target_agent)
            return () if executor is None else executor.definition.lock_keys
        event_id = queue_item.get("event_id")
        if not isinstance(event_id, str):
            return ()
        event = self._stored_event(event_id)
        matching = [
            executor
            for executor in self.executors
            if executor.definition.accepts(event)
        ]
        if len(matching) != 1:
            return ()
        return matching[0].definition.lock_keys

    def _observe_runtime_metric(
        self,
        name: str,
        value: float,
        **attributes: MetricAttribute,
    ) -> None:
        self.queue_store.observe_runtime_metric(name, value, **attributes)

    def _stored_queue_item(self, queue_item_id: str) -> RoutedQueueItem:
        record = self.queue_store.queue_item(queue_item_id)
        if record is not None:
            return queue_item_from_record(record)
        return super()._stored_queue_item(queue_item_id)

    def _next_attempt_number(self, queue_item_id: str) -> int:
        return self.queue_store.queue_item_attempt_count(queue_item_id) + 1

    def _queue_claim_is_current(self, queue_item_id: str) -> bool:
        if self.worker_name is None or self.claim_token is None:
            return True
        return self.queue_store.queue_claim_is_current(
            queue_item_id,
            self.worker_name,
            self.claim_token,
        )

    def _start_attempt_heartbeat(
        self,
        attempt_id: str,
        queue_item_id: str,
        lock_keys: Iterable[str] = (),
    ) -> asyncio.Task[None] | None:
        if (
            self.worker_name is None
            or self.claim_token is None
            or self.heartbeat_interval_seconds is None
            or self.heartbeat_interval_seconds <= 0
        ):
            return None
        return asyncio.create_task(
            self._heartbeat_attempt(attempt_id, queue_item_id, tuple(lock_keys))
        )

    async def _heartbeat_attempt(
        self,
        attempt_id: str,
        queue_item_id: str,
        lock_keys: tuple[str, ...],
    ) -> None:
        if (
            self.worker_name is None
            or self.claim_token is None
            or self.heartbeat_interval_seconds is None
        ):
            return
        while True:
            expected_at = time.monotonic() + self.heartbeat_interval_seconds
            await asyncio.sleep(self.heartbeat_interval_seconds)
            self.queue_store.observe_runtime_metric(
                "runtime.heartbeat_delay_ms",
                max(0.0, (time.monotonic() - expected_at) * 1000),
                queue_item_id=queue_item_id,
            )
            now_ms = current_time_ms()
            heartbeat_current = self.queue_store.heartbeat_attempt(
                attempt_id,
                queue_item_id,
                self.worker_name,
                claim_token=self.claim_token,
                lease_ms=self.lease_ms,
                now_ms=now_ms,
            )
            if heartbeat_current and lock_keys:
                self.queue_store.renew_locks(
                    lock_keys,
                    self.claim_token,
                    lease_ms=self.lease_ms,
                    now_ms=now_ms,
                )


def reject_reserved_runtime_event(draft: DraftEvent) -> None:
    if draft.event_type.startswith(RESERVED_RUNTIME_EVENT_PREFIXES):
        raise ReservedRuntimeEventError(draft.event_type)


def current_time_ms() -> int:
    return time.time_ns() // 1_000_000
