"""Config, ack-gated cursors, and operation calls over wire-v0."""

import asyncio
import sys
import textwrap

from wire_test_support import (
    RUNTIME_ID,
    finished,
    frame_reader,
    read_envelope,
    send,
    spawn,
    write_child,
)
from zeta.wire.envelopes import envelope
from zeta.wire.host import CallError, SourceCommand, SubprocessSource

OPERATIONS_CHILD = """
    import asyncio

    from zeta.wire.plugin import EventType, OperationError, SourceEvent, run_source


    async def deliver(payload, effect_key):
        if payload.get("boom"):
            raise OperationError("internal", "provider rejected", retryable=True)
        return {"delivered": payload["text"], "effect_key": effect_key}


    def source(config):
        async def events():
            for item in config.get("emit", []):
                yield SourceEvent(
                    "note.created",
                    {"text": item},
                    on_ack=lambda item=item: print(f"cursor:{item}"),
                )
            await asyncio.sleep(60)

        return events()


    run_source(
        source,
        name="notes",
        plugin_version="0.0.1",
        event_types=[EventType("note.created", "note.created@1")],
        operations={"note.deliver": deliver},
    )
"""


def operations_child(tmp_path):
    return write_child(tmp_path, textwrap.dedent(OPERATIONS_CHILD))


async def test_hello_ack_config_reaches_the_source_factory(tmp_path) -> None:
    process = await spawn(operations_child(tmp_path))
    reader = frame_reader(process)
    hello = await read_envelope(reader)
    assert hello["operations"] == [{"name": "note.deliver"}]
    await send(
        process,
        envelope(
            "hello_ack",
            "m-t-1",
            protocol_version=0,
            runtime=RUNTIME_ID,
            config={"emit": ["alpha"]},
        ),
    )
    event = await read_envelope(reader)
    assert event["kind"] == "event"
    assert event["payload"] == {"text": "alpha"}
    await send(process, envelope("shutdown", "m-t-9"))
    assert await finished(process) == 0


async def test_on_ack_runs_only_after_the_runtime_acks(tmp_path) -> None:
    """The upstream cursor advances on ack, not on emit (spec §7)."""
    process = await spawn(operations_child(tmp_path))
    reader = frame_reader(process)
    await read_envelope(reader)
    await send(
        process,
        envelope(
            "hello_ack",
            "m-t-1",
            protocol_version=0,
            runtime=RUNTIME_ID,
            config={"emit": ["alpha"]},
        ),
    )
    event = await read_envelope(reader)
    await send(process, envelope("ack", "m-t-2", event_id=event["id"]))
    await send(process, envelope("shutdown", "m-t-9"))
    assert await finished(process) == 0
    assert process.stderr is not None
    stderr = (await process.stderr.read()).decode()
    assert "cursor:alpha" in stderr


async def test_operations_answer_calls_with_results_and_errors(tmp_path) -> None:
    process = await spawn(operations_child(tmp_path))
    reader = frame_reader(process)
    await read_envelope(reader)
    await send(
        process,
        envelope("hello_ack", "m-t-1", protocol_version=0, runtime=RUNTIME_ID),
    )
    await send(
        process,
        envelope(
            "call",
            "m-t-2",
            name="note.deliver",
            payload={"text": "hi"},
            effect_key="k-1",
        ),
    )
    result = await read_envelope(reader)
    assert result["kind"] == "call_result"
    assert result["call_id"] == "m-t-2"
    assert result["ok"] is True
    assert result["result"] == {"delivered": "hi", "effect_key": "k-1"}

    await send(
        process,
        envelope(
            "call",
            "m-t-3",
            name="note.deliver",
            payload={"boom": True},
            effect_key="k-2",
        ),
    )
    failed = await read_envelope(reader)
    assert failed["ok"] is False
    assert failed["error"]["code"] == "internal"
    assert failed["error"]["retryable"] is True

    await send(
        process,
        envelope("call", "m-t-4", name="nope", payload={}, effect_key="k-3"),
    )
    undeclared = await read_envelope(reader)
    assert undeclared["ok"] is False
    assert undeclared["error"]["code"] == "protocol"
    await send(process, envelope("shutdown", "m-t-9"))
    assert await finished(process) == 0


async def test_subprocess_source_calls_operations_round_trip(tmp_path) -> None:
    script = operations_child(tmp_path)
    source = SubprocessSource(
        SourceCommand((sys.executable, str(script))),
        runtime_id=RUNTIME_ID,
        config={"emit": []},
    )
    consume = None
    async with source:
        generator = source.events()
        consume = asyncio.ensure_future(generator.__anext__())
        deadline = asyncio.get_running_loop().time() + 5
        while source.hello is None:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.02)
        result = await source.call("note.deliver", {"text": "hi"}, "k-1")
        assert result == {"delivered": "hi", "effect_key": "k-1"}
        try:
            await source.call("note.deliver", {"boom": True}, "k-2")
            raise AssertionError("expected CallError")
        except CallError as error:
            assert error.code == "internal"
            assert error.retryable
        try:
            await source.call("undeclared.op", {}, "k-3")
            raise AssertionError("expected CallError")
        except CallError as error:
            assert error.code == "protocol"
    consume.cancel()
    try:
        await consume
    except (asyncio.CancelledError, StopAsyncIteration):
        pass
    await generator.aclose()


async def test_calls_to_a_dead_child_fail_as_retryable(tmp_path) -> None:
    script = write_child(tmp_path, "raise SystemExit(1)")
    source = SubprocessSource(
        SourceCommand((sys.executable, str(script))),
        runtime_id=RUNTIME_ID,
        max_restarts=0,
        backoff_initial=0.05,
        backoff_cap=0.05,
    )
    async with source:
        events = [event async for event in source.events()]
        assert events == []
        try:
            await source.call("anything", {}, "k-1")
            raise AssertionError("expected CallError")
        except CallError as error:
            assert error.retryable
