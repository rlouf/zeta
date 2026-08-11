"""Runtime method adapters for the unified IPC boundary."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from zeta.authoring.spec import MASTER_AGENT_ID
from zeta.events import DraftEvent, Event
from zeta.harness.dispatch import QueueingDispatcher, ReservedRuntimeEventError
from zeta.harness.project import ProjectSnapshot, record_project_snapshot
from zeta.harness.protocols import UnauthorizedCancellation
from zeta.harness.sessions import (
    SessionNotFound,
    SessionOwnerConflict,
    SessionOwnerUnavailable,
    session_owner_for_submission,
    start_master_session,
    submit_session_message,
)
from zeta.harness.store import RuntimeEventStore
from zeta.ipc.connection import JsonRpcConnection, JsonRpcRouter, RpcError
from zeta.journal.store import EventReader, Filter
from zeta.journal.wire import event_to_wire
from zeta.loop.runtime_context import RuntimeContext

logger = logging.getLogger(__name__)


@dataclass
class IpcClient:
    """Hold runtime resources used by one initialized IPC client."""

    connection: JsonRpcConnection | None
    session: RuntimeContext
    dispatcher: QueueingDispatcher
    background_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    project_snapshot: ProjectSnapshot | None = None

    @property
    def peer_name(self) -> str:
        connection = self.connection
        if connection is None or connection.peer_name is None:
            return "ipc-client"
        return connection.peer_name

    def create_background_task(self, awaitable: Any) -> asyncio.Task[Any]:
        """Start background work and keep it alive until it completes."""

        task = asyncio.create_task(awaitable)
        self.background_tasks.add(task)
        task.add_done_callback(self._discard_background_task)
        return task

    def _discard_background_task(self, task: asyncio.Task[Any]) -> None:
        self.background_tasks.discard(task)
        if not task.cancelled():
            task.exception()


def invalid_params(code: str, message: str, **extra: Any) -> RpcError:
    """Build a stable JSON-RPC invalid-params error for route validation failures."""

    return RpcError(-32602, code, "Invalid params", {"message": message, **extra})


async def events_publish(
    params: dict[str, Any],
    client: IpcClient,
) -> dict[str, Any]:
    """Publish a source event and return only after its durable insertion."""

    supported = {
        "type",
        "payload",
        "idempotency_key",
        "caused_by",
        "session_id",
        "run_id",
        "turn_id",
    }
    unknown = sorted(set(params) - supported)
    if unknown:
        raise invalid_params(
            "invalid_params",
            f"events.publish contains unsupported fields: {', '.join(unknown)}",
            fields=unknown,
        )
    event_type = params.get("type")
    if not isinstance(event_type, str) or not event_type:
        raise invalid_params("invalid_event_type", "type must be non-empty")
    payload = params.get("payload")
    if not isinstance(payload, dict):
        raise invalid_params("invalid_payload", "payload must be an object")
    for field_name in (
        "idempotency_key",
        "caused_by",
        "session_id",
        "run_id",
        "turn_id",
    ):
        value = params.get(field_name)
        if value is not None and (not isinstance(value, str) or not value):
            raise invalid_params(
                "invalid_event",
                f"{field_name} must be null or a non-empty string",
            )
    try:
        draft = DraftEvent(
            event_type=event_type,
            source=client.peer_name,
            payload=payload,
            idempotency_key=params.get("idempotency_key"),
            caused_by=params.get("caused_by"),
            session_id=params.get("session_id"),
            run_id=params.get("run_id"),
            turn_id=params.get("turn_id"),
        )
    except (TypeError, ValueError) as exc:
        raise invalid_params(
            "invalid_event",
            f"Event values are invalid: {exc}",
        ) from exc

    try:
        outcome = await client.dispatcher.publish_event(draft)
    except ReservedRuntimeEventError as exc:
        raise invalid_params(
            "reserved_runtime_event",
            "events.publish cannot accept runtime lifecycle events",
            event_type=exc.event_type,
        ) from exc
    except (TypeError, ValueError) as exc:
        raise invalid_params(
            "invalid_event",
            f"Event values are invalid: {exc}",
        ) from exc

    if outcome.inserted and client.connection is not None:
        client.create_background_task(route_event(client, outcome.event))

    return {
        "inserted": outcome.inserted,
        "event": event_to_wire(outcome.event),
    }


async def route_event(client: IpcClient, event: Event) -> None:
    """Let IPC ingress return before the durable queue is drained."""

    event_id = event.id

    try:
        await client.dispatcher.drain()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Background event routing failed for event %s", event_id)


async def events_list(params: dict[str, Any], client: IpcClient) -> dict[str, Any]:
    """List durable events using the event store's constructor-shaped filter."""

    try:
        filter = Filter(**params)
    except ValueError as exc:
        raise invalid_params(
            "invalid_limit",
            str(exc),
        ) from exc
    except TypeError as exc:
        raise invalid_params(
            "invalid_params",
            f"Filter parameters are invalid: {exc}",
        ) from exc

    if filter.after_cursor is not None and (
        isinstance(filter.after_cursor, bool)
        or not isinstance(filter.after_cursor, int)
        or filter.after_cursor < 0
    ):
        raise invalid_params(
            "invalid_cursor",
            "after_cursor must be a non-negative integer",
        )
    if filter.limit is not None and (
        isinstance(filter.limit, bool)
        or not isinstance(filter.limit, int)
        or filter.limit < 0
    ):
        raise invalid_params("invalid_limit", "limit must be a non-negative integer")
    if not isinstance(client.session.event_sink, EventReader):
        raise RpcError(
            -32000,
            "events_unavailable",
            "Server error",
            {"message": "events.list is not configured"},
        )

    events = client.session.event_sink.list_events(filter)

    return {
        "events": [event_to_wire(event) for event in events],
        "next_cursor": events[-1].cursor if events else filter.after_cursor,
    }


