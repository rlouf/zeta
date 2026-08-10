"""Bridge connector ingress and egress to the durable event queue.

Ingress turns connector input (polled or pushed) into accepted events; egress
turns matching events into connector side effects via one-shot agents. Both
sides share idempotency-key rendering and event-payload validation. The worker
loop owns scheduling; this module owns the connector-to-event translation.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import sys
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, cast

from connectors import (
    EgressBinding,
    InboundRequest,
    InboundResponse,
    IngressBinding,
)
from jsonschema import Draft202012Validator

from zeta._version import __version__
from zeta.authoring.manifest import egress_bindings, ingress_bindings
from zeta.authoring.resources import (
    AgentProject,
)
from zeta.effects import DeliverySemantics, EffectDeliveryError
from zeta.events import DraftEvent, Event
from zeta.harness.routing import (
    AgentDefinition,
    AgentInvocation,
    EventPattern,
    ExecutableAgent,
)
from zeta.harness.templates import render_template
from zeta.wire.host import SourceCommand, SubprocessSource, WireEvent

if TYPE_CHECKING:
    from zeta.harness.worker import WorkerServices

logger = logging.getLogger(__name__)


def project_egress_executors(
    project: AgentProject,
    *,
    project_generation: str | None = None,
    execution_manifests: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[ExecutableAgent, ...]:
    executors: list[ExecutableAgent] = []
    for spec in project.specs:
        for index, binding in enumerate(egress_bindings(spec)):
            connector = project.connectors.connector_for_event(binding.event)
            if connector is None:
                continue
            handler = connector.egress.get(binding.event)
            if handler is None:
                continue
            semantics = connector.egress_semantics[binding.event]
            agent_id = f"egress:{spec.slug}:{index}:{connector.id}:{binding.event}"
            executors.append(
                ExecutableAgent(
                    AgentDefinition(
                        agent_id,
                        (EventPattern(binding.event),),
                        session="per-event",
                        project_generation=project_generation,
                        execution_manifest=(execution_manifests or {}).get(spec.slug),
                    ),
                    run=egress_runner(binding, handler, connector.id, semantics),
                )
            )
    return tuple(executors)


def egress_runner(
    binding: EgressBinding,
    handler,
    connector_id: str,
    semantics: DeliverySemantics,
):
    async def run(invocation: AgentInvocation) -> dict[str, Any]:
        event = invocation.triggering_event
        idempotency_key = egress_idempotency_key(binding, event, connector_id)
        await invocation.publish(
            effect_event_draft(
                "planned",
                event,
                connector_id=connector_id,
                effect_key=idempotency_key,
                semantics=semantics,
                invocation=invocation,
            )
        )
        await invocation.publish(
            effect_event_draft(
                "started",
                event,
                connector_id=connector_id,
                effect_key=idempotency_key,
                semantics=semantics,
                invocation=invocation,
            )
        )
        await invocation.publish(
            DraftEvent(
                "runtime.egress.started",
                f"egress:{connector_id}",
                {
                    "connector": connector_id,
                    "event_id": event.id,
                    "event_type": event.event_type,
                    "idempotency_key": idempotency_key,
                },
                idempotency_key=f"runtime.egress.started:{idempotency_key}",
            )
        )
        try:
            result = handler(event, binding, idempotency_key)
            if inspect.isawaitable(result):
                result = await result
            result_payload = dict(result or {})
        except Exception as exc:
            await invocation.publish(
                DraftEvent(
                    "runtime.egress.failed",
                    f"egress:{connector_id}",
                    {
                        "connector": connector_id,
                        "event_id": event.id,
                        "event_type": event.event_type,
                        "idempotency_key": idempotency_key,
                        "error": str(exc),
                    },
                    idempotency_key=f"runtime.egress.failed:{idempotency_key}",
                )
            )
            effect_status = "ambiguous" if semantics == "unsafe_to_retry" else "failed"
            await invocation.publish(
                effect_event_draft(
                    effect_status,
                    event,
                    connector_id=connector_id,
                    effect_key=idempotency_key,
                    semantics=semantics,
                    invocation=invocation,
                    error=str(exc),
                )
            )
            logger.exception("egress connector %r failed", connector_id)
            raise EffectDeliveryError(
                idempotency_key,
                semantics,
                f"{connector_id} delivery failed: {exc}",
            ) from exc
        await invocation.publish(
            DraftEvent(
                "runtime.egress.completed",
                f"egress:{connector_id}",
                {
                    "connector": connector_id,
                    "event_id": event.id,
                    "event_type": event.event_type,
                    "idempotency_key": idempotency_key,
                    "result": result_payload,
                },
                idempotency_key=f"runtime.egress.completed:{idempotency_key}",
            )
        )
        await invocation.publish(
            effect_event_draft(
                "completed",
                event,
                connector_id=connector_id,
                effect_key=idempotency_key,
                semantics=semantics,
                invocation=invocation,
                result=result_payload,
            )
        )
        return {
            "egress": {
                "connector": connector_id,
                "event_id": event.id,
                "result": result_payload,
            }
        }

    return run


def effect_event_draft(
    status: str,
    event: Event,
    *,
    connector_id: str,
    effect_key: str,
    semantics: DeliverySemantics,
    invocation: AgentInvocation,
    error: str | None = None,
    result: dict[str, Any] | None = None,
) -> DraftEvent:
    payload: dict[str, Any] = {
        "effect_key": effect_key,
        "operation": f"connector:{connector_id}:{event.event_type}",
        "semantics": semantics,
        "connector": connector_id,
        "event_id": event.id,
        "queue_item_id": invocation.queue_item_id,
        "attempt_id": invocation.attempt_id,
        "status": status,
    }
    if error is not None:
        payload["error"] = error
    if result is not None:
        payload["result"] = result
    return DraftEvent(
        f"runtime.effect.{status}",
        f"egress:{connector_id}",
        payload,
        idempotency_key=f"runtime.effect.{status}:{effect_key}",
        caused_by=event.id,
    )


async def run_ingress_once(
    runtime: WorkerServices,
    *,
    skip_connector_ids: frozenset[str] = frozenset(),
) -> int:
    project = runtime.project_snapshot.project
    inserted = 0
    for spec in project.specs:
        for binding in ingress_bindings(spec):
            connector = project.connectors.connector_for_event(binding.event)
            if connector is None or connector.id in skip_connector_ids:
                continue
            handler = connector.ingress.get(binding.event)
            if handler is None:
                continue
            drafts = handler(binding, None)
            if inspect.isawaitable(drafts):
                drafts = await drafts
            for draft in cast(Iterable[DraftEvent], drafts):
                if draft.event_type != binding.event:
                    raise RuntimeError(
                        f"ingress event {binding.event!r} produced {draft.event_type!r}"
                    )
                validate_event_payload(project.events, draft)
                outcome = runtime.events.accept(
                    DraftEvent(
                        draft.event_type,
                        draft.source,
                        draft.payload,
                        idempotency_key=ingress_idempotency_key(binding, draft),
                        caused_by=draft.caused_by,
                        session_id=draft.session_id,
                        run_id=draft.run_id,
                        turn_id=draft.turn_id,
                    )
                )
                if outcome.inserted:
                    inserted += 1
    return inserted


async def run_ingress_forever(
    runtime: WorkerServices,
    *,
    poll_interval_seconds: float = 1.0,
    stop_event: asyncio.Event | None = None,
    skip_connector_ids: frozenset[str] = frozenset(),
) -> None:
    while stop_event is None or not stop_event.is_set():
        try:
            await run_ingress_once(runtime, skip_connector_ids=skip_connector_ids)
        except Exception:
            logger.exception("ingress polling failed")
        await asyncio.sleep(poll_interval_seconds)


IPC_SOURCE_CONNECTOR_IDS = frozenset({"filesystem"})


def ipc_ingress_connector_ids(runtime: WorkerServices) -> frozenset[str]:
    """Return the connectors whose ingress runs as a wire-v0 subprocess.

    Connectors with a subprocess implementation always run over IPC;
    everything else stays on the in-process path until it grows a
    wire-v0 child.
    """
    if runtime.registry is None:
        return frozenset()
    return IPC_SOURCE_CONNECTOR_IDS & set(runtime.registry.connectors)


def fs_child_command(
    binding: IngressBinding,
    *,
    poll_interval_seconds: float,
) -> SourceCommand:
    config = {
        "watches": [dict(binding.filter)],
        "poll_interval": poll_interval_seconds,
    }
    return SourceCommand(
        (sys.executable, "-m", "zeta.wire.fs_inbox", json.dumps(config))
    )


async def run_ipc_ingress_forever(
    runtime: WorkerServices,
    *,
    connector_ids: frozenset[str],
    poll_interval_seconds: float = 1.0,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Supervise one wire-v0 child per IPC ingress binding."""
    project = runtime.project_snapshot.project
    tasks = []
    for spec in project.specs:
        for binding in ingress_bindings(spec):
            connector = project.connectors.connector_for_event(binding.event)
            if connector is None or connector.id not in connector_ids:
                continue
            tasks.append(
                asyncio.create_task(
                    run_ipc_binding_forever(
                        runtime,
                        binding,
                        connector_id=connector.id,
                        poll_interval_seconds=poll_interval_seconds,
                    )
                )
            )
    if not tasks:
        return
    try:
        if stop_event is None:
            await asyncio.gather(*tasks)
        else:
            await stop_event.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run_ipc_binding_forever(
    runtime: WorkerServices,
    binding: IngressBinding,
    *,
    connector_id: str,
    poll_interval_seconds: float,
) -> None:
    command = fs_child_command(binding, poll_interval_seconds=poll_interval_seconds)
    async with SubprocessSource(command, runtime_id=f"zeta-os/{__version__}") as source:
        async for wire_event in source.events():
            try:
                accepted = accept_ipc_event(runtime, binding, wire_event)
            except Exception:
                logger.exception(
                    "rejecting event %s from connector %r",
                    wire_event.id,
                    connector_id,
                )
                continue
            if accepted:
                await source.ack(wire_event.id)


