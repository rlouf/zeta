"""The filesystem connector as a supervised IPC subprocess."""

import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from ipc_test_support import finished, frame_reader, read_message, send
from zeta.authoring.resources import load_connector_registry
from zeta.authoring.starter import scaffold_inbox_summarizer_project
from zeta.harness import worker
from zeta.harness.connector_bridge import (
    accept_ipc_event,
    ingress_waves,
    run_ipc_ingress_forever,
)
from zeta.ipc.messages import request, success_response
from zeta.ipc.supervisor import PublishRequest
from zeta.journal.store import Filter


def scaffolded_runtime(tmp_path: Path, name: str) -> worker.WorkerServices:
    root = scaffold_inbox_summarizer_project(tmp_path / name)
    return worker.build_worker_services(
        project_root=root,
        state_dir=root / ".zeta",
    )


def test_scaffolded_project_waves_the_filesystem_connector(tmp_path: Path) -> None:
    runtime = scaffolded_runtime(tmp_path, "demo")
    try:
        waves = ingress_waves(runtime.project_revision.project)
        assert len(waves) == 1
        connector, wave = waves[0]
        assert connector.id == "filesystem"
        assert set(wave) == {"file.created"}
    finally:
        runtime.events.close()


def test_process_allowlist_excluding_a_bound_connector_fails_loudly(
    tmp_path: Path,
) -> None:
    """An allowlist that hides a connector the project binds is an error."""
    from zeta.authoring.manifest import ManifestError

    root = scaffold_inbox_summarizer_project(tmp_path / "demo")
    runtime = worker.build_worker_services(
        project_root=root,
        state_dir=root / ".zeta",
        connector_names=("slack",),
    )
    try:
        with pytest.raises(ManifestError, match="unknown ingress event"):
            _ = runtime.project_revision
    finally:
        runtime.events.close()


