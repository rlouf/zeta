"""Bridge isolated connector peers to the durable event queue."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from connectors import ConnectorManifest, EgressBinding, IngressBinding
from jsonschema import Draft202012Validator

from zeta._version import __version__
from zeta.authoring.manifest import egress_bindings, ingress_bindings
from zeta.effects import DeliverySemantics, EffectDeliveryError
from zeta.events import DraftEvent, Event
from zeta.harness.routing import (
    AgentDefinition,
    AgentInvocation,
    EventPattern,
    ExecutableAgent,
)
from zeta.harness.templates import render_template
from zeta.ipc.supervisor import PeerCommand, PublishRequest, SubprocessPeer
from zeta.journal.types import AppendOutcome

if TYPE_CHECKING:
    from zeta.harness.worker import WorkerServices

logger = logging.getLogger(__name__)

RUNTIME_NAME = "zeta"
RUNTIME_VERSION = __version__


class ConnectorCalls:
    """Lazily spawn one provider peer per connector.

    A provider peer gets no binding in its config, so a connector
    that also sources events does not double-ingest; its ingress runs
    in separate per-binding children.
    """

    def __init__(self) -> None:
        self._peers: dict[str, SubprocessPeer] = {}
        self._drains: dict[str, asyncio.Task[None]] = {}

    async def call(
        self,
        connector: ConnectorManifest,
        *,
        operation: str,
        payload: dict[str, Any],
        options: dict[str, Any],
        effect_key: str,
    ) -> dict[str, Any]:
        peer = await self._peer_for(connector)
        return await peer.call(
            operation,
            {"payload": payload, "options": options},
            effect_key,
        )

    async def _peer_for(self, connector: ConnectorManifest) -> SubprocessPeer:
        peer = self._peers.get(connector.id)
        if peer is not None:
            return peer
        peer = SubprocessPeer(
            PeerCommand(connector.command),
            runtime_name=RUNTIME_NAME,
            runtime_version=RUNTIME_VERSION,
            config={},
        )
        self._peers[connector.id] = peer
        self._drains[connector.id] = asyncio.create_task(
            _drain_provider_peer(connector.id, peer)
        )
        await _wait_for_initialization(peer)
        return peer

    async def aclose(self) -> None:
        for task in self._drains.values():
            task.cancel()
        await asyncio.gather(*self._drains.values(), return_exceptions=True)
        self._drains.clear()
        peers, self._peers = self._peers, {}
        await asyncio.gather(
            *(peer.aclose() for peer in peers.values()),
            return_exceptions=True,
        )


async def _drain_provider_peer(connector_id: str, peer: SubprocessPeer) -> None:
    async for publication in peer.publications():
        logger.warning(
            "provider peer for %r published %s without a binding",
            connector_id,
            publication.type,
        )
        await peer.fail_publish(
            publication,
            "unbound_event_type",
            "the provider peer has no ingress binding",
        )


async def _wait_for_initialization(
    peer: SubprocessPeer, *, timeout: float = 15.0
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while peer.initialization is None:
        if asyncio.get_running_loop().time() > deadline:
            return  # let the call itself fail with a retryable error
        await asyncio.sleep(0.05)


def project_egress_executors(
    project,
    *,
    project_generation: str | None = None,
    execution_manifests: Mapping[str, Mapping[str, Any]] | None = None,
    connector_calls: ConnectorCalls | None = None,
) -> tuple[ExecutableAgent, ...]:
    calls = connector_calls or ConnectorCalls()
    executors: list[ExecutableAgent] = []
    for spec in project.specs:
        for index, binding in enumerate(egress_bindings(spec)):
            connector = project.connectors.connector_for_event(binding.event)
            if connector is None:
                continue
            operation = connector.operations.get(binding.event)
            if operation is None:
                continue
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
                    run=egress_runner(
                        binding,
                        connector,
                        operation.semantics,
                        calls,
                    ),
                )
            )
    return tuple(executors)


def egress_runner(
    binding: EgressBinding,
    connector: ConnectorManifest,
    semantics: DeliverySemantics,
    calls: ConnectorCalls,
):
    connector_id = connector.id

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
            result_payload = dict(
                await calls.call(
                    connector,
                    operation=binding.event,
                    payload=dict(event.payload),
                    options=dict(binding.options),
                    effect_key=idempotency_key,
                )
            )
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


def ingress_waves(project) -> list[tuple[ConnectorManifest, dict[str, IngressBinding]]]:
    """Partition ingress bindings into one child's worth each.

    A wave holds at most one binding per event type, so a child can be
    handed several event types (Telegram's messages and reactions share
    one upstream cursor) while several bindings of the same type (two
    watched directories) each get their own child.
    """
    per_connector: dict[str, list[IngressBinding]] = {}
    manifests: dict[str, ConnectorManifest] = {}
    for spec in project.specs:
        for binding in ingress_bindings(spec):
            connector = project.connectors.connector_for_event(binding.event)
            if connector is None or binding.event not in connector.ingress_event_types:
                continue
            manifests[connector.id] = connector
            per_connector.setdefault(connector.id, []).append(binding)
    waves: list[tuple[ConnectorManifest, dict[str, IngressBinding]]] = []
    for connector_id, bindings in per_connector.items():
        pending = list(bindings)
        while pending:
            wave: dict[str, IngressBinding] = {}
            leftover: list[IngressBinding] = []
            for binding in pending:
                if binding.event in wave:
                    leftover.append(binding)
                else:
                    wave[binding.event] = binding
            waves.append((manifests[connector_id], wave))
            pending = leftover
    return waves


def ingress_child_config(
    wave: dict[str, IngressBinding],
    *,
    poll_interval_seconds: float,
) -> dict[str, Any]:
    return {
        "bindings": [
            {"event": binding.event, "filter": dict(binding.filter)}
            for binding in wave.values()
        ],
        "poll_interval": poll_interval_seconds,
    }


async def run_ipc_ingress_forever(
    runtime: WorkerServices,
    *,
    poll_interval_seconds: float = 1.0,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Supervise one IPC peer per ingress wave."""
    tasks = [
        asyncio.create_task(
            run_ipc_wave_forever(
                runtime,
                wave,
                connector=connector,
                poll_interval_seconds=poll_interval_seconds,
            )
        )
        for connector, wave in ingress_waves(runtime.project_snapshot.project)
    ]
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


