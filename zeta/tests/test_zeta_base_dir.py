"""Base-directory resolution for file capability paths."""

import asyncio
from pathlib import Path

from zeta.capabilities.paths import reset_base_dir, resolve_path, set_base_dir


def test_resolve_path_without_base_returns_path_unchanged():
    assert resolve_path("relationships/Acme.md") == Path("relationships/Acme.md")


def test_resolve_path_joins_relative_under_base():
    token = set_base_dir(Path("/vault"))
    try:
        assert resolve_path("inbox/note.md") == Path("/vault/inbox/note.md")
    finally:
        reset_base_dir(token)


def test_resolve_path_passes_absolute_through_when_base_set():
    token = set_base_dir(Path("/vault"))
    try:
        assert resolve_path("/etc/hosts") == Path("/etc/hosts")
    finally:
        reset_base_dir(token)


def test_reset_base_dir_restores_absence_of_base():
    token = set_base_dir(Path("/vault"))
    reset_base_dir(token)
    assert resolve_path("x.md") == Path("x.md")


def test_base_dir_is_task_local():
    async def worker(base: str) -> Path:
        token = set_base_dir(Path(base))
        await asyncio.sleep(0)  # yield so tasks interleave
        result = resolve_path("n.md")
        reset_base_dir(token)
        return result

    async def main():
        return await asyncio.gather(worker("/a"), worker("/b"))

    results = asyncio.run(main())
    assert Path("/a/n.md") in results
    assert Path("/b/n.md") in results


def test_in_process_host_activates_base_dir_from_context():
    from zeta.capabilities.execution import (
        CapabilityExecutionContext,
        InProcessCapabilityExecutor,
    )
    from zeta.capabilities.host import TransitionalInProcessHost
    from zeta.capabilities.registry import CapabilityRegistry, RegisteredCapability
    from zeta.capabilities.types import Capability, CapabilityId

    def probe(params: dict) -> dict:
        return {"ok": True, "resolved": str(resolve_path("note.md"))}

    registry = CapabilityRegistry()
    registry.register(
        RegisteredCapability(
            Capability(
                CapabilityId("test", "probe"),
                "resolve a relative path against the active base",
                {"type": "object", "additionalProperties": True},
            ),
            InProcessCapabilityExecutor(probe, None),
        )
    )
    host = TransitionalInProcessHost(registry)
    ctx = CapabilityExecutionContext(
        event_sink=None,
        trace_store=None,
        tool_registry=registry,
        base_dir=Path("/vault"),
    )

    result = asyncio.run(host.call("test.probe", {}, "direct", ctx))

    assert result["resolved"] == str(Path("/vault/note.md"))
    # base did not leak out of the call
    assert resolve_path("note.md") == Path("note.md")
