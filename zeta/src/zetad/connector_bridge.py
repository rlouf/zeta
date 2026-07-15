"""Bridge connector ingress and egress to the durable event queue.

Ingress turns connector input (polled or pushed) into accepted events; egress
turns matching events into connector side effects via one-shot agents. Both
sides share idempotency-key rendering and event-payload validation. The worker
loop owns scheduling; this module owns the connector-to-event translation.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, cast

from jsonschema import Draft202012Validator
from zeta.agents.manifest import egress_bindings, ingress_bindings
from zeta.agents.resources import (
    AgentProject,
    load_agent_project,
    validate_agent_project,
)
from zeta.events import DraftEvent, Event

from connectors import (
    EgressBinding,
    InboundRequest,
    InboundResponse,
    IngressBinding,
)
from zetad.agents import (
    AgentDefinition,
    AgentInvocation,
    EventPattern,
    ExecutableAgent,
)

if TYPE_CHECKING:
    from zetad.worker import WorkerServices

logger = logging.getLogger(__name__)


def project_egress_executors(
    project: AgentProject,
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
            agent_id = f"egress:{spec.slug}:{index}:{connector.id}:{binding.event}"
            executors.append(
                ExecutableAgent(
                    AgentDefinition(
                        agent_id,
                        (EventPattern(binding.event),),
                        dispatch_mode="one_shot",
                    ),
                    run=egress_runner(binding, handler, connector.id),
                )
            )
    return tuple(executors)


def egress_runner(binding: EgressBinding, handler, connector_id: str):
    async def run(invocation: AgentInvocation) -> dict[str, Any]:
        event = invocation.triggering_event
        idempotency_key = egress_idempotency_key(binding, event, connector_id)
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
            logger.exception("egress connector %r failed", connector_id)
            return {
                "egress": {
                    "connector": connector_id,
                    "event_id": event.id,
                    "failed": True,
                    "error": str(exc),
                }
            }
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
        return {
            "egress": {
                "connector": connector_id,
                "event_id": event.id,
                "result": result_payload,
            }
        }

    return run


async def run_ingress_once(runtime: WorkerServices) -> int:
    project = load_agent_project(
        runtime.project_root / "agents",
        registry=runtime.registry,
    )
    validate_agent_project(project)
    inserted = 0
    for spec in project.specs:
        for binding in ingress_bindings(spec):
            connector = project.connectors.connector_for_event(binding.event)
            if connector is None:
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
) -> None:
    while stop_event is None or not stop_event.is_set():
        try:
            await run_ingress_once(runtime)
        except Exception:
            logger.exception("ingress polling failed")
        await asyncio.sleep(poll_interval_seconds)


async def handle_push_ingress_request(
    runtime: WorkerServices,
    connector_id: str,
    request: InboundRequest,
) -> InboundResponse:
    project = load_agent_project(
        runtime.project_root / "agents",
        registry=runtime.registry,
    )
    validate_agent_project(project)
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
    return render_template(binding.idempotency_key, draft)


def egress_idempotency_key(
    binding: EgressBinding,
    event: Event,
    connector_id: str,
) -> str:
    if binding.idempotency_key is None:
        return f"{connector_id}:{event.id}"
    return render_template(binding.idempotency_key, event)


def render_template(template: str, event: DraftEvent | Event) -> str:
    try:
        return template.format(event=event, **dict(event.payload))
    except (KeyError, IndexError) as exc:
        raise RuntimeError(
            f"idempotency-key template {template!r} references a missing field: {exc}"
        ) from exc
