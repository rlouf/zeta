"""Fault drills for the runtime-side subprocess peer supervisor."""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap

from ipc_test_support import RUNTIME, write_child
from zeta.ipc.messages import request
from zeta.ipc.supervisor import PeerCommand, PublishRequest, SubprocessPeer

INITIALIZE = request(
    "peer-init",
    "initialize",
    {
        "protocol_versions": [0],
        "peer": {"name": "drill", "version": "0.0.1"},
        "roles": ["source"],
        "event_types": [{"type": "file.created", "schema": "file.created@1"}],
        "heartbeat_seconds": 1,
        "max_in_flight": 64,
    },
)
PAYLOAD = {"dir": "/p", "name": "a.txt", "path": "/p/a.txt"}
PUBLISH = request(
    1,
    "events.publish",
    {
        "type": "file.created",
        "payload": PAYLOAD,
        "idempotency_key": "file:/p/a.txt",
    },
)


def compact(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def peer_for(
    script,
    *,
    heartbeat_miss_limit: int = 3,
    max_restarts: int | None = None,
) -> SubprocessPeer:
    return SubprocessPeer(
        PeerCommand((sys.executable, str(script))),
        runtime_name=RUNTIME["name"],
        runtime_version=RUNTIME["version"],
        handshake_timeout=0.5,
        grace_seconds=0.3,
        backoff_initial=0.05,
        backoff_cap=0.1,
        heartbeat_miss_limit=heartbeat_miss_limit,
        max_restarts=max_restarts,
    )


def staged_child(tmp_path, first_run_body: str, second_run_body: str):
    marker = tmp_path / "ran-once"
    return write_child(
        tmp_path,
        f"""
        import json, sys, time
        from pathlib import Path

        marker = Path({str(marker)!r})
        first_run = not marker.exists()
        marker.write_text("x")

        def emit(value):
            line = value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
            sys.stdout.write(line + "\\n")
            sys.stdout.flush()

        def initialize():
            emit({INITIALIZE!r})
            response = json.loads(sys.stdin.readline())
            assert response["id"] == "peer-init" and "result" in response, response

        if first_run:
{textwrap.indent(textwrap.dedent(first_run_body).strip(), " " * 12)}
        else:
{textwrap.indent(textwrap.dedent(second_run_body).strip(), " " * 12)}
        """,
    )


GOOD_RUN = f"""
initialize()
emit({PUBLISH!r})
response = json.loads(sys.stdin.readline())
assert response["id"] == 1 and response["result"]["inserted"] is True, response
time.sleep(60)
"""


def publish_result(publication: PublishRequest) -> dict:
    return {
        "inserted": True,
        "event": {
            "id": "evt_1",
            "type": publication.type,
            "source": "drill",
            "payload": publication.payload,
            "idempotency_key": publication.idempotency_key,
            "caused_by": publication.caused_by,
            "session_id": publication.session_id,
            "run_id": publication.run_id,
            "turn_id": publication.turn_id,
            "timestamp_ms": 1,
            "cursor": 1,
        },
    }


async def collect_one_publication(peer: SubprocessPeer) -> PublishRequest:
    async with peer:
        async for publication in peer.publications():
            await peer.complete_publish(publication, publish_result(publication))
            return publication
    raise AssertionError("no publication before the supervisor gave up")


async def test_child_that_never_initializes_is_killed_and_respawned(tmp_path) -> None:
    script = staged_child(tmp_path, "time.sleep(60)", GOOD_RUN)
    peer = peer_for(script)
    publication = await asyncio.wait_for(collect_one_publication(peer), timeout=10)
    assert publication.payload == PAYLOAD
    assert peer.restarts == 1


async def test_junk_stdout_earns_strikes_without_crashing_the_parent(tmp_path) -> None:
    first = """
    initialize()
    emit("this is not json")
    emit('{"jsonrpc":"2.0"}')
    emit("neither is this")
    time.sleep(60)
    """
    script = staged_child(tmp_path, first, GOOD_RUN)
    peer = peer_for(script)
    publication = await asyncio.wait_for(collect_one_publication(peer), timeout=10)
    assert publication.payload == PAYLOAD
    assert peer.restarts == 1


async def test_unresponsive_child_is_pinged_then_respawned(tmp_path) -> None:
    first = """
    initialize()
    time.sleep(60)
    """
    script = staged_child(tmp_path, first, GOOD_RUN)
    peer = peer_for(script, heartbeat_miss_limit=1)
    publication = await asyncio.wait_for(collect_one_publication(peer), timeout=10)
    assert publication.payload == PAYLOAD
    assert peer.restarts == 1


async def test_respawn_backoff_is_capped_and_the_supervisor_survives(tmp_path) -> None:
    script = write_child(tmp_path, "raise SystemExit(1)")
    peer = peer_for(script, max_restarts=4)
    async with peer:
        assert [publication async for publication in peer.publications()] == []
    assert peer.restarts == 5


async def test_supervisor_rejects_an_unsupported_role_as_invalid_params(
    tmp_path,
) -> None:
    checked = tmp_path / "checked"
    client_initialize = request(
        "peer-init",
        "initialize",
        {
            "protocol_versions": [0],
            "peer": {"name": "client", "version": "0.1.0"},
            "roles": ["client"],
            "heartbeat_seconds": 10,
            "max_in_flight": 64,
        },
    )
    script = write_child(
        tmp_path,
        f"""
        import json, sys
        from pathlib import Path

        sys.stdout.write({compact(client_initialize)!r} + "\\n")
        sys.stdout.flush()
        response = json.loads(sys.stdin.readline())
        assert response["error"]["code"] == -32602, response
        Path({str(checked)!r}).write_text("ok")
        """,
    )
    peer = peer_for(script, max_restarts=0)
    async with peer:
        assert [publication async for publication in peer.publications()] == []
    assert checked.read_text() == "ok"


async def test_publish_is_answered_only_when_the_consumer_completes_it(
    tmp_path,
) -> None:
    script = staged_child(tmp_path, GOOD_RUN, GOOD_RUN)
    peer = peer_for(script, max_restarts=0)
    generator = peer.publications()
    async with peer:
        publication = await generator.__anext__()
        process = peer._process
        assert process is not None and process.returncode is None
        await peer.complete_publish(publication, publish_result(publication))
    await generator.aclose()


async def test_shutdown_escalates_to_sigkill_for_a_deaf_child(tmp_path) -> None:
    script = write_child(
        tmp_path,
        f"""
        import json, signal, sys, time

        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        sys.stdout.write({compact(INITIALIZE)!r} + "\\n")
        sys.stdout.flush()
        json.loads(sys.stdin.readline())
        time.sleep(60)
        """,
    )
    peer = peer_for(script)
    generator = peer.publications()
    consume = asyncio.create_task(generator.__anext__())
    deadline = asyncio.get_running_loop().time() + 2
    while peer.initialization is None:
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.02)
    process = peer._process
    assert process is not None
    await asyncio.wait_for(peer.aclose(), timeout=5)
    assert process.returncode == -9
    consume.cancel()
    await asyncio.gather(consume, return_exceptions=True)
    await generator.aclose()
