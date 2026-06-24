"""Dispatch attempt domain shapes."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

AttemptId = str

AttemptStatus = Literal[
    "running",
    "completed",
    "failed",
    "cancelled",
]


@dataclass(frozen=True)
class Attempt:
    """One execution try for a queue item.

    Attempts carry session and run context so lifecycle events can be queried
    without decoding the triggering event, while retries remain tied to the
    same queue item through `attempt_number`.
    """

    attempt_id: AttemptId
    queue_item_id: str
    event_id: str
    attempt_number: int
    target_agent: str
    status: AttemptStatus
    started_at: str
    finished_at: str | None = None
    error: str | None = None
    session_id: str | None = None
    run_id: str | None = None


def attempt_idempotency_key(
    queue_item_id: str,
    attempt_number: int,
    status: str,
) -> str:
    return f"attempt:{queue_item_id}:{attempt_number}:{status}"


def attempt_event_payload(
    attempt: Attempt,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "attempt_id": attempt.attempt_id,
        "queue_item_id": attempt.queue_item_id,
        "event_id": attempt.event_id,
        "attempt_number": attempt.attempt_number,
        "target_agent": attempt.target_agent,
        "status": attempt.status,
        "started_at": attempt.started_at,
        "finished_at": attempt.finished_at,
        "error": attempt.error,
        "session_id": attempt.session_id,
        "run_id": attempt.run_id,
        **extra,
    }


def attempt_from_event_payload(payload: Mapping[str, Any]) -> Attempt | None:
    attempt_id = payload.get("attempt_id")
    queue_item_id = payload.get("queue_item_id")
    event_id = payload.get("event_id")
    attempt_number = payload.get("attempt_number")
    target_agent = payload.get("target_agent")
    started_at = payload.get("started_at")
    status = payload.get("status")
    if (
        not isinstance(attempt_id, str)
        or not isinstance(queue_item_id, str)
        or not isinstance(event_id, str)
        or not isinstance(attempt_number, int)
        or isinstance(attempt_number, bool)
        or not isinstance(target_agent, str)
        or not isinstance(started_at, str)
    ):
        return None
    if status not in {"running", "completed", "failed", "cancelled"}:
        raise ValueError(f"unsupported attempt status {status!r}")
    finished_at = payload.get("finished_at")
    error = payload.get("error")
    session_id = payload.get("session_id")
    run_id = payload.get("run_id")
    return Attempt(
        attempt_id=attempt_id,
        queue_item_id=queue_item_id,
        event_id=event_id,
        attempt_number=attempt_number,
        target_agent=target_agent,
        status=cast("AttemptStatus", status),
        started_at=started_at,
        finished_at=finished_at if isinstance(finished_at, str) else None,
        error=error if isinstance(error, str) else None,
        session_id=session_id if isinstance(session_id, str) else None,
        run_id=run_id if isinstance(run_id, str) else None,
    )
