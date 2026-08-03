"""Attempt execution state machine for one claimed queue item."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime
from typing import Any, cast

from zeta import ids
from zeta.effects import EffectDeliveryError
from zeta.events import DraftEvent, Event
from zeta.harness.attempts import AttemptStatus
from zeta.harness.lifecycle import LifecycleRecorder
from zeta.harness.queue import QueueItemStatus, RoutedQueueItem
from zeta.harness.retry import RetryPolicy, error_code_for_exception
from zeta.harness.routing import (
    AgentInvocation,
    ExecutableAgent,
)
from zeta.harness.sessions import invocation_session_id

HeartbeatTask = asyncio.Task[None] | None
AgentEventPublisher = Callable[[DraftEvent], Awaitable[Event]]
AgentEventPublisherFactory = Callable[
    [ExecutableAgent, Event, str, str, str | None, str | None],
    AgentEventPublisher,
]
RetryScheduler = Callable[..., Event]
CompletionBatch = Callable[[], AbstractContextManager[None]]


class AttemptCoordinator:
    """Own one attempt's legal start, execute, and terminal transitions."""

    def __init__(
        self,
        lifecycle: LifecycleRecorder,
        *,
        claim_is_current: Callable[[str], bool],
        next_attempt_number: Callable[[str], int],
        start_heartbeat: Callable[[str, str, Iterable[str]], HeartbeatTask],
        stop_heartbeat: Callable[[HeartbeatTask], Awaitable[None]],
        event_publisher: AgentEventPublisherFactory,
        retry_scheduler: RetryScheduler,
        retry_policy: RetryPolicy,
        blocking_unsafe_effect: Callable[[str], str | None] | None = None,
        completion_batch: CompletionBatch = nullcontext,
    ) -> None:
        self.lifecycle = lifecycle
        self.claim_is_current = claim_is_current
        self.next_attempt_number = next_attempt_number
        self.start_heartbeat = start_heartbeat
        self.stop_heartbeat = stop_heartbeat
        self.event_publisher = event_publisher
        self.retry_scheduler = retry_scheduler
        self.retry_policy = retry_policy
        self.blocking_unsafe_effect = blocking_unsafe_effect or (lambda _item: None)
        self.completion_batch = completion_batch

    async def run(
        self,
        agent: ExecutableAgent,
        triggering_event: Event,
        queue_item: RoutedQueueItem,
    ) -> list[Event]:
        queue_item_id = queue_item.queue_item_id
        events: list[Event] = []
        attempt_number = self.next_attempt_number(queue_item_id)
        attempt_id = ids.attempt_id(queue_item_id, attempt_number)
        run_id = ids.run_id_for_attempt(triggering_event.run_id, attempt_id)
        session_id = invocation_session_id(agent, triggering_event)
        if not self.claim_is_current(queue_item_id):
            return events
        events.append(
            self.lifecycle.queue_item(
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
            self.lifecycle.attempt(
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
        blocked_effect_key = self.blocking_unsafe_effect(queue_item_id)
        if blocked_effect_key is not None:
            events.extend(
                self.failed_events(
                    EffectDeliveryError(
                        blocked_effect_key,
                        "unsafe_to_retry",
                        f"unsafe effect {blocked_effect_key} may already have occurred",
                    ),
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
        heartbeat_task = self.start_heartbeat(
            attempt_id,
            queue_item_id,
            agent.definition.lock_keys,
        )
        try:
            try:
                result = await agent.run(
                    AgentInvocation(
                        agent.definition,
                        triggering_event,
                        publish_event=self.event_publisher(
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
                if not self.claim_is_current(queue_item_id):
                    return events
                events.extend(
                    self.failed_events(
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
            await self.stop_heartbeat(heartbeat_task)

        # The transaction fences the final claim check from competing writers.
        # Notifications wait for its commit so observers see only durable facts.
        with self.lifecycle.defer_publications():
            with self.completion_batch():
                if not self.claim_is_current(queue_item_id):
                    return events
                terminal = await self.terminal_events(
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
        events.extend(terminal)
        return events

    def failed_events(
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
        failed_attempt = self.lifecycle.attempt(
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
        retry_policy = agent.definition.retry_policy or self.retry_policy
        failure_class = retry_policy.classify(error_code)
        if failure_class == "permanent" or attempt_number >= retry_policy.max_attempts:
            reason = "permanent" if failure_class == "permanent" else "exhausted"
            return [
                failed_attempt,
                self.dead_lettered_event(
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
            self.retry_scheduler(
                RoutedQueueItem(
                    queue_item_id=queue_item_id,
                    event_id=triggering_event.id,
                    target_agent=agent.definition.agent_id,
                    project_generation=agent.definition.project_generation,
                ),
                attempt_number=attempt_number + 1,
                policy=retry_policy,
            ),
        ]

    def dead_lettered_event(
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
        return self.lifecycle.queue_item(
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

    async def terminal_events(
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
        cancelled = (
            result.get("outcome")
            in {
                "aborted",
                "cancelled",
            }
            or result.get("stop_reason") == "aborted"
        )
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
        finished_at = event_timestamp()
        attempt_event = self.lifecycle.attempt(
            triggering_event,
            agent,
            queue_item_id,
            attempt_id,
            attempt_number,
            event_suffix=attempt_status,
            status=attempt_status,
            started_at=started_at,
            finished_at=finished_at,
            session_id=session_id,
            run_id=run_id,
            **attempt_payload_extra,
        )
        events = [attempt_event]
        if not cancelled:
            events.extend(
                await self.publish_requests(
                    result,
                    triggering_event,
                    attempt_event,
                    finished_at,
                    agent,
                    queue_item_id,
                    attempt_id,
                    session_id,
                    run_id,
                )
            )
        events.append(
            self.lifecycle.queue_item(
                triggering_event,
                agent.route,
                queue_item_id,
                event_suffix=queue_status,
                status=queue_status,
                result=result,
                session_id=session_id,
                run_id=run_id,
            )
        )
        return events

    async def publish_requests(
        self,
        result: dict[str, Any],
        triggering_event: Event,
        completed_attempt: Event,
        completed_at: str,
        agent: ExecutableAgent,
        queue_item_id: str,
        attempt_id: str,
        session_id: str | None,
        run_id: str | None,
    ) -> list[Event]:
        """Publish here so failed or stale attempts cannot create downstream work."""
        requests = cast(
            list[dict[str, Any]],
            result.get("publish_event_requests", []),
        )
        publisher = self.event_publisher(
            agent,
            triggering_event,
            queue_item_id,
            attempt_id,
            session_id,
            run_id,
        )
        published: list[Event] = []
        for request in sorted(requests, key=lambda item: item["position"]):
            position = request["position"]
            at = request["at"]
            if publication_is_immediate(at, completed_at):
                published.append(
                    await publisher(
                        DraftEvent(
                            request["event_type"],
                            f"agent:{agent.definition.agent_id}",
                            request["payload"],
                            idempotency_key=(
                                f"agent.publish:{queue_item_id}:{position}"
                            ),
                            caused_by=completed_attempt.id,
                            session_id=session_id,
                            run_id=run_id,
                            turn_id=triggering_event.turn_id,
                        )
                    )
                )
                continue
            published.append(
                self.lifecycle.append(
                    "runtime.scheduled_event.created",
                    completed_attempt,
                    {
                        "handle": request["handle"],
                        "event_type": request["event_type"],
                        "payload": request["payload"],
                        "publish_at": at,
                        "source_agent_id": agent.definition.agent_id,
                        "source_session_id": session_id,
                        "source_queue_item_id": queue_item_id,
                        "position": position,
                    },
                    idempotency_key=f"agent.schedule:{queue_item_id}:{position}",
                    session_id=session_id,
                    run_id=run_id,
                )
            )
        return published


def event_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def publication_is_immediate(at: str | None, completed_at: str) -> bool:
    if at is None:
        return True
    return parse_timestamp(at) <= parse_timestamp(completed_at)


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
