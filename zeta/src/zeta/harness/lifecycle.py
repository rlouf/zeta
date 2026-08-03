"""Author and persist orchestration-owned lifecycle events."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from zeta import ids
from zeta.events import DraftEvent, Event
from zeta.harness.attempts import (
    Attempt,
    AttemptStatus,
    attempt_event_payload,
    attempt_idempotency_key,
)
from zeta.harness.queue import (
    QueueItem,
    QueueItemStatus,
    queue_item_event_payload,
    queue_item_idempotency_key,
    unhandled_queue_item_idempotency_key,
)
from zeta.harness.routing import AgentRoute, ExecutableAgent
from zeta.journal.store import EventWriter


class LifecycleRecorder:
    """Single append boundary for queue and attempt lifecycle facts."""

    def __init__(
        self,
        event_sink: EventWriter,
        *,
        publish_event: Callable[[Event], None] | None = None,
        worker_name: str | None = None,
    ) -> None:
        self.event_sink = event_sink
        self.publish_callback = publish_event
        self.worker_name = worker_name
        self._deferred_publications: ContextVar[list[Event] | None] = ContextVar(
            "zeta_deferred_lifecycle_publications",
            default=None,
        )

    @contextmanager
    def defer_publications(self) -> Iterator[None]:
        """Delay notifications so observers cannot see rolled-back events."""

        parent = self._deferred_publications.get()
        deferred: list[Event] = []
        token = self._deferred_publications.set(deferred)
        try:
            yield
        except BaseException:
            self._deferred_publications.reset(token)
            raise
        else:
            self._deferred_publications.reset(token)
            if parent is not None:
                parent.extend(deferred)
            elif self.publish_callback is not None:
                for event in deferred:
                    self.publish_callback(event)

    def queue_item(
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
        if route.project_generation is not None:
            payload_extra = {
                "project_generation": route.project_generation,
                **payload_extra,
            }
        return self.queue_item_for_target(
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

    def queue_item_for_target(
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
        return self.append(
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

    def attempt(
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
            session_id=(
                session_id if session_id is not None else triggering_event.session_id
            ),
            run_id=run_id if run_id is not None else triggering_event.run_id,
        )
        if self.worker_name is not None:
            payload_extra = {"worker_name": self.worker_name, **payload_extra}
        if agent.definition.project_generation is not None:
            payload_extra = {
                "project_generation": agent.definition.project_generation,
                **payload_extra,
            }
        if agent.definition.execution_manifest is not None:
            manifest = dict(agent.definition.execution_manifest)
            payload_extra = {
                "execution_manifest_id": manifest.get("id"),
                "execution_manifest": manifest,
                **payload_extra,
            }
        return self.append(
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

    def unhandled(self, triggering_event: Event) -> Event:
        queue_item_id = ids.unhandled_queue_item_id(triggering_event.id)
        queue_item = QueueItem(
            queue_item_id=queue_item_id,
            event_id=triggering_event.id,
            target_agent="",
            status="unhandled",
        )
        return self.append(
            "runtime.queue_item.unhandled",
            triggering_event,
            queue_item_event_payload(queue_item),
            idempotency_key=unhandled_queue_item_idempotency_key(triggering_event),
        )

    def append(
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
        self.publish(event)
        return event

    def publish(self, event: Event) -> None:
        deferred = self._deferred_publications.get()
        if deferred is not None:
            deferred.append(event)
        elif self.publish_callback is not None:
            self.publish_callback(event)
