"""The filesystem connector as a supervised wire-v0 subprocess."""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest
from wire_test_support import finished, frame_reader, read_envelope, send
from zeta.authoring.starter import scaffold_inbox_summarizer_project
from zeta.harness import worker
from zeta.harness.connector_bridge import (
    accept_ipc_event,
    ingress_waves,
    run_ipc_ingress_forever,
)
from zeta.journal.store import Filter
from zeta.wire.envelopes import envelope, mint_event_id
from zeta.wire.host import WireEvent


def scaffolded_runtime(tmp_path: Path, name: str) -> worker.WorkerServices:
    root = scaffold_inbox_summarizer_project(tmp_path / name)
    return worker.build_worker_services(
        project_root=root,
        state_dir=root / ".zeta",
    )


def test_scaffolded_project_waves_the_filesystem_connector(tmp_path: Path) -> None:
    runtime = scaffolded_runtime(tmp_path, "demo")
    try:
        waves = ingress_waves(runtime.project_snapshot.project)
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
            _ = runtime.project_snapshot
    finally:
        runtime.events.close()


async def test_fs_connector_executable_speaks_wire_v0(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "zeta_connectors.filesystem",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    reader = frame_reader(process)
    hello = await read_envelope(reader)
    assert hello["kind"] == "hello"
    assert hello["name"] == "filesystem"
    assert hello["event_types"] == [
        {"type": "file.created", "schema": "file.created@1"}
    ]
    await send(
        process,
        envelope(
            "hello_ack",
            "m-t-1",
            protocol_version=0,
            runtime="zeta-test/0",
            config={
                "bindings": [{"event": "file.created", "filter": {"dir": str(inbox)}}],
                "poll_interval": 0.05,
                "debounce": 0,
            },
        ),
    )
    await asyncio.sleep(0.3)
    target = inbox / "todo.txt"
    target.write_text("Buy milk.\n", encoding="utf-8")
    now = time.time()
    os.utime(target, (now, now))
    event = await read_envelope(reader, timeout=10)
    assert event["kind"] == "event"
    assert event["type"] == "file.created"
    assert event["payload"] == {
        "path": str(target),
        "name": "todo.txt",
        "dir": str(inbox),
    }
    await send(process, envelope("ack", "m-t-2", event_id=event["id"]))
    await send(process, envelope("shutdown", "m-t-3"))
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
    """End to end: discovered executable child → wire → journal row."""
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
    spec = next(spec for spec in runtime.project_snapshot.project.specs if spec.ingress)
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
        wire_event = WireEvent(
            id=mint_event_id("file.created", payload),
            type="file.created",
            schema="file.created@1",
            payload=payload,
            caused_by=None,
            session_id=None,
            ts="2026-08-10T12:00:00Z",
        )
        assert accept_ipc_event(runtime, binding, wire_event, connector_id="filesystem")
        assert accept_ipc_event(runtime, binding, wire_event, connector_id="filesystem")
        assert len(accepted_file_events(runtime)) == 1
    finally:
        runtime.events.close()


def test_ipc_events_of_the_wrong_type_are_refused_without_crashing(
    tmp_path: Path,
) -> None:
    runtime = scaffolded_runtime(tmp_path, "wrong-type-project")
    try:
        binding = demo_binding(runtime)
        wire_event = WireEvent(
            id=mint_event_id("file.deleted", {}),
            type="file.deleted",
            schema="file.deleted@1",
            payload={},
            caused_by=None,
            session_id=None,
            ts="2026-08-10T12:00:00Z",
        )
        assert not accept_ipc_event(
            runtime, binding, wire_event, connector_id="filesystem"
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