async def test_fs_connector_entry_point_speaks_ipc_v0(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    manifest = load_connector_registry(connector_names=("filesystem",)).resolve(
        "filesystem"
    )
    assert manifest is not None
    process = await asyncio.create_subprocess_exec(
        *manifest.command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    reader = frame_reader(process)
    initialize = await read_message(reader)
    assert initialize["method"] == "initialize"
    params = initialize["params"]
    assert params["peer"]["name"] == "filesystem"
    assert params["roles"] == ["source"]
    assert params["event_types"] == [
        {"type": "file.created", "schema": "file.created@1"}
    ]
    await send(
        process,
        success_response(
            initialize["id"],
            {
                "protocol_version": 0,
                "runtime": {"name": "zeta-test", "version": "0"},
                "roles": ["source"],
                "config": {
                    "bindings": [
                        {"event": "file.created", "filter": {"dir": str(inbox)}}
                    ],
                    "poll_interval": 0.05,
                    "debounce": 0,
                },
                "heartbeat_seconds": 10,
                "max_in_flight": 64,
            },
        ),
    )
    await asyncio.sleep(0.3)
    target = inbox / "todo.txt"
    target.write_text("Buy milk.\n", encoding="utf-8")
    now = time.time()
    os.utime(target, (now, now))
    publish = await read_message(reader, timeout=10)
    assert publish["method"] == "events.publish"
    assert publish["params"]["type"] == "file.created"
    assert publish["params"]["payload"] == {
        "path": str(target),
        "name": "todo.txt",
        "dir": str(inbox),
    }
    durable_event = {
        "id": "evt_1",
        "type": "file.created",
        "source": "filesystem",
        "payload": publish["params"]["payload"],
        "idempotency_key": None,
        "caused_by": None,
        "session_id": None,
        "run_id": None,
        "turn_id": None,
        "timestamp_ms": 1,
        "cursor": 1,
    }
    await send(
        process,
        success_response(publish["id"], {"inserted": True, "event": durable_event}),
    )
    await send(process, request("runtime-stop", "shutdown", {}))
    assert await read_message(reader) == success_response("runtime-stop", {})
    assert await finished(process) == 0


def accepted_file_events(runtime: worker.WorkerServices) -> list[dict]:
    return [
        {
            "event_type": event.event_type,
            "source": event.source,
            "payload": dict(event.payload),
            "idempotency_key": event.idempotency_key,
            "caused_by": event.caused_by,
            "session_id": event.session_id,
        }
        for event in runtime.events.list_events(Filter(event_type="file.created"))
    ]


async def test_ipc_ingress_reaches_the_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: installed entry point → isolated child → journal row."""
    monkeypatch.setenv("FILESYSTEM_DEBOUNCE_SECONDS", "0")
    runtime = scaffolded_runtime(tmp_path, "ipc-project")
    ingress = asyncio.create_task(
        run_ipc_ingress_forever(runtime, poll_interval_seconds=0.05)
    )
    try:
        await asyncio.sleep(0.5)
        inbox = runtime.project_root / "inbox"
        target = inbox / "todo.txt"
        target.write_text("Buy milk.\n", encoding="utf-8")
        deadline = time.time() + 15
        while not accepted_file_events(runtime) and time.time() < deadline:
            now = time.time()
            os.utime(target, (now, now))
            await asyncio.sleep(0.5)
        events = accepted_file_events(runtime)
        assert events, "the subprocess path journaled no event"
        event = events[0]
        assert event["event_type"] == "file.created"
        assert event["source"] == "filesystem"
        assert event["payload"]["name"] == "todo.txt"
        assert event["payload"]["path"] == str(target)
        assert event["idempotency_key"] == f"file:{target}"
    finally:
        ingress.cancel()
        await asyncio.gather(ingress, return_exceptions=True)
        runtime.events.close()


def demo_binding(runtime: worker.WorkerServices):
    spec = next(spec for spec in runtime.project_revision.project.specs if spec.ingress)
    return spec.ingress[0]


def test_ipc_event_redelivery_is_deduplicated_by_idempotency_key(
    tmp_path: Path,
) -> None:
    """A restarted child redelivers events; the journal keeps one row."""
    runtime = scaffolded_runtime(tmp_path, "dedupe-project")
    try:
        binding = demo_binding(runtime)
        inbox = str(runtime.project_root / "inbox")
        payload = {
            "path": f"{inbox}/todo.txt",
            "name": "todo.txt",
            "dir": inbox,
        }
        publication = PublishRequest(
            request_id=1,
            type="file.created",
            payload=payload,
            idempotency_key=None,
            caused_by=None,
            session_id=None,
            run_id=None,
            turn_id=None,
        )
        first = accept_ipc_event(
            runtime, binding, publication, connector_id="filesystem"
        )
        duplicate = accept_ipc_event(
            runtime, binding, publication, connector_id="filesystem"
        )
        assert first is not None and first.inserted
        assert duplicate is not None and not duplicate.inserted
        assert duplicate.event.id == first.event.id
        assert len(accepted_file_events(runtime)) == 1
    finally:
        runtime.events.close()


def test_ipc_events_of_the_wrong_type_are_refused_without_crashing(
    tmp_path: Path,
) -> None:
    runtime = scaffolded_runtime(tmp_path, "wrong-type-project")
    try:
        binding = demo_binding(runtime)
        publication = PublishRequest(
            request_id=1,
            type="file.deleted",
            payload={},
            idempotency_key=None,
            caused_by=None,
            session_id=None,
            run_id=None,
            turn_id=None,
        )
        assert (
            accept_ipc_event(runtime, binding, publication, connector_id="filesystem")
            is None
        )
        assert accepted_file_events(runtime) == []
    finally:
        runtime.events.close()


def test_wave_partition_reuses_one_child_per_event_type(tmp_path: Path) -> None:
    """Two same-type bindings split into waves; distinct types share one."""
    from connectors import ConnectorManifest, EventConnectorRegistry, IngressBinding

    manifest = ConnectorManifest(
        id="multi",
        command=("multi",),
        events={"a.created": None, "b.created": None},
    )
    registry = EventConnectorRegistry()
    registry.register(manifest)

    class Spec:
        ingress = (
            IngressBinding("a.created", filter={"dir": "/one"}, idempotency_key="k"),
            IngressBinding("a.created", filter={"dir": "/two"}, idempotency_key="k"),
            IngressBinding("b.created", idempotency_key="k"),
        )

    class Project:
        connectors = registry
        specs = (Spec(),)

    waves = ingress_waves(Project())
    assert [sorted(wave) for _connector, wave in waves] == [
        ["a.created", "b.created"],
        ["a.created"],
    ]
    assert json.loads(json.dumps(waves[0][1]["a.created"].filter)) == {"dir": "/one"}
