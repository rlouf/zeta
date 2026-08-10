"""Fault drills for the runtime-side SubprocessSource supervisor."""

import asyncio
import sys

from wire_test_support import RUNTIME_ID, write_child
from zeta.wire.envelopes import canonical_json, envelope, mint_event_id
from zeta.wire.host import SourceCommand, SubprocessSource

HELLO = envelope(
    "hello",
    "m-c-1",
    name="drill",
    plugin_version="0.0.1",
    role="source",
    protocol_versions=[0],
    event_types=[{"type": "file.created", "schema": "file.created@1"}],
)
PAYLOAD = {"dir": "/p", "name": "a.txt", "path": "/p/a.txt"}
EVENT = envelope(
    "event",
    mint_event_id("file.created", PAYLOAD),
    type="file.created",
    schema="file.created@1",
    caused_by=None,
    session_id=None,
    payload=PAYLOAD,
)


def source_for(
    script,
    *,
    heartbeat_miss_limit: int = 3,
    max_restarts: int | None = None,
) -> SubprocessSource:
    return SubprocessSource(
        SourceCommand((sys.executable, str(script))),
        runtime_id=RUNTIME_ID,
        handshake_timeout=0.5,
        grace_seconds=0.3,
        backoff_initial=0.05,
        backoff_cap=0.1,
        heartbeat_miss_limit=heartbeat_miss_limit,
        max_restarts=max_restarts,
    )


def staged_child(tmp_path, first_run_body: str, second_run_body: str):
    """A child whose behavior differs between its first and later runs."""
    marker = tmp_path / "ran-once"
    return write_child(
        tmp_path,
        f"""
        import json, sys, time
        from pathlib import Path

        marker = Path({str(marker)!r})
        first_run = not marker.exists()
        marker.write_text("x")

        def emit(line):
            sys.stdout.write(line + "\\n")
            sys.stdout.flush()

        def emit_hello_and_wait_ack():
            emit({canonical_json(HELLO)!r})
            ack = json.loads(sys.stdin.readline())
            assert ack["kind"] == "hello_ack", ack

        if first_run:
{_indent8(first_run_body)}
        else:
{_indent8(second_run_body)}
        """,
    )


def _indent8(body: str) -> str:
    import textwrap

    return textwrap.indent(textwrap.dedent(body).strip("\n"), " " * 12)


GOOD_RUN = f"""
emit_hello_and_wait_ack()
emit({canonical_json(EVENT)!r})
json.loads(sys.stdin.readline())
time.sleep(60)
"""


async def collect_one_event(source: SubprocessSource):
    async with source:
        async for event in source.events():
            await source.ack(event.id)
            return event
    raise AssertionError("no event before the supervisor gave up")


async def test_child_that_never_handshakes_is_killed_and_respawned(tmp_path) -> None:
    script = staged_child(tmp_path, "time.sleep(60)", GOOD_RUN)
    source = source_for(script)
    event = await asyncio.wait_for(collect_one_event(source), timeout=10)
    assert event.payload == PAYLOAD
    assert source.restarts == 1


async def test_junk_stdout_earns_strikes_but_never_kills_the_parent(tmp_path) -> None:
    first = """
    emit_hello_and_wait_ack()
    emit("this is not json")
    emit('{"v":0}')
    emit("neither is this")
    time.sleep(60)
    """
    script = staged_child(tmp_path, first, GOOD_RUN)
    source = source_for(script)
    event = await asyncio.wait_for(collect_one_event(source), timeout=10)
    assert event.payload == PAYLOAD
    assert source.restarts == 1


async def test_child_that_stops_heartbeating_is_killed_and_respawned(
    tmp_path,
) -> None:
    first = """
    emit_hello_and_wait_ack()
    time.sleep(60)
    """
    script = staged_child(tmp_path, first, GOOD_RUN)
    source = source_for(script, heartbeat_miss_limit=1)
    hello_with_fast_heartbeat = {**HELLO, "heartbeat_secs": 1}
    script.write_text(
        script.read_text().replace(
            canonical_json(HELLO), canonical_json(hello_with_fast_heartbeat)
        )
    )
    event = await asyncio.wait_for(collect_one_event(source), timeout=10)
    assert event.payload == PAYLOAD
    assert source.restarts == 1


async def test_respawn_backoff_is_capped_and_the_supervisor_survives(
    tmp_path,
) -> None:
    script = write_child(tmp_path, "raise SystemExit(1)")
    source = source_for(script, max_restarts=4)
    async with source:
        events = [event async for event in source.events()]
    assert events == []
    assert source.restarts == 5


async def test_ack_window_overflow_is_a_protocol_error_on_the_child(
    tmp_path,
) -> None:
    hello_small_window = {**HELLO, "ack_window": 1}
    second_payload = {"dir": "/p", "name": "b.txt", "path": "/p/b.txt"}
    second_event = envelope(
        "event",
        mint_event_id("file.created", second_payload),
        type="file.created",
        schema="file.created@1",
        caused_by=None,
        session_id=None,
        payload=second_payload,
    )
    first = f"""
    emit({canonical_json(hello_small_window)!r})
    ack = json.loads(sys.stdin.readline())
    assert ack["kind"] == "hello_ack", ack
    emit({canonical_json(EVENT)!r})
    emit({canonical_json(second_event)!r})
    time.sleep(60)
    """
    script = staged_child(tmp_path, first, GOOD_RUN)
    source = source_for(script)
    received = []
    async with source:
        async for event in source.events():
            received.append(event)
            if source.restarts == 1:
                await source.ack(event.id)
                break
    assert source.restarts == 1
    assert received[-1].payload == PAYLOAD


async def test_payload_hash_events_are_refused_as_unsupported(tmp_path) -> None:
    hash_event = envelope(
        "event",
        "b3:" + "a" * 64,
        type="file.created",
        schema="file.created@1",
        caused_by=None,
        session_id=None,
        payload_hash="b3:" + "b" * 64,
    )
    first = f"""
    emit_hello_and_wait_ack()
    emit({canonical_json(hash_event)!r})
    message = json.loads(sys.stdin.readline())
    assert message["kind"] == "error" and message["code"] == "unsupported", message
    emit({canonical_json(EVENT)!r})
    json.loads(sys.stdin.readline())
    time.sleep(60)
    """
    script = staged_child(tmp_path, first, GOOD_RUN)
    source = source_for(script)
    event = await asyncio.wait_for(collect_one_event(source), timeout=10)
    assert event.payload == PAYLOAD
    assert source.restarts == 0


async def test_shutdown_escalates_to_sigkill_for_a_deaf_child(tmp_path) -> None:
    script = write_child(
        tmp_path,
        f"""
        import json, signal, sys, time

        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        sys.stdout.write({canonical_json(HELLO)!r} + "\\n")
        sys.stdout.flush()
        json.loads(sys.stdin.readline())
        time.sleep(60)
        """,
    )
    source = source_for(script)
    generator = source.events()
    consume = asyncio.ensure_future(generator.__anext__())
    await asyncio.sleep(0.5)
    process = source._process
    assert process is not None
    await asyncio.wait_for(source.aclose(), timeout=5)
    assert process.returncode == -9
    consume.cancel()
    try:
        await consume
    except (asyncio.CancelledError, StopAsyncIteration):
        pass
    await generator.aclose()