async def session_start(params: dict[str, Any], client: IpcClient) -> dict[str, Any]:
    """Queue a master turn without giving the submitting client worker ownership."""
    message, idempotency_key = session_message_params(
        params,
        supported={"message", "idempotency_key"},
    )
    store, snapshot = session_submission_resources(client)
    after_cursor = latest_event_cursor(store)
    try:
        session_owner_for_submission(
            {"agent_id": MASTER_AGENT_ID},
            snapshot.project.specs,
        )
    except SessionOwnerUnavailable as exc:
        raise session_route_error(exc, None) from exc
    record_project_snapshot(store, snapshot)
    result = start_master_session(
        store,
        message=message,
        project_generation=snapshot.generation_id,
        idempotency_key=idempotency_key,
    )
    notify_committed_events(client, store, after_cursor)
    return result


async def session_send(params: dict[str, Any], client: IpcClient) -> dict[str, Any]:
    """Queue one message for the existing session owner in the current generation."""
    message, idempotency_key = session_message_params(
        params,
        supported={"session_id", "message", "idempotency_key"},
    )
    session_id = required_session_id(params)
    store, snapshot = session_submission_resources(client)
    after_cursor = latest_event_cursor(store)
    try:
        session = store.session_status(session_id)
        agent_id = session_owner_for_submission(session, snapshot.project.specs)
    except (SessionNotFound, SessionOwnerConflict, SessionOwnerUnavailable) as exc:
        raise session_route_error(exc, session_id) from exc
    record_project_snapshot(store, snapshot)
    result = submit_session_message(
        store,
        message=message,
        agent_id=agent_id,
        session_id=session_id,
        project_generation=snapshot.generation_id,
        idempotency_key=idempotency_key,
    )
    notify_committed_events(client, store, after_cursor)
    return result


async def session_status(params: dict[str, Any], client: IpcClient) -> dict[str, Any]:
    """Return one session activity record from durable runtime state."""
    reject_unknown_params(params, {"session_id"})
    session_id = required_session_id(params)
    store = session_runtime_store(client)
    try:
        return store.session_status(session_id)
    except (SessionNotFound, SessionOwnerConflict) as exc:
        raise session_route_error(exc, session_id) from exc


async def session_list(params: dict[str, Any], client: IpcClient) -> dict[str, Any]:
    """Return the derived authored-session catalog."""
    reject_unknown_params(params, set())
    store = session_runtime_store(client)
    try:
        return {"sessions": store.list_sessions()}
    except SessionOwnerConflict as exc:
        raise session_route_error(exc, None) from exc


def session_submission_resources(
    client: IpcClient,
) -> tuple[RuntimeEventStore, ProjectSnapshot]:
    store = session_runtime_store(client)
    snapshot = client.project_snapshot
    if snapshot is None:
        raise RpcError(
            -32000,
            "sessions_unavailable",
            "Server error",
            {"message": "session submission is not configured"},
        )
    return store, snapshot


