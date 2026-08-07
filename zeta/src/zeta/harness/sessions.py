"""Session scope for one agent invocation.

A session is the timeline scope, so it decides what an agent remembers. One
rule answers what identifies a session: the agent, the triggering event, or a
value the event carries. The agent declares which.

The string derivations live in `zeta.ids`, and the rendering in
`zeta.harness.templates`. This module names which rule applies to an
invocation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from zeta import ids
from zeta.authoring.spec import MASTER_AGENT_ID, SESSION_MESSAGE_REQUESTED, AgentSpec
from zeta.events import DraftEvent, Event
from zeta.harness.routing import (
    AgentDefinition,
    ExecutableAgent,
    is_session_message_for,
    is_wait_continuation_for,
)
from zeta.harness.templates import agent_session_id as session_id_for
from zeta.journal.store import Filter

if TYPE_CHECKING:
    from zeta.harness.store import RuntimeEventStore

TERMINAL_SESSION_QUEUE_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "dead_lettered", "unhandled"}
)


class SessionNotFound(LookupError):
    """The durable history does not contain the requested session."""


class SessionOwnerConflict(RuntimeError):
    """One session id refers to more than one owning agent."""


class SessionOwnerUnavailable(RuntimeError):
    """The current generation cannot continue a session's authored agent."""


@dataclass
class _SessionSources:
    owners: set[str] = field(default_factory=set)
    queue_items: list[Mapping[str, Any]] = field(default_factory=list)
    attempts: list[Mapping[str, Any]] = field(default_factory=list)
    waits: list[Mapping[str, Any]] = field(default_factory=list)


def agent_session_id(definition: AgentDefinition, event: Event) -> str:
    """Return the durable runtime session id for an authored agent invocation."""
    return session_id_for(definition.agent_id, definition.session, event)


def agent_run_id(attempt_id: str) -> str:
    """Return the run id derived from an attempt."""
    return ids.derived_run_id(attempt_id)


def invocation_session_id(agent: ExecutableAgent, event: Event) -> str | None:
    """Return the session an invocation joins.

    A session turn carries its own session, because the caller owns the
    timeline. Every other event uses the agent's own session rule.
    """
    if (
        event.event_type == "session.turn.requested"
        or is_wait_continuation_for(event, agent.agent_id)
        or is_session_message_for(event, agent.agent_id)
    ) and event.session_id is not None:
        return event.session_id
    return agent_session_id(agent.definition, event)


