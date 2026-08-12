"""Provider calls and source configuration over IPC."""

from __future__ import annotations

import asyncio
import sys
import textwrap

from ipc_test_support import (
    RUNTIME,
    finished,
    frame_reader,
    read_message,
    send,
    spawn,
    write_child,
)
from zeta.ipc.messages import request, success_response
from zeta.ipc.supervisor import PeerCommand, ProviderCallError, SubprocessPeer

PROVIDER_CHILD = """
    import asyncio

    from zeta.ipc.client import EventType, ProviderError, SourceEvent, run_peer


    async def deliver(input_value, base_dir, effect_key):
        del base_dir
        if input_value.get("boom"):
            raise ProviderError("provider_rejected", "provider rejected", retryable=True)
        if input_value.get("slow"):
            await asyncio.sleep(0.2)
        return {"delivered": input_value["text"], "effect_key": effect_key}


    def source(config):
        async def events():
            for item in config.get("emit", []):
                yield SourceEvent("note.created", {"text": item})
            await asyncio.sleep(60)

        return events()


    run_peer(
        source,
        name="notes",
        peer_version="0.0.1",
        event_types=[EventType("note.created", "note.created@1")],
        methods={"note.deliver": deliver},
    )
"""


def provider_child(tmp_path):
    return write_child(tmp_path, textwrap.dedent(PROVIDER_CHILD))


async def initialize_provider(process, reader, *, config: dict | None = None) -> dict:
    initialize = await read_message(reader)
    params = initialize["params"]
    assert params["roles"] == ["source", "provider"]
    assert params["methods"] == [{"name": "note.deliver"}]
    await send(
        process,
        success_response(
            initialize["id"],
            {
                "protocol_version": 0,
                "runtime": RUNTIME,
                "roles": params["roles"],
                "config": config or {},
                "heartbeat_seconds": 10,
                "max_in_flight": 64,
            },
        ),
    )
    return initialize


async def shutdown_provider(process, reader) -> None:
    await send(process, request("stop", "shutdown", {}))
    assert await read_message(reader) == success_response("stop", {})
    assert await finished(process) == 0


async def test_initialize_config_reaches_the_source_factory(tmp_path) -> None:
    process = await spawn(provider_child(tmp_path))
    reader = frame_reader(process)
    await initialize_provider(process, reader, config={"emit": ["alpha"]})
    publish = await read_message(reader)
    assert publish["method"] == "events.publish"
    assert publish["params"]["payload"] == {"text": "alpha"}
    await shutdown_provider(process, reader)


async def test_provider_methods_return_jsonrpc_results_and_errors(tmp_path) -> None:
    process = await spawn(provider_child(tmp_path))
    reader = frame_reader(process)
    await initialize_provider(process, reader)
    await send(
        process,
        request(
            "runtime-1",
            "note.deliver",
            {"input": {"text": "hi"}, "effect_key": "k-1"},
        ),
    )
    assert await read_message(reader) == success_response(
        "runtime-1", {"delivered": "hi", "effect_key": "k-1"}
    )

    await send(
        process,
        request(
            "runtime-read",
            "note.deliver",
            {"input": {"text": "read"}, "base_dir": "/workspace/zeta"},
        ),
    )
    assert await read_message(reader) == success_response(
        "runtime-read", {"delivered": "read", "effect_key": None}
    )

    await send(
        process,
        request(
            "runtime-2",
            "note.deliver",
            {"input": {"boom": True}, "effect_key": "k-2"},
        ),
    )
    failed = await read_message(reader)
    assert failed["id"] == "runtime-2"
    assert failed["error"]["code"] == -32000
    assert failed["error"]["data"] == {
        "code": "provider_rejected",
        "retryable": True,
    }

    await send(
        process,
        request("runtime-3", "undeclared.method", {"input": {}, "effect_key": "k"}),
    )
    undeclared = await read_message(reader)
    assert undeclared["error"]["code"] == -32601
    await shutdown_provider(process, reader)


async def test_subprocess_peer_calls_provider_methods_out_of_order(tmp_path) -> None:
    peer = SubprocessPeer(
        PeerCommand((sys.executable, str(provider_child(tmp_path)))),
        runtime_name=RUNTIME["name"],
        runtime_version=RUNTIME["version"],
        config={"emit": []},
    )
    consume = None
    generator = peer.publications()
    async with peer:
        consume = asyncio.create_task(generator.__anext__())
        deadline = asyncio.get_running_loop().time() + 5
        while peer.initialization is None:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.02)
        slow = asyncio.create_task(
            peer.call(
                "note.deliver",
                {"text": "slow", "slow": True},
                effect_key="k-1",
            )
        )
        fast = asyncio.create_task(
            peer.call("note.deliver", {"text": "fast"}, effect_key="k-2")
        )
        fast_result = await fast
        slow_result = await slow
        assert fast_result["delivered"] == "fast"
        assert slow_result["delivered"] == "slow"
        try:
            await peer.call("note.deliver", {"boom": True}, effect_key="k-3")
            raise AssertionError("expected ProviderCallError")
        except ProviderCallError as error:
            assert error.code == "provider_rejected"
            assert error.retryable
        try:
            await peer.call("undeclared.method", {}, effect_key="k-4")
            raise AssertionError("expected ProviderCallError")
        except ProviderCallError as error:
            assert error.code == "method_not_found"
            assert not error.retryable
    assert consume is not None
    consume.cancel()
    await asyncio.gather(consume, return_exceptions=True)
    await generator.aclose()


async def test_calls_to_a_dead_peer_fail_as_retryable(tmp_path) -> None:
    script = write_child(tmp_path, "raise SystemExit(1)")
    peer = SubprocessPeer(
        PeerCommand((sys.executable, str(script))),
        runtime_name=RUNTIME["name"],
        runtime_version=RUNTIME["version"],
        max_restarts=0,
        backoff_initial=0.05,
        backoff_cap=0.05,
    )
    async with peer:
        assert [publication async for publication in peer.publications()] == []
        try:
            await peer.call("anything", {}, effect_key="k-1")
            raise AssertionError("expected ProviderCallError")
        except ProviderCallError as error:
            assert error.retryable
