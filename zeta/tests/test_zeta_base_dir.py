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


def test_file_tools_resolve_relative_paths_under_base(tmp_path):
    from zeta.tools import bash, edit, grep, ls, read, write

    (tmp_path / "note.md").write_text("hello Acme\n", encoding="utf-8")
    token = set_base_dir(tmp_path)
    try:
        read_result = read.run({"path": "note.md"})
        assert read_result["ok"] is True
        assert "hello Acme" in read_result["content"][0]["text"]

        ls_result = ls.run({"path": "."})
        assert ls_result["ok"] is True
        assert "note.md" in ls_result["content"][0]["text"]

        grep_result = grep.run({"pattern": "Acme", "path": "."})
        assert grep_result["ok"] is True
        assert "note.md" in grep_result["content"][0]["text"]

        write_result = write.run({"path": "out.md", "content": "written"})
        assert write_result["ok"] is True
        assert (tmp_path / "out.md").read_text(encoding="utf-8") == "written"

        edit_result = edit.run({"location": "note.md", "old": "Acme", "new": "Beta"})
        assert edit_result["ok"] is True
        assert (tmp_path / "note.md").read_text(encoding="utf-8") == "hello Beta\n"

        bash_result = bash.run({"command": "ls"})
        assert bash_result["ok"] is True
        assert "out.md" in bash_result["content"][0]["text"]
    finally:
        reset_base_dir(token)