def start_master_session(
    store: RuntimeEventStore,
    *,
    message: str,
    project_generation: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    durable_key = (
        f"session.start:{idempotency_key}" if idempotency_key is not None else None
    )
    return _store_session_message(
        store,
        message=message,
        agent_id=MASTER_AGENT_ID,
        session_id=ids.claimed_session_id(),
        project_generation=project_generation,
        durable_key=durable_key,
    )


def submit_session_message(
    store: RuntimeEventStore,
    *,
    message: str,
    agent_id: str,
    session_id: str,
    project_generation: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Store one addressed turn and its queue binding in one transaction."""
    durable_key = (
        f"session.message:{session_id}:{idempotency_key}"
        if idempotency_key is not None
        else None
    )
    return _store_session_message(
        store,
        message=message,
        agent_id=agent_id,
        session_id=session_id,
        project_generation=project_generation,
        durable_key=durable_key,
    )


def session_owner_for_submission(
    session: Mapping[str, Any],
    specs: Iterable[AgentSpec],
) -> str:
    """Require the current generation so a continuation cannot change agents."""
    agent_id = session.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id:
        raise SessionOwnerConflict("session has no single owner agent")
    if not any(spec.slug == agent_id and spec.enabled for spec in specs):
        raise SessionOwnerUnavailable(
            f"session owner {agent_id!r} is not enabled in the current project"
        )
    return agent_id


def _store_session_message(
    store: RuntimeEventStore,
    *,
    message: str,
    agent_id: str,
    session_id: str,
    project_generation: str,
    durable_key: str | None,
) -> dict[str, Any]:
    if not message:
        raise ValueError("session message must not be empty")
    if not agent_id or not session_id or not project_generation:
        raise ValueError("agent, session, and project generation are required")
    with store.transaction():
        requested = _existing_session_message(store, durable_key)
        if requested is None:
            _cancel_active_wait(store, agent_id, session_id)
            run_id = ids.claimed_run_id()
            requested = store.accept(
                DraftEvent(
                    SESSION_MESSAGE_REQUESTED,
                    "user",
                    {
                        "message": message,
                        "agent_id": agent_id,
                        "session_id": session_id,
                        "run_id": run_id,
                    },
                    idempotency_key=durable_key,
                    session_id=session_id,
                    run_id=run_id,
                )
            ).event
        run_id = requested.run_id
        if run_id is None:
            raise RuntimeError("stored session message is missing its run id")
        stored_agent_id = requested.payload.get("agent_id")
        stored_session_id = requested.session_id
        if not isinstance(stored_agent_id, str) or stored_session_id is None:
            raise RuntimeError("stored session message is missing its owner")
        queue_item_id = ids.queue_item_id(requested.id, stored_agent_id)
        store.accept(
            DraftEvent(
                "runtime.queue_item.available",
                "zeta",
                {
                    "queue_item_id": queue_item_id,
                    "event_id": requested.id,
                    "target_agent": stored_agent_id,
                    "project_generation": project_generation,
                    "session_id": stored_session_id,
                    "status": "available",
                },
                idempotency_key=ids.queue_item_idempotency_key(
                    requested.id,
                    stored_agent_id,
                    "available",
                ),
                caused_by=requested.id,
                session_id=stored_session_id,
                run_id=run_id,
            )
        )
    return {
        "event_id": requested.id,
        "queue_item_id": queue_item_id,
        "agent_id": stored_agent_id,
        "session_id": stored_session_id,
        "run_id": run_id,
        "status": "queued",
    }


def _existing_session_message(
    store: RuntimeEventStore,
    idempotency_key: str | None,
) -> Event | None:
    if idempotency_key is None:
        return None
    return next(
        (
            event
            for event in store.list_events(Filter(event_type=SESSION_MESSAGE_REQUESTED))
            if event.idempotency_key == idempotency_key
        ),
        None,
    )


def _cancel_active_wait(
    store: RuntimeEventStore,
    agent_id: str,
    session_id: str,
) -> None:
    active = next(
        (
            wait
            for wait in store.list_waits()
            if wait.get("session_id") == session_id and wait.get("status") == "active"
        ),
        None,
    )
    if active is None:
        return
    handle = active.get("handle")
    if not isinstance(handle, str):
        raise RuntimeError("active wait is missing its handle")
    store.cancel_resource(
        handle,
        reason="The user continued the session.",
        source_agent_id=agent_id,
        source_session_id=session_id,
    )


def project_sessions(
    queue_items: Iterable[Mapping[str, Any]],
    attempts: Iterable[Mapping[str, Any]],
    waits: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Derive current session activity from durable runtime records."""
    sources: dict[str, _SessionSources] = {}
    for item in queue_items:
        _add_session_source(sources, item, "target_agent", "queue_items")
    for attempt in attempts:
        _add_session_source(sources, attempt, "target_agent", "attempts")
    for wait in waits:
        _add_session_source(sources, wait, "agent_id", "waits")
    records = [
        _session_record_from_sources(session_id, source)
        for session_id, source in sources.items()
    ]
    return sorted(
        records,
        key=lambda record: (record["updated_at"], record["session_id"]),
        reverse=True,
    )


def session_record(
    records: Iterable[Mapping[str, Any]],
    session_id: str,
) -> dict[str, Any]:
    """Return one unambiguous session so addressed messages have one owner."""
    for record in records:
        if record.get("session_id") != session_id:
            continue
        conflicts = record.get("conflicting_agent_ids")
        if isinstance(conflicts, list) and conflicts:
            agents = ", ".join(str(agent_id) for agent_id in conflicts)
            raise SessionOwnerConflict(
                f"session {session_id!r} has conflicting owners: {agents}"
            )
        return dict(record)
    raise SessionNotFound(f"unknown session {session_id!r}")


def _add_session_source(
    sessions: dict[str, _SessionSources],
    record: Mapping[str, Any],
    owner_key: str,
    collection: str,
) -> None:
    session_id = record.get("session_id")
    owner = record.get(owner_key)
    if not isinstance(session_id, str) or not session_id:
        return
    source = sessions.setdefault(session_id, _SessionSources())
    if isinstance(owner, str) and owner:
        source.owners.add(owner)
    getattr(source, collection).append(record)


def _session_record_from_sources(
    session_id: str,
    source: _SessionSources,
) -> dict[str, Any]:
    running = [
        attempt for attempt in source.attempts if attempt.get("status") == "running"
    ]
    running_queue_ids = {
        attempt.get("queue_item_id")
        for attempt in running
        if isinstance(attempt.get("queue_item_id"), str)
    }
    queued = [
        item
        for item in source.queue_items
        if item.get("status") not in TERMINAL_SESSION_QUEUE_STATUSES
        and item.get("queue_item_id") not in running_queue_ids
    ]
    active_waits = [wait for wait in source.waits if wait.get("status") == "active"]
    latest_attempt = max(source.attempts, key=_attempt_time, default=None)
    active_attempt = max(running, key=_attempt_time, default=None)
    active_wait = max(active_waits, key=_record_time, default=None)
    owners = sorted(source.owners)
    record: dict[str, Any] = {
        "session_id": session_id,
        "agent_id": owners[0] if len(owners) == 1 else None,
        "status": _session_activity(running, queued, active_waits),
        "cancellation_requested": any(
            isinstance(item.get("cancel_requested_event_id"), str)
            and item.get("status") not in TERMINAL_SESSION_QUEUE_STATUSES
            for item in source.queue_items
        ),
        "active_run_id": _mapping_string(active_attempt, "run_id"),
        "queued_turns": len(queued),
        "active_wait": _active_wait_record(active_wait),
        "latest_run": _latest_run_record(latest_attempt),
        "updated_at": _session_updated_at(source),
    }
    if len(owners) > 1:
        record["conflicting_agent_ids"] = owners
    return record


def _session_activity(
    running: list[Mapping[str, Any]],
    queued: list[Mapping[str, Any]],
    active_waits: list[Mapping[str, Any]],
) -> str:
    if running:
        return "running"
    if queued:
        return "queued"
    if active_waits:
        return "waiting"
    return "idle"


def _active_wait_record(wait: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if wait is None:
        return None
    fields = wait.get("fields")
    return {
        "handle": _mapping_string(wait, "handle"),
        "event_type": _mapping_string(wait, "event_type"),
        "fields": dict(fields) if isinstance(fields, Mapping) else {},
        "deadline_ms": wait.get("deadline_ms"),
    }


def _latest_run_record(attempt: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if attempt is None:
        return None
    return {
        "run_id": _mapping_string(attempt, "run_id"),
        "status": _mapping_string(attempt, "status"),
    }


def _session_updated_at(source: _SessionSources) -> str:
    timestamp_ms = max(
        (
            *(_record_time(item) for item in source.queue_items),
            *(_attempt_time(attempt) for attempt in source.attempts),
            *(_record_time(wait) for wait in source.waits),
        ),
        default=0,
    )
    return (
        datetime.fromtimestamp(timestamp_ms / 1_000, tz=UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _record_time(record: Mapping[str, Any]) -> int:
    updated_at = record.get("updated_at")
    return updated_at if isinstance(updated_at, int) else 0


def _attempt_time(attempt: Mapping[str, Any]) -> int:
    value = attempt.get("finished_at") or attempt.get("started_at")
    if not isinstance(value, str):
        return 0
    try:
        return int(
            datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1_000
        )
    except ValueError:
        return 0


def _mapping_string(
    record: Mapping[str, Any] | None,
    key: str,
) -> str | None:
    if record is None:
        return None
    value = record.get(key)
    return value if isinstance(value, str) else None
