"""Child-side IPC client behavior over real subprocess streams."""

from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest
from ipc_test_support import (
    child_stderr,
    complete_initialize,
    finished,
    frame_reader,
    read_message,
    sdk_child_peer,
    send,
    spawn,
    write_child,
)
from zeta.ipc import client as ipc_client
from zeta.ipc.messages import request, success_response

PAYLOAD = {"dir": "/p/inbox", "name": "a.txt", "path": "/p/inbox/a.txt"}


def test_module_runner_invokes_target_without_runpy_warning() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "zeta.ipc.client",
            "test.entry_points",
            "print",
            "builtins:print",
            "--describe",
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert result.stdout == "['--describe']\n"
    assert result.stderr == ""


def test_entry_point_runner_loads_exact_target_and_forwards_plugin_argv(
    monkeypatch,
) -> None:
    observed = {}

    class StubEntryPoint:
        def __init__(self, *, name: str, value: str, group: str) -> None:
            observed["metadata"] = (group, name, value)

        def load(self):
            observed["loaded"] = True

            def target(argv: list[str]) -> None:
                observed["argv"] = argv

            return target

    monkeypatch.setattr(ipc_client, "EntryPoint", StubEntryPoint)

    ipc_client._run_entry_point(
        [
            "zeta.event_connectors",
            "filesystem",
            "zeta_connectors.filesystem:main",
            "--describe",
        ]
    )

    assert observed == {
        "metadata": (
            "zeta.event_connectors",
            "filesystem",
            "zeta_connectors.filesystem:main",
        ),
        "loaded": True,
        "argv": ["--describe"],
    }


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["zeta.event_connectors"],
        ["zeta.event_connectors", "filesystem"],
        ["", "filesystem", "zeta_connectors.filesystem:main"],
        ["zeta.event_connectors", "", "zeta_connectors.filesystem:main"],
        ["zeta.event_connectors", "filesystem", ""],
    ],
)
def test_entry_point_runner_rejects_malformed_arguments(argv) -> None:
    with pytest.raises(SystemExit, match="usage: python -m zeta.ipc.client"):
        ipc_client._run_entry_point(argv)


def test_entry_point_runner_rejects_noncallable_target(monkeypatch) -> None:
    class StubEntryPoint:
        def __init__(self, *, name: str, value: str, group: str) -> None:
            pass

        def load(self):
            return object()

    monkeypatch.setattr(ipc_client, "EntryPoint", StubEntryPoint)

    with pytest.raises(SystemExit, match="entry point filesystem is not callable"):
        ipc_client._run_entry_point(
            [
                "zeta.event_connectors",
                "filesystem",
                "zeta_connectors.filesystem:main",
            ]
        )


def one_event_body(payload: dict, *, on_ack: str = "None") -> str:
    return f"""
    yield SourceEvent("file.created", {payload!r}, on_ack={on_ack})
    await asyncio.sleep(60)
    """


def durable_publish_result(params: dict, *, inserted: bool = True) -> dict:
    return {
        "inserted": inserted,
        "event": {
            "id": "evt_1",
            "type": params["type"],
            "source": "fs-inbox",
            "payload": params["payload"],
            "idempotency_key": params.get("idempotency_key"),
            "caused_by": params.get("caused_by"),
            "session_id": params.get("session_id"),
            "run_id": params.get("run_id"),
            "turn_id": params.get("turn_id"),
            "timestamp_ms": 1,
            "cursor": 1,
        },
    }


async def stop_peer(process, reader) -> None:
    await send(process, request("runtime-stop", "shutdown", {"reason": "test done"}))
    response = await read_message(reader)
    assert response == success_response("runtime-stop", {})
    assert await finished(process) == 0


async def test_run_peer_initializes_and_publishes_source_events(tmp_path) -> None:
    script = write_child(tmp_path, sdk_child_peer(events_body=one_event_body(PAYLOAD)))
    process = await spawn(script)
    reader = frame_reader(process)
    initialize = await complete_initialize(process, reader)
    assert initialize["params"]["roles"] == ["source"]
    assert initialize["params"]["event_types"] == [
        {"type": "file.created", "schema": "file.created@1"}
    ]

    publish = await read_message(reader)
    assert publish["method"] == "events.publish"
    assert publish["params"] == {
        "type": "file.created",
        "payload": PAYLOAD,
        "idempotency_key": None,
        "caused_by": None,
        "session_id": None,
        "run_id": None,
        "turn_id": None,
    }
    await send(
        process,
        success_response(publish["id"], durable_publish_result(publish["params"])),
    )
    await stop_peer(process, reader)