def session_runtime_store(client: IpcClient) -> RuntimeEventStore:
    store = client.session.event_sink
    if not isinstance(store, RuntimeEventStore):
        raise RpcError(
            -32000,
            "sessions_unavailable",
            "Server error",
            {"message": "session queries require a durable runtime store"},
        )
    return store


def session_message_params(
    params: dict[str, Any],
    *,
    supported: set[str],
) -> tuple[str, str | None]:
    reject_unknown_params(params, supported)
    message = params.get("message")
    if not isinstance(message, str) or not message:
        raise invalid_params("invalid_message", "message must be non-empty")
    idempotency_key = params.get("idempotency_key")
    if idempotency_key is not None and (
        not isinstance(idempotency_key, str) or not idempotency_key
    ):
        raise invalid_params(
            "invalid_idempotency_key",
            "idempotency_key must be a non-empty string",
        )
    return message, idempotency_key


def required_session_id(params: dict[str, Any]) -> str:
    session_id = params.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise invalid_params("invalid_session_id", "session_id must be non-empty")
    return session_id


def reject_unknown_params(params: dict[str, Any], supported: set[str]) -> None:
    unknown = sorted(set(params) - supported)
    if unknown:
        raise invalid_params(
            "unknown_session_fields",
            f"session request contains unsupported fields: {', '.join(unknown)}",
            fields=unknown,
        )


def session_route_error(
    error: Exception,
    session_id: str | None,
) -> RpcError:
    if isinstance(error, SessionNotFound):
        code = "session_not_found"
        jsonrpc_code = -32004
    elif isinstance(error, SessionOwnerUnavailable):
        code = "session_owner_unavailable"
        jsonrpc_code = -32004
    else:
        code = "session_owner_conflict"
        jsonrpc_code = -32009
    data: dict[str, Any] = {"message": str(error)}
    if session_id is not None:
        data["session_id"] = session_id
    return RpcError(jsonrpc_code, code, "Session error", data)


async def session_cancel(params: dict[str, Any], client: IpcClient) -> dict[str, Any]:
    """Use durable state so cancellation does not depend on this IPC peer."""

    reject_unknown_params(params, {"run_id", "session_id", "reason"})
    run_id = params.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise invalid_params("invalid_run_id", "run_id must be non-empty")
    expected_session_id = params.get("session_id")
    if expected_session_id is not None and (
        not isinstance(expected_session_id, str) or not expected_session_id
    ):
        raise invalid_params("invalid_session_id", "session_id must be non-empty")
    reason = params.get("reason")
    if reason is not None and (not isinstance(reason, str) or not reason):
        raise invalid_params("invalid_reason", "reason must be non-empty")
    store = session_runtime_store(client)
    after_cursor = latest_event_cursor(store)
    try:
        result = store.cancel_run(
            run_id,
            expected_session_id=expected_session_id,
            reason=reason,
        )
    except UnauthorizedCancellation as exc:
        raise RpcError(
            -32009,
            "session_cancel_forbidden",
            "Session error",
            {"message": str(exc), "run_id": run_id},
        ) from exc
    notify_committed_events(client, store, after_cursor)
    return {
        "cancelled": result.status in {"cancelling", "cancelled", "already_cancelled"},
        "changed": result.changed,
        "run_id": run_id,
        "queue_item_id": result.queue_item_id,
        "session_id": result.session_id,
        "status": result.status,
        "terminal_status": result.terminal_status,
    }


def latest_event_cursor(store: RuntimeEventStore) -> int | None:
    events = store.list_events(Filter(limit=1, newest_first=True))
    return events[0].cursor if events else None


def notify_committed_events(
    client: IpcClient,
    store: RuntimeEventStore,
    after_cursor: int | None,
) -> None:
    connection = client.connection
    if connection is None:
        return
    events = store.list_events(Filter(after_cursor=after_cursor))
    if events:
        client.create_background_task(send_event_notifications(connection, events))


async def send_event_notifications(
    connection: JsonRpcConnection,
    events: list[Event],
) -> None:
    for event in events:
        await connection.notify("event", {"event": event_to_wire(event)})


def build_ipc_router(client: IpcClient) -> JsonRpcRouter:
    """Wire the fixed application methods onto the IPC router."""
    router = JsonRpcRouter(client)
    router.route("events.publish", events_publish)
    router.route("events.list", events_list)
    router.route("session.start", session_start)
    router.route("session.send", session_send)
    router.route("session.status", session_status)
    router.route("session.list", session_list)
    router.route("session.cancel", session_cancel)
    return router
