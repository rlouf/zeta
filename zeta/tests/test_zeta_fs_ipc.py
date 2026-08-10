"""The filesystem connector as a supervised wire-v0 subprocess."""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest
from wire_test_support import complete_handshake, finished, frame_reader, send
from zeta.authoring.starter import scaffold_inbox_summarizer_project
from zeta.harness import worker
from zeta.harness.connector_bridge import (
    accept_ipc_event,
    ipc_ingress_connector_ids,
    run_ingress_once,
    run_ipc_ingress_forever,
)
from zeta.journal.store import Filter
from zeta.wire.envelopes import envelope, mint_event_id
from zeta.wire.framing import FrameViolation
from zeta.wire.host import WireEvent


def scaffolded_runtime(tmp_path: Path, name: str) -> worker.WorkerServices:
    root = scaffold_inbox_summarizer_project(tmp_path / name)
    return worker.build_worker_services(
        project_root=root,
        state_dir=root / ".zeta",
    )


def test_scaffolded_project_selects_ipc_for_the_filesystem_connector(
    tmp_path: Path,
) -> None:
    runtime = scaffolded_runtime(tmp_path, "demo")
    try:
        assert ipc_ingress_connector_ids(runtime) == frozenset({"filesystem"})
    finally:
        runtime.events.close()


def test_process_allowlist_disables_subprocess_ingress(tmp_path: Path) -> None:
    root = scaffold_inbox_summarizer_project(tmp_path / "demo")
    runtime = worker.build_worker_services(
        project_root=root,
        state_dir=root / ".zeta",
        connector_names=("slack",),
    )
    try:
        assert ipc_ingress_connector_ids(runtime) == frozenset()
    finally:
        runtime.events.close()


async def test_fs_inbox_child_speaks_wire_v0(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    config = {
        "watches": [{"dir": str(inbox)}],
        "poll_interval": 0.05,
        "debounce": 0,
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "zeta.wire.fs_inbox",
        json.dumps(config),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    reader = frame_reader(process)
    hello = await complete_handshake(process, reader)
    assert hello["name"] == "fs-inbox"
    assert hello["event_types"] == [
        {"type": "file.created", "schema": "file.created@1"}
    ]
    await asyncio.sleep(0.3)
    target = inbox / "todo.txt"
    target.write_text("Buy milk.\n", encoding="utf-8")
    now = time.time()
    os.utime(target, (now, now))
    event = await asyncio.wait_for(reader.read_frame(), timeout=10)
    assert event is not None and not isinstance(event, FrameViolation)
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


def normalized(events: list[dict], root: Path) -> list[dict]:
    text = json.dumps(events, sort_keys=True)
    return json.loads(text.replace(str(root), "<root>"))


async def collect_inproc_golden(tmp_path: Path) -> list[dict]:
    runtime = scaffolded_runtime(tmp_path, "inproc-project")
    try:
        await run_ingress_once(runtime)
        inbox = runtime.project_root / "inbox"
        target = inbox / "todo.txt"
        target.write_text("Buy milk.\n", encoding="utf-8")
        deadline = time.time() + 10
        while not accepted_file_events(runtime) and time.time() < deadline:
            now = time.time()
            os.utime(target, (now, now))
            await run_ingress_once(runtime)
            await asyncio.sleep(0.05)
        return normalized(accepted_file_events(runtime), runtime.project_root)
    finally:
        runtime.events.close()


async def collect_ipc_golden(tmp_path: Path) -> list[dict]:
    runtime = scaffolded_runtime(tmp_path, "ipc-project")
    ingress = asyncio.create_task(
        run_ipc_ingress_forever(
            runtime,
            connector_ids=frozenset({"filesystem"}),
            poll_interval_seconds=0.05,
        )
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
        return normalized(accepted_file_events(runtime), runtime.project_root)
    finally:
        ingress.cancel()
        await asyncio.gather(ingress, return_exceptions=True)
        runtime.events.close()


async def test_ipc_ingress_reaches_the_journal_identically_to_inproc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Golden comparison: same file, same journaled event on both transports."""
    monkeypatch.setenv("FILESYSTEM_DEBOUNCE_SECONDS", "0")
    inproc_events = await collect_inproc_golden(tmp_path)
    ipc_events = await collect_ipc_golden(tmp_path)
    assert inproc_events, "the in-process path journaled no event"
    assert ipc_events, "the subprocess path journaled no event"
    assert ipc_events == inproc_events


def test_ipc_event_redelivery_is_deduplicated_by_idempotency_key(
    tmp_path: Path,
) -> None:
    """A restarted child redelivers events; the journal keeps one row."""
    runtime = scaffolded_runtime(tmp_path, "dedupe-project")
    try:
        spec = next(
            spec for spec in runtime.project_snapshot.project.specs if spec.ingress
        )
        binding = spec.ingress[0]
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
        assert accept_ipc_event(runtime, binding, wire_event)
        assert accept_ipc_event(runtime, binding, wire_event)
        assert len(accepted_file_events(runtime)) == 1
    finally:
        runtime.events.close()


def test_ipc_events_of_the_wrong_type_are_refused_without_crashing(
    tmp_path: Path,
) -> None:
    runtime = scaffolded_runtime(tmp_path, "wrong-type-project")
    try:
        spec = next(
            spec for spec in runtime.project_snapshot.project.specs if spec.ingress
        )
        binding = spec.ingress[0]
        wire_event = WireEvent(
            id=mint_event_id("file.deleted", {}),
            type="file.deleted",
            schema="file.deleted@1",
            payload={},
            caused_by=None,
            session_id=None,
            ts="2026-08-10T12:00:00Z",
        )
        assert not accept_ipc_event(runtime, binding, wire_event)
        assert accepted_file_events(runtime) == []
    finally:
        runtime.events.close()