async def run_ipc_wave_forever(
    runtime: WorkerServices,
    wave: dict[str, IngressBinding],
    *,
    connector: ConnectorManifest,
    poll_interval_seconds: float,
) -> None:
    async with SubprocessPeer(
        PeerCommand(connector.command),
        runtime_name=RUNTIME_NAME,
        runtime_version=RUNTIME_VERSION,
        config=ingress_child_config(wave, poll_interval_seconds=poll_interval_seconds),
    ) as peer:
        async for publication in peer.publications():
            binding = wave.get(publication.type)
            if binding is None:
                logger.warning(
                    "connector %r emitted unbound event type %r",
                    connector.id,
                    publication.type,
                )
                await peer.fail_publish(
                    publication,
                    "unbound_event_type",
                    f"event type {publication.type!r} has no binding",
                )
                continue
            try:
                outcome = accept_ipc_event(
                    runtime, binding, publication, connector_id=connector.id
                )
            except Exception as exc:
                logger.exception(
                    "rejecting %s publication from connector %r",
                    publication.type,
                    connector.id,
                )
                await peer.fail_publish(
                    publication,
                    "event_rejected",
                    str(exc),
                )
                continue
            if outcome is None:
                await peer.fail_publish(
                    publication,
                    "event_type_mismatch",
                    "the publication does not match its ingress binding",
                )
                continue
            await peer.complete_publish(
                publication,
                {
                    "inserted": outcome.inserted,
                    "event": ipc_event_record(outcome.event),
                },
            )


def accept_ipc_event(
    runtime: WorkerServices,
    binding: IngressBinding,
    publication: PublishRequest,
    *,
    connector_id: str,
) -> AppendOutcome | None:
    """Journal one connector event under the binding's idempotency key."""
    if publication.type != binding.event:
        logger.warning(
            "ipc ingress event %r does not match binding %r",
            publication.type,
            binding.event,
        )
        return None
    project = runtime.project_snapshot.project
    draft = DraftEvent(
        publication.type,
        connector_id,
        publication.payload,
        caused_by=publication.caused_by,
        session_id=publication.session_id,
        run_id=publication.run_id,
        turn_id=publication.turn_id,
    )
    validate_event_payload(project.events, draft)
    return runtime.events.accept(
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


def ipc_event_record(event: Event) -> dict[str, Any]:
    """Return the durable event shape used by IPC publish results."""
    return {
        "id": event.id,
        "type": event.event_type,
        "source": event.source,
        "payload": dict(event.payload),
        "idempotency_key": event.idempotency_key,
        "caused_by": event.caused_by,
        "session_id": event.session_id,
        "run_id": event.run_id,
        "turn_id": event.turn_id,
        "timestamp_ms": event.timestamp_ms,
        "cursor": event.cursor,
    }


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