def accept_ipc_event(
    runtime: WorkerServices,
    binding: IngressBinding,
    wire_event: WireEvent,
) -> bool:
    """Journal one subprocess event exactly like the in-process path."""
    if wire_event.type != binding.event:
        logger.warning(
            "ipc ingress event %r does not match binding %r",
            wire_event.type,
            binding.event,
        )
        return False
    project = runtime.project_snapshot.project
    draft = DraftEvent(
        wire_event.type,
        "filesystem",
        wire_event.payload,
        caused_by=wire_event.caused_by,
        session_id=wire_event.session_id,
    )
    validate_event_payload(project.events, draft)
    runtime.events.accept(
        DraftEvent(
            draft.event_type,
            draft.source,
            draft.payload,
            idempotency_key=ingress_idempotency_key(binding, draft),
            caused_by=draft.caused_by,
            session_id=draft.session_id,
        )
    )
    return True


async def handle_push_ingress_request(
    runtime: WorkerServices,
    connector_id: str,
    request: InboundRequest,
) -> InboundResponse:
    project = runtime.project_snapshot.project
    connector = project.connectors.resolve(connector_id)
    if connector is None:
        return InboundResponse(status_code=404, body=b"unknown connector")
    if connector.push_ingress is None:
        return InboundResponse(status_code=405, body=b"push ingress not supported")

    result = connector.push_ingress(request)
    if inspect.isawaitable(result):
        result = await result
    response, drafts = cast(
        tuple[InboundResponse, Iterable[DraftEvent]],
        result,
    )
    for draft in drafts:
        validate_event_payload(project.events, draft)
        runtime.events.accept(draft)
    return response


def validate_event_payload(events, draft: DraftEvent) -> None:
    schema = events.schema(draft.event_type)
    if schema is not None:
        Draft202012Validator(schema).validate(dict(draft.payload))


def ingress_idempotency_key(binding: IngressBinding, draft: DraftEvent) -> str:
    if binding.idempotency_key is None:
        raise RuntimeError(f"ingress event {binding.event!r} requires idempotency_key")
    return render_template(
        binding.idempotency_key, draft, what="idempotency-key template"
    )


def egress_idempotency_key(
    binding: EgressBinding,
    event: Event,
    connector_id: str,
) -> str:
    if binding.idempotency_key is None:
        return f"{connector_id}:{event.id}"
    return render_template(
        binding.idempotency_key, event, what="idempotency-key template"
    )
