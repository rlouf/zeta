"""Plugin-side SDK behavior tests (run_source over real child processes)."""

import asyncio

from wire_test_support import (
    child_stderr,
    complete_handshake,
    finished,
    frame_reader,
    read_envelope,
    sdk_child_source,
    send,
    spawn,
    write_child,
)
from zeta.wire.envelopes import envelope, mint_event_id

PAYLOAD = {"dir": "/p/inbox", "name": "a.txt", "path": "/p/inbox/a.txt"}


def one_event_body(payload: dict) -> str:
    return f"""
    import asyncio
    yield SourceEvent("file.created", {payload!r})
    await asyncio.sleep(60)
    """


async def test_run_source_handshakes_and_mints_deterministic_event_ids(
    tmp_path,
) -> None:
    script = write_child(
        tmp_path, sdk_child_source(events_body=one_event_body(PAYLOAD))
    )
    process = await spawn(script)
    reader = frame_reader(process)
    hello = await complete_handshake(process, reader)
    assert hello["role"] == "source"
    assert hello["protocol_versions"] == [0]
    assert hello["capabilities"] == {"effects_are_proposals": False}
    event = await read_envelope(reader)
    assert event["kind"] == "event"
    assert event["id"] == mint_event_id("file.created", PAYLOAD)
    assert event["payload"] == PAYLOAD
    assert event["schema"] == "file.created@1"
    await send(process, envelope("shutdown", "m-t-9"))
    assert await finished(process) == 0


async def test_run_source_emits_heartbeats(tmp_path) -> None:
    script = write_child(
        tmp_path,
        sdk_child_source(
            events_body="""
            import asyncio
            await asyncio.sleep(60)
            yield SourceEvent("file.created", {})
            """,
            heartbeat_secs=1,
        ),
    )
    process = await spawn(script)
    reader = frame_reader(process)
    await complete_handshake(process, reader)
    beat = await read_envelope(reader, timeout=5.0)
    assert beat["kind"] == "heartbeat"
    await send(process, envelope("shutdown", "m-t-9"))
    assert await finished(process) == 0


async def test_run_source_keeps_stdout_pure_despite_prints_and_logging(
    tmp_path,
) -> None:
    """print() and logging inside a plugin land on stderr, never stdout."""
    script = write_child(
        tmp_path,
        sdk_child_source(
            events_body=f"""
            import asyncio, logging
            print("this print must not corrupt the protocol")
            logging.getLogger("plugin").warning("neither must logging")
            yield SourceEvent("file.created", {PAYLOAD!r})
            await asyncio.sleep(60)
            """,
        ),
    )
    process = await spawn(script)
    reader = frame_reader(process)
    await complete_handshake(process, reader)
    event = await read_envelope(reader)
    assert event["kind"] == "event"
    await send(process, envelope("shutdown", "m-t-9"))
    assert await finished(process) == 0
    stderr = await child_stderr(process)
    assert "this print must not corrupt the protocol" in stderr
    assert "neither must logging" in stderr


async def test_run_source_honors_the_ack_window(tmp_path) -> None:
    payloads = [
        {"dir": "/p", "name": f"f{index}.txt", "path": f"/p/f{index}.txt"}
        for index in range(3)
    ]
    script = write_child(
        tmp_path,
        sdk_child_source(
            events_body=f"""
            import asyncio
            for payload in {payloads!r}:
                yield SourceEvent("file.created", payload)
            await asyncio.sleep(60)
            """,
            ack_window=1,
        ),
    )
    process = await spawn(script)
    reader = frame_reader(process)
    hello = await complete_handshake(process, reader)
    assert hello["ack_window"] == 1

    first = await read_envelope(reader)
    assert first["payload"] == payloads[0]
    try:
        await asyncio.wait_for(reader.read_frame(), timeout=0.5)
        raise AssertionError("a second event arrived without an ack")
    except TimeoutError:
        pass
    await send(process, envelope("ack", "m-t-2", event_id=first["id"]))
    second = await read_envelope(reader)
    assert second["payload"] == payloads[1]
    await send(process, envelope("shutdown", "m-t-9"))
    assert await finished(process) == 0


async def test_run_source_rejects_oversized_inline_payloads(tmp_path) -> None:
    script = write_child(
        tmp_path,
        sdk_child_source(
            events_body="""
            yield SourceEvent("file.created", {"data": "x" * 70000})
            """,
        ),
    )
    process = await spawn(script)
    reader = frame_reader(process)
    await complete_handshake(process, reader)
    assert await asyncio.wait_for(reader.read_frame(), timeout=5.0) is None
    await finished(process)
    stderr = await child_stderr(process)
    assert "64 KiB" in stderr