async def test_run_peer_answers_ping_and_shutdown_requests(tmp_path) -> None:
    script = write_child(
        tmp_path,
        sdk_child_peer(
            events_body="""
            await asyncio.sleep(60)
            yield SourceEvent("file.created", {})
            """
        ),
    )
    process = await spawn(script)
    reader = frame_reader(process)
    await complete_initialize(process, reader)
    await send(process, request(7, "ping"))
    assert await read_message(reader) == success_response(7, {})
    await stop_peer(process, reader)


async def test_run_peer_keeps_stdout_for_protocol_messages(tmp_path) -> None:
    script = write_child(
        tmp_path,
        sdk_child_peer(
            events_body=f"""
            import logging
            print("this print must not corrupt the protocol")
            logging.getLogger("peer").warning("neither must logging")
            yield SourceEvent("file.created", {PAYLOAD!r})
            await asyncio.sleep(60)
            """,
        ),
    )
    process = await spawn(script)
    reader = frame_reader(process)
    await complete_initialize(process, reader)
    publish = await read_message(reader)
    assert publish["method"] == "events.publish"
    await stop_peer(process, reader)
    stderr = await child_stderr(process)
    assert "this print must not corrupt the protocol" in stderr
    assert "neither must logging" in stderr


async def test_publish_response_is_the_upstream_ack_boundary(tmp_path) -> None:
    script = write_child(
        tmp_path,
        sdk_child_peer(
            events_body=one_event_body(PAYLOAD, on_ack='lambda: print("cursor:1")')
        ),
    )
    process = await spawn(script)
    reader = frame_reader(process)
    await complete_initialize(process, reader)
    publish = await read_message(reader)
    assert process.stderr is not None
    try:
        before = await asyncio.wait_for(process.stderr.read(1), timeout=0.2)
    except TimeoutError:
        before = b""
    assert before == b""
    await send(
        process,
        success_response(publish["id"], durable_publish_result(publish["params"])),
    )
    await stop_peer(process, reader)
    assert "cursor:1" in await child_stderr(process)


async def test_malformed_publish_success_does_not_ack_the_upstream(tmp_path) -> None:
    script = write_child(
        tmp_path,
        sdk_child_peer(
            events_body=one_event_body(PAYLOAD, on_ack='lambda: print("cursor:1")')
        ),
    )
    process = await spawn(script)
    reader = frame_reader(process)
    await complete_initialize(process, reader)
    publish = await read_message(reader)
    await send(process, success_response(publish["id"], {}))
    await stop_peer(process, reader)
    assert "cursor:1" not in await child_stderr(process)


async def test_run_peer_honors_the_negotiated_in_flight_limit(tmp_path) -> None:
    payloads = [{"path": f"/p/f{index}.txt"} for index in range(3)]
    script = write_child(
        tmp_path,
        sdk_child_peer(
            events_body=f"""
            for payload in {payloads!r}:
                yield SourceEvent("file.created", payload)
            await asyncio.sleep(60)
            """,
            max_in_flight=1,
        ),
    )
    process = await spawn(script)
    reader = frame_reader(process)
    await complete_initialize(process, reader)
    first = await read_message(reader)
    assert first["params"]["payload"] == payloads[0]
    try:
        await asyncio.wait_for(reader.read_frame(), timeout=0.3)
        raise AssertionError("a second publish arrived before the first response")
    except TimeoutError:
        pass
    await send(
        process,
        success_response(first["id"], durable_publish_result(first["params"])),
    )
    second = await read_message(reader)
    assert second["params"]["payload"] == payloads[1]
    await stop_peer(process, reader)


async def test_run_peer_rejects_oversized_inline_payloads(tmp_path) -> None:
    script = write_child(
        tmp_path,
        sdk_child_peer(
            events_body='yield SourceEvent("file.created", {"data": "x" * 70000})'
        ),
    )
    process = await spawn(script)
    reader = frame_reader(process)
    await complete_initialize(process, reader)
    assert await asyncio.wait_for(reader.read_frame(), timeout=5.0) is None
    await finished(process)
    assert "64 KiB" in await child_stderr(process)
