"""Dispatch queue item domain shapes."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from zeta.orchestration.agents import AgentRoute
from zeta.records.events import Event

QueueItemId = str

QueueItemStatus = Literal[
    "pending",
    "available",
    "claimed",
    "completed",
    "failed",
    "cancelled",
    "retry_scheduled",
    "unhandled",
]


@dataclass(frozen=True)
class QueueItem:
    """The durable assignment of one event to one target agent.

    Queue items describe routing state, not execution. Runtime lifecycle events
    record their status changes while the original event envelope carries
    session and run correlation.
    """

    queue_item_id: QueueItemId
    event_id: str
    target_agent: str
    status: QueueItemStatus


@dataclass(frozen=True)
class RoutedQueueItem:
    """Durable work made available by routing an event to an agent."""

    queue_item_id: str
    event_id: str
    target_agent: str


QUEUE_ITEM_STATUSES = frozenset(
    {
        "pending",
        "available",
        "claimed",
        "completed",
        "failed",
        "cancelled",
        "retry_scheduled",
        "unhandled",
    }
)

TERMINAL_QUEUE_ITEM_EVENT_TYPES = {
    "runtime.queue_item.completed",
    "runtime.queue_item.failed",
    "runtime.queue_item.cancelled",
    "runtime.queue_item.unhandled",
}


def project_queue_items(events: Iterable[Event]) -> list[QueueItem]:
    items: dict[str, QueueItem] = {}
    for event in events:
        item = project_one_queue_item(event)
        if item is None:
            continue
        items[item.queue_item_id] = item
    return list(items.values())


def project_one_queue_item(event: Event) -> QueueItem | None:
    if not event.event_type.startswith("runtime.queue_item."):
        return None
    queue_item_id = event.payload.get("queue_item_id")
    event_id = event.payload.get("event_id")
    target_agent = event.payload.get("target_agent")
    if (
        not isinstance(queue_item_id, str)
        or not isinstance(event_id, str)
        or not isinstance(target_agent, str)
    ):
        return None
    return QueueItem(
        queue_item_id=queue_item_id,
        event_id=event_id,
        target_agent=target_agent,
        status=_queue_item_status_from_event(event),
    )


def _queue_item_status_from_event(event: Event) -> QueueItemStatus:
    raw_status = event.payload.get("status")
    status = raw_status if isinstance(raw_status, str) else None
    if status not in QUEUE_ITEM_STATUSES:
        status = event.event_type.rsplit(".", 1)[-1]
    if status not in QUEUE_ITEM_STATUSES:
        raise ValueError(f"unsupported queue item status {status!r}")
    return cast("QueueItemStatus", status)


def queue_item_status_counts(
    items: Iterable[QueueItem],
) -> dict[QueueItemStatus, int]:
    counts: dict[QueueItemStatus, int] = {}
    for item in items:
        counts[item.status] = counts.get(item.status, 0) + 1
    return counts


def routed_queue_item_from_event(event: Event) -> RoutedQueueItem:
    queue_item_id = event.payload.get("queue_item_id")
    event_id = event.payload.get("event_id")
    target_agent = event.payload.get("target_agent")
    if (
        not isinstance(queue_item_id, str)
        or not isinstance(event_id, str)
        or not isinstance(target_agent, str)
    ):
        raise ValueError("available queue item event is missing required payload")
    return RoutedQueueItem(
        queue_item_id=queue_item_id,
        event_id=event_id,
        target_agent=target_agent,
    )


def queue_item_from_record(record: Mapping[str, Any]) -> RoutedQueueItem:
    return RoutedQueueItem(
        queue_item_id=str(record["queue_item_id"]),
        event_id=str(record["event_id"]),
        target_agent=str(record["target_agent"]),
    )


def queue_item_id_for_event(route: AgentRoute, event: Event) -> str:
    agent_id = route.agent_id.replace(":", "_").replace(".", "_")
    return f"qi_{event.id}_{agent_id}"


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


def terminal_queue_item_result(
    lifecycle_events: Iterable[Event],
    *,
    event_id: str,
    target_agent: str,
) -> dict[str, Any] | None:
    for event in reversed(tuple(lifecycle_events)):
        if event.event_type not in TERMINAL_QUEUE_ITEM_EVENT_TYPES:
            continue
        if event.payload.get("event_id") != event_id:
            continue
        if event.payload.get("target_agent") != target_agent:
            continue
        return terminal_queue_item_event_result(event)
    return None


def terminal_queue_item_event_result(event: Event) -> dict[str, Any] | None:
    if event.event_type not in TERMINAL_QUEUE_ITEM_EVENT_TYPES:
        return None
    result = event.payload.get("result")
    if isinstance(result, dict):
        return result_with_final_cursor(result, event)
    return result_with_final_cursor(terminal_fallback_result(event), event)


def terminal_fallback_result(event: Event) -> dict[str, Any]:
    raw_status = event.payload.get("status")
    fallback: dict[str, Any] = {
        "outcome": raw_status
        if isinstance(raw_status, str)
        else event.event_type.rsplit(".", 1)[-1]
    }
    raw_error = event.payload.get("error")
    error = raw_error if isinstance(raw_error, str) else None
    if error is not None:
        fallback["error"] = error
    return fallback


def result_with_final_cursor(result: dict[str, Any], event: Event) -> dict[str, Any]:
    if event.cursor is None:
        return dict(result)
    return {**result, "final_event_cursor": str(event.cursor)}
