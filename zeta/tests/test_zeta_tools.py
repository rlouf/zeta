"""Builtin tool tests."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import shutil
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from zeta.capabilities.execution import tool_args_schema_error
from zeta.capabilities.executors import (
    InProcessCapabilityExecutor,
    ToolExecutorProvider,
    load_tool_executor_provider_registry,
)
from zeta.capabilities.registry import (
    CapabilityRegistry,
    RegisteredCapability,
    validated_capability_result_payload,
)
from zeta.capabilities.registry import registry as tool_registry
from zeta.capabilities.types import (
    Capability,
    CapabilityId,
)
from zeta.events import Event
from zeta.journal.memory import MemoryEventStore
from zeta.loop.runtime import registered_capabilities
from zeta.tools import bash as bash_tool
from zeta.tools import ensure_builtin_tools_registered, register_builtin_tools
from zeta.tools import grep as grep_tool
from zeta.tools import read as read_tool
from zeta.tools import web as web_tool
from zeta.trace.query import (
    MAX_QUERY_LOG_EVENTS,
    MAX_QUERY_LOG_OUTPUT_CHARS,
    query_run_log,
)

ensure_builtin_tools_registered()


def tool_metadata(name: str) -> dict[str, Any]:
    capability = tool_registry.get_by_name(name)
    assert capability is not None
    return {
        "id": capability.declaration.id.canonical(),
        "provider": capability.declaration.id.provider,
        "name": capability.declaration.id.name,
        "description": capability.declaration.description,
        "input_schema": capability.declaration.input_schema,
    }


def _test_capability(
    name: str,
    *,
    provider: str = "test",
    schema: dict[str, Any] | None = None,
    run_result: dict[str, Any] | None = None,
) -> RegisteredCapability:
    return RegisteredCapability(
        Capability(
            CapabilityId(provider, name),
            "Unit test capability.",
            schema or {"type": "object"},
        ),
        InProcessCapabilityExecutor(
            lambda params: run_result or {"ok": True, "metadata": params},
        ),
    )


def test_zeta_capability_registry_registers_and_lists_capabilities() -> None:
    capability = _test_capability("unit")
    registry = CapabilityRegistry()

    registry.register(capability)

    assert registry.get("test.unit") is capability
    assert registry.list_capability_ids() == ["test.unit"]
    assert capability.declaration.id.canonical() == "test.unit"


def test_zeta_tool_executor_provider_registry_loads_builtin_and_entry_point() -> None:
    async def setup(
        agent_id: str,
        registry: CapabilityRegistry,
        config: Mapping[str, Any],
    ) -> Any:
        del agent_id, registry, config
        raise AssertionError("setup should not be called while loading")

    provider = ToolExecutorProvider("remote", setup)

    class EntryPoint:
        name = "remote"
        group = "zeta.tool_executors"

        def load(self) -> ToolExecutorProvider:
            return provider

    registry = load_tool_executor_provider_registry([EntryPoint()])

    assert registry.resolve("local") is not None
    assert registry.resolve("remote") == provider
    with pytest.raises(ValueError, match="already registered"):
        registry.register(provider)


def test_zeta_capability_registry_accepts_unchecked_capability_schema() -> None:
    registry = CapabilityRegistry()
    capability = _test_capability(
        "bad",
        schema={"type": "definitely-not-json-schema"},
    )

    registry.register(capability)

    assert registry.get("test.bad") is capability


def test_zeta_capability_registry_executes_registered_capability() -> None:
    registry = CapabilityRegistry()
    registry.register(_test_capability("unit"))

    result = registry.invoke("unit", {})

    assert result == {"ok": True, "metadata": {}}


def test_zeta_capability_registry_normalizes_malformed_executor_result() -> None:
    registry = CapabilityRegistry()
    registry.register(
        _test_capability(
            "unit",
            run_result={"content": [{"type": "text", "text": "raw text"}]},
        )
    )

    result = registry.invoke("unit", {})

    assert result == {
        "ok": False,
        "content": [{"type": "text", "text": "raw text"}],
        "error": {
            "code": "invalid-capability-result",
            "message": "capability result must include boolean ok",
            "data": {"capability_id": "test.unit"},
        },
    }


def test_zeta_capability_result_validation_keeps_success_fields() -> None:
    payload = validated_capability_result_payload(
        "test.unit",
        {
            "ok": True,
            "content": [{"type": "text", "text": "done"}],
            "metadata": {"path": "README.md"},
            "debug": {"attempt": 1},
        },
    )

    assert payload == {
        "ok": True,
        "content": [{"type": "text", "text": "done"}],
        "metadata": {"path": "README.md"},
        "debug": {"attempt": 1},
    }


def test_zeta_capability_result_validation_keeps_extra_fields() -> None:
    payload = validated_capability_result_payload(
        "test.write",
        {"ok": True, "metadata": {"kind": "file"}},
    )

    assert payload == {
        "ok": True,
        "metadata": {"kind": "file"},
    }


def test_zeta_capability_result_validation_keeps_structured_error() -> None:
    payload = validated_capability_result_payload(
        "test.read",
        {
            "ok": False,
            "error": {
                "code": "read-failed",
                "message": "missing",
                "data": {"path": "missing.txt"},
            },
        },
    )

    assert payload == {
        "ok": False,
        "error": {
            "code": "read-failed",
            "message": "missing",
            "data": {"path": "missing.txt"},
        },
    }


def test_zeta_capability_result_validation_marks_malformed_result_as_error() -> None:
    payload = validated_capability_result_payload(
        "test.unit",
        {"content": [{"type": "text", "text": "raw text"}]},
    )

    assert payload == {
        "ok": False,
        "content": [{"type": "text", "text": "raw text"}],
        "error": {
            "code": "invalid-capability-result",
            "message": "capability result must include boolean ok",
            "data": {"capability_id": "test.unit"},
        },
    }


def test_zeta_capability_registry_converts_executor_exception_to_error_result() -> None:
    registry = CapabilityRegistry()

    def crash(params: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("boom")

    registry.register(
        RegisteredCapability(
            Capability(
                CapabilityId("test", "crash"),
                "Crash test capability.",
                {"type": "object"},
            ),
            InProcessCapabilityExecutor(crash),
        )
    )

    result = registry.invoke("crash", {})

    assert result == {
        "ok": False,
        "error": {
            "code": "executor-exception",
            "message": "RuntimeError: boom",
            "data": {"capability_id": "test.crash"},
        },
    }


def test_zeta_in_process_capability_executor_runs_capability() -> None:
    capability = _test_capability("read")

    result = asyncio.run(
        capability.executor(
            {"path": "README.md"},
        )
    )

    assert result == {"ok": True, "metadata": {"path": "README.md"}}


def test_zeta_capability_registry_rejects_duplicate_canonical_ids() -> None:
    registry = CapabilityRegistry()
    registry.register(_test_capability("read"))

    with pytest.raises(
        ValueError, match="capability 'test.read' is already registered"
    ):
        registry.register(_test_capability("read"))


def test_zeta_capability_tool_schema_rejects_ambiguous_names() -> None:
    registry = CapabilityRegistry()
    registry.register(_test_capability("read", provider="host"))
    registry.register(_test_capability("read", provider="rpc"))

    with pytest.raises(ValueError, match="ambiguous capability name 'read'"):
        registry.model_tool_schema(("host.read", "rpc.read"))


def test_zeta_capability_tool_schema_can_use_qualified_names() -> None:
    registry = CapabilityRegistry()
    registry.register(_test_capability("read", provider="host"))
    registry.register(_test_capability("read", provider="rpc"))

    tool_schema = registry.model_tool_schema(
        ("host.read", "rpc.read"),
        name_overrides={
            "host.read": "host.read",
            "rpc.read": "rpc.read",
        },
    )

    assert tool_schema.name_to_id == {
        "host.read": "host.read",
        "rpc.read": "rpc.read",
    }
    assert [
        descriptor["function"]["name"] for descriptor in tool_schema.descriptors
    ] == ["host.read", "rpc.read"]


def test_zeta_capability_registry_starts_empty() -> None:
    registry = CapabilityRegistry()

    assert registry.list_capability_ids() == []


def test_zeta_registers_builtin_tools_explicitly() -> None:
    registry = CapabilityRegistry()

    register_builtin_tools(registry)

    assert {
        "zeta.ast_grep",
        "zeta.read",
        "zeta.grep",
        "zeta.ls",
        "zeta.bash",
        "zeta.edit",
        "zeta.query_content",
        "zeta.query_log",
        "zeta.transform_content",
        "zeta.write",
        "zeta.web_search",
    } <= set(registry.list_capability_ids())
    assert "zeta.web_fetch" not in set(registry.list_capability_ids())


def test_zeta_ensures_shared_registry_has_builtins() -> None:
    ensure_builtin_tools_registered()

    names = set(tool_registry.list_capability_ids())
    assert {
        "zeta.read",
        "zeta.grep",
        "zeta.ast_grep",
        "zeta.ls",
        "zeta.bash",
        "zeta.edit",
        "zeta.query_content",
        "zeta.query_log",
        "zeta.transform_content",
        "zeta.write",
        "zeta.web_search",
    } <= names
    assert "zeta.web_fetch" not in names


def test_zeta_capability_registry_does_not_import_commas_tools() -> None:
    source = Path("zeta/src/zeta/capabilities/registry.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.append(node.module)
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)

    assert all(not module.startswith("commas.tools") for module in imports)


def test_zeta_grep_metadata_guides_model_tool_choice() -> None:
    metadata = tool_metadata("grep")
    schema = metadata["input_schema"]

    assert (
        metadata["description"]
        == "Search file contents recursively. Use before read when looking for symbols, errors, strings, or definitions. Successful results include [path#tag] snapshot headers and numbered lines for grounded edits."
    )
    assert schema["properties"]["pattern"]["description"] == (
        "Text or regular expression to search for."
    )
    assert schema["properties"]["path"]["description"] == (
        "File or directory to search. Defaults to the current working directory."
    )
    assert schema["properties"]["limit"]["description"] == (
        "Maximum number of matching lines to return."
    )


def test_zeta_ast_grep_metadata_guides_model_tool_choice() -> None:
    metadata = tool_metadata("ast_grep")
    schema = metadata["input_schema"]

    assert metadata["description"] == (
        "Search code structurally with ast-grep. Use when looking for syntax "
        "patterns rather than plain text. Results include [path#tag] snapshot "
        "headers and numbered matched lines for grounded edits."
    )
    assert schema["required"] == ["pattern", "lang"]
    assert schema["properties"]["pattern"]["description"].startswith(
        "ast-grep structural pattern"
    )


def test_zeta_web_search_schema_matches_codex_contract() -> None:
    schema = web_tool.SEARCH_SPEC.input_schema

    assert web_tool.SEARCH_SPEC.id.canonical() == "zeta.web_search"
    assert web_tool.SEARCH_SPEC.id.name == "web_search"
    assert schema["required"] == ["query"]
    assert schema["properties"]["query"]["type"] == "string"
    assert schema["properties"]["limit"]["minimum"] == 1


def test_zeta_web_search_reports_missing_codex_credentials(monkeypatch) -> None:
    def missing_credentials() -> web_tool.CodexCredentials:
        raise RuntimeError("no Codex credentials at ~/.codex/auth.json")

    monkeypatch.setattr(web_tool, "load_codex_credentials", missing_credentials)

    result = asyncio.run(web_tool.search({"query": "parallel api docs"}))

    assert result == {
        "ok": False,
        "error": {
            "code": "codex-auth-missing",
            "message": "no Codex credentials at ~/.codex/auth.json",
        },
    }


def test_zeta_web_search_posts_codex_payload(monkeypatch) -> None:
    calls: list[tuple[str, web_tool.WebConfig]] = []

    monkeypatch.setenv("ZETA_WEB_SEARCH_MODEL", "gpt-test")
    monkeypatch.setattr(
        web_tool,
        "load_codex_credentials",
        lambda: web_tool.CodexCredentials(access_token="tok-1", account_id="acct-1"),
    )

    async def fake_request(
        query: str, config: web_tool.WebConfig
    ) -> web_tool.CodexSearch:
        calls.append((query, config))
        return web_tool.CodexSearch(
            answer="Parallel documents the Search API.",
            sources=[
                web_tool.SearchSource(
                    title="Parallel docs",
                    url="https://docs.parallel.ai/search",
                    snippet="Search API overview",
                )
            ],
            request_id="resp_123",
            model="gpt-test",
            usage={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
        )

    monkeypatch.setattr(web_tool, "codex_search", fake_request)

    result = asyncio.run(web_tool.search({"query": "parallel search api", "limit": 5}))

    assert result["ok"] is True
    assert calls == [
        (
            "parallel search api",
            web_tool.WebConfig(
                credentials=web_tool.CodexCredentials(
                    access_token="tok-1",
                    account_id="acct-1",
                ),
                model="gpt-test",
                timeout_sec=30.0,
                max_preview_bytes=8192,
                max_preview_lines=100,
                limit=5,
            ),
        )
    ]
    text = result["content"][0]["text"]
    assert "Parallel documents the Search API." in text
    assert "## Sources" in text
    assert "[1] [Parallel docs](https://docs.parallel.ai/search)" in text
    assert "Search API overview" in text
    assert result["metadata"]["provider"] == "codex"
    assert result["metadata"]["request_id"] == "resp_123"
    assert result["metadata"]["model"] == "gpt-test"
    assert result["metadata"]["result_count"] == 1


def test_zeta_read_fetches_public_url(monkeypatch) -> None:
    class FakeResponse:
        headers = {"content-type": "text/html"}

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"<html><head><title>Example</title></head><body><h1>Hello</h1><p>World</p></body></html>"

    requests: list[Any] = []

    def fake_open(request: Any, timeout: float) -> FakeResponse:
        requests.append((request, timeout))
        return FakeResponse()

    monkeypatch.setattr(read_tool._URL_OPENER, "open", fake_open)
    monkeypatch.setattr(
        read_tool.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )

    result = read_tool.run({"path": "https://example.com", "limit": 5})

    assert result["ok"] is True
    assert requests
    text = result["content"][0]["text"]
    assert "[https://example.com#" in text
    assert "1:Example" in text
    assert "2:# Hello" in text
    assert "3:World" in text
    assert result["metadata"]["source"] == "web"
    assert result["metadata"]["url"] == "https://example.com"


def test_zeta_read_blocks_loopback_url() -> None:
    result = read_tool.run({"path": "http://127.0.0.1/secret"})

    assert result["ok"] is False
    assert result["error"]["code"] == "web-read-blocked"


def test_zeta_read_blocks_cloud_metadata_url() -> None:
    result = read_tool.run({"path": "http://169.254.169.254/latest/meta-data/"})

    assert result["ok"] is False
    assert result["error"]["code"] == "web-read-blocked"


def test_zeta_tool_args_schema_error_skips_malformed_schema() -> None:
    # A schema with an invalid "type" must be skipped, not crash dispatch.
    assert tool_args_schema_error({"x": 1}, {"type": "not_a_type"}) is None
    # A valid schema still reports violations.
    assert tool_args_schema_error({}, {"required": ["path"]}) is not None


def test_zeta_read_blocks_redirect_to_private_host() -> None:
    handler = read_tool._BlockPrivateRedirects()

    with pytest.raises(read_tool.urllib.error.HTTPError):
        handler.redirect_request(
            None,
            None,
            302,
            "Found",
            {},
            "http://169.254.169.254/latest/meta-data/",
        )


def test_zeta_tool_read_schema_and_run(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("hello zeta\n", encoding="utf-8")

    assert tool_metadata("read")["input_schema"]["required"] == ["path"]

    data = tool_registry.invoke("read", {"path": str(target)})
    assert data["ok"] is True
    tag = data["metadata"]["tag"]
    assert data["content"][0]["text"] == f"[{target}#{tag}]\n1:hello zeta\n"
    assert data["metadata"]["content_hash"].startswith("sha256:")
    assert data["metadata"]["line_start"] == 1
    assert data["metadata"]["line_end"] == 1


def test_zeta_tool_read_offset_and_limit_select_lines(tmp_path: Path) -> None:
    target = tmp_path / "lines.txt"
    target.write_text("one\ntwo\nthree\nfour\nfive\n", encoding="utf-8")

    data = tool_registry.invoke("read", {"path": str(target), "offset": 1, "limit": 2})

    assert data["ok"] is True
    tag = data["metadata"]["tag"]
    assert data["content"][0]["text"] == f"[{target}#{tag}]\n2:two\n3:three\n"
    assert data["metadata"]["offset"] == 1
    assert data["metadata"]["limit"] == 2
    assert data["metadata"]["line_start"] == 2
    assert data["metadata"]["line_end"] == 3


def test_zeta_tool_read_limit_past_end_returns_remaining_lines(tmp_path: Path) -> None:
    target = tmp_path / "short.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")

    data = tool_registry.invoke("read", {"path": str(target), "offset": 1, "limit": 10})

    tag = data["metadata"]["tag"]
    assert data["content"][0]["text"] == f"[{target}#{tag}]\n2:beta\n"


def test_zeta_tool_read_rejects_binary_file(tmp_path: Path) -> None:
    target = tmp_path / "image.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")

    data = tool_registry.invoke("read", {"path": str(target)})

    assert data["ok"] is False
    assert data["error"]["code"] == "binary-file"


def test_zeta_tool_read_caps_returned_characters(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(read_tool, "MAX_READ_CHARS", 100)
    target = tmp_path / "wide.txt"
    target.write_text("x" * 1_000 + "\n", encoding="utf-8")

    data = tool_registry.invoke("read", {"path": str(target)})

    assert data["ok"] is True
    assert len(data["content"][0]["text"]) == 100
    assert data["metadata"]["truncated"] is True


def test_zeta_tool_grep_reports_total_limited_metadata(tmp_path: Path) -> None:
    first = tmp_path / "a.txt"
    first.write_text("needle one\nneedle two\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("needle three\n", encoding="utf-8")

    data = tool_registry.invoke(
        "grep", {"path": str(tmp_path), "pattern": "needle", "limit": 2}
    )

    assert data["ok"] is True
    assert data["content"][0]["text"].count("needle") == 2
    assert data["content"][0]["text"].startswith(f"[{first}#")
    assert "1:needle one\n2:needle two" in data["content"][0]["text"]
    assert data["metadata"]["matches"] == 2
    assert data["metadata"]["files"] == 1
    assert data["metadata"]["tags"][str(first)]
    assert data["metadata"]["limit"] == 2
    assert data["metadata"]["truncated"] is True
    assert data["metadata"]["match_limit_reached"] is True


def test_zeta_tool_grep_reports_content_truncation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "long.txt"
    target.write_text("needle " + ("x" * 80) + "\n", encoding="utf-8")
    monkeypatch.setattr(grep_tool, "MAX_TOOL_RESULT_CHARS", 20)

    data = tool_registry.invoke("grep", {"path": str(target), "pattern": "needle"})

    assert data["ok"] is True
    assert len(data["content"][0]["text"]) == 20
    assert data["metadata"]["matches"] == 1
    assert data["metadata"]["files"] == 1
    assert data["metadata"]["truncated"] is True
    assert data["metadata"]["match_limit_reached"] is False
    assert data["metadata"]["content_truncated"] is True


def test_zeta_tool_grep_fallback_searches_without_ripgrep(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("needle two\n", encoding="utf-8")
    (tmp_path / "a.txt").write_text("needle one\n", encoding="utf-8")

    def missing_rg(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("rg")

    monkeypatch.setattr(grep_tool.subprocess, "Popen", missing_rg)

    data = tool_registry.invoke("grep", {"path": str(tmp_path), "pattern": "needle"})

    assert data["ok"] is True
    assert data["metadata"]["matches"] == 2
    lines = data["content"][0]["text"].splitlines()
    assert lines[0].startswith(f"[{tmp_path / 'a.txt'}#")
    assert lines[1] == "1:needle one"
    assert lines[2].startswith(f"[{tmp_path / 'sub' / 'b.txt'}#")
    assert lines[3] == "1:needle two"


def test_zeta_tool_grep_tag_can_ground_hashline_edit(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("keep\nneedle old\nkeep\n", encoding="utf-8")

    grep = tool_registry.invoke("grep", {"path": str(target), "pattern": "needle"})
    tag = grep["metadata"]["tags"][str(target)]
    data = tool_registry.invoke(
        "edit",
        {"input": f"[{target}#{tag}]\nSWAP 2..2:\n+needle new\n"},
    )

    assert data["ok"] is True
    assert target.read_text(encoding="utf-8") == "keep\nneedle new\nkeep\n"


@pytest.mark.skipif(shutil.which("sg") is None, reason="ast-grep is not installed")
def test_zeta_tool_ast_grep_returns_tagged_structural_matches(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    target.write_text(
        "import subprocess\n\n"
        "def run_it():\n"
        "    return subprocess.Popen(['echo', 'ok'])\n",
        encoding="utf-8",
    )

    data = tool_registry.invoke(
        "ast_grep",
        {
            "path": str(target),
            "lang": "python",
            "pattern": "subprocess.Popen($$$ARGS)",
        },
    )

    assert data["ok"] is True
    tag = data["metadata"]["tags"][str(target)]
    assert data["content"][0]["text"] == (
        f"[{target}#{tag}]\n4:    return subprocess.Popen(['echo', 'ok'])"
    )
    assert data["metadata"]["matches"] == 1
    assert data["metadata"]["files"] == 1


@pytest.mark.skipif(shutil.which("sg") is None, reason="ast-grep is not installed")
def test_zeta_tool_ast_grep_tag_can_ground_hashline_edit(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    target.write_text(
        "import subprocess\n\n"
        "def run_it():\n"
        "    return subprocess.Popen(['echo', 'ok'])\n",
        encoding="utf-8",
    )

    result = tool_registry.invoke(
        "ast_grep",
        {
            "path": str(target),
            "lang": "python",
            "pattern": "subprocess.Popen($$$ARGS)",
        },
    )
    tag = result["metadata"]["tags"][str(target)]
    data = tool_registry.invoke(
        "edit",
        {"input": f"[{target}#{tag}]\nSWAP 4..4:\n+    return 'ok'\n"},
    )

    assert data["ok"] is True
    assert "return 'ok'\n" in target.read_text(encoding="utf-8")


def test_zeta_tool_grep_fallback_stops_at_limit(tmp_path: Path, monkeypatch) -> None:
    for index in range(20):
        (tmp_path / f"file-{index:02}.txt").write_text("needle\n", encoding="utf-8")

    def missing_rg(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("rg")

    monkeypatch.setattr(grep_tool.subprocess, "Popen", missing_rg)

    data = tool_registry.invoke(
        "grep", {"path": str(tmp_path), "pattern": "needle", "limit": 3}
    )

    assert data["metadata"]["matches"] == 3
    assert data["metadata"]["truncated"] is True


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is not installed")
def test_zeta_tool_grep_reports_invalid_pattern_error(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("text\n", encoding="utf-8")

    data = tool_registry.invoke("grep", {"path": str(tmp_path), "pattern": "("})

    assert data["ok"] is False
    assert data["metadata"]["status"] not in {0, 1}
    assert data["content"][0]["text"]


def test_zeta_tool_bash_executes_command() -> None:
    data = tool_registry.invoke(
        "bash",
        {"command": "printf direct-bash"},
    )

    assert data["ok"] is True
    assert data["metadata"]["status"] == 0
    assert "stdout" not in data["metadata"]
    assert "stderr" not in data["metadata"]
    assert "direct-bash" in data["content"][0]["text"]


def test_zeta_tool_bash_normalizes_failure_error() -> None:
    data = tool_registry.invoke(
        "bash",
        {"command": "sh -c 'echo \"ValueError: bad input\" >&2; exit 1'"},
    )

    assert data["ok"] is False
    assert data["error"] == {
        "code": "bash-failed",
        "message": "ValueError: bad input",
    }
    assert data["metadata"]["status"] == 1


def test_zeta_tool_bash_replaces_invalid_utf8_output() -> None:
    data = tool_registry.invoke(
        "bash",
        {"command": "printf '\\377\\376'"},
    )

    assert data["ok"] is True
    assert "�" in data["content"][0]["text"]


def test_zeta_tool_bash_kills_command_on_timeout(monkeypatch) -> None:
    monkeypatch.setattr(bash_tool, "DEFAULT_TIMEOUT_SECONDS", 0.2)

    data = tool_registry.invoke(
        "bash",
        {"command": "sleep 5"},
    )

    assert data["ok"] is False
    assert data["error"]["code"] == "bash-timeout"
    assert data["metadata"]["timed_out"] is True
    assert "timed out" in data["content"][0]["text"]


def test_zeta_tool_bash_truncates_large_output() -> None:
    data = tool_registry.invoke(
        "bash",
        {"command": "head -c 100000 /dev/zero | tr '\\0' 'x'"},
    )

    assert data["ok"] is True
    assert data["metadata"]["stdout_truncated"] is True
    text = data["content"][0]["text"]
    assert len(text) < 2 * bash_tool.MAX_OUTPUT_CHARS
    assert "truncated" in text


def test_zeta_tool_write_writes_file(tmp_path: Path) -> None:
    target = tmp_path / "written.txt"

    data = tool_registry.invoke(
        "write",
        {"path": str(target), "content": "hello\n"},
    )

    assert data["ok"] is True
    metadata = data["metadata"]
    assert metadata["path"] == str(target)
    assert target.read_text(encoding="utf-8") == "hello\n"


def test_zeta_tool_ls_lists_directory_contents(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    data = tool_registry.invoke("ls", {"path": str(tmp_path)})

    assert data["ok"] is True
    assert data["content"][0]["text"].splitlines() == [
        "-\tdir\tsrc/",
        "10\tfile\tpyproject.toml",
    ]
    assert data["metadata"]["entries"] == 2


def test_zeta_tool_ls_can_filter_large_files_without_shelling_out(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "large-object").write_bytes(b"x" * 12)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "large.bin").write_bytes(b"x" * 12)
    (tmp_path / "small.txt").write_bytes(b"x" * 4)

    data = tool_registry.invoke(
        "ls",
        {
            "path": str(tmp_path),
            "recursive": True,
            "min_size_bytes": 10,
            "exclude": [".git"],
        },
    )

    assert data["ok"] is True
    assert data["content"][0]["text"].splitlines() == ["12\tfile\tsrc/large.bin"]
    assert data["metadata"]["entries"] == 1
    assert data["metadata"]["exclude"] == [".git"]


def test_zeta_tool_edit_writes_patch_artifact(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("old\n", encoding="utf-8")

    data = tool_registry.invoke(
        "edit", {"location": str(target), "old": "old\n", "new": "new\n"}
    )
    artifact = Path(data["metadata"]["artifact"])
    assert artifact.exists()
    patch = artifact.read_text(encoding="utf-8")
    assert "-old\n" in patch
    assert "+new\n" in patch


def test_zeta_tool_edit_accepts_exact_replacement(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("hello\nold\nbye\n", encoding="utf-8")
    payload = {
        "location": str(target),
        "old": "old\n",
        "new": "new\n",
    }

    data = tool_registry.invoke("edit", payload)

    artifact = Path(data["metadata"]["artifact"])
    patch = artifact.read_text(encoding="utf-8")
    assert "-old\n" in patch
    assert "+new\n" in patch


def test_zeta_tool_edit_applies_hashline_swap_from_read_tag(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("hello\nold\nbye\n", encoding="utf-8")
    read = tool_registry.invoke("read", {"path": str(target)})
    tag = read["metadata"]["tag"]

    data = tool_registry.invoke(
        "edit",
        {"input": f"[{target}#{tag}]\nSWAP 2..2:\n+new\n"},
    )

    assert data["ok"] is True
    patch = Path(data["metadata"]["artifact"]).read_text(encoding="utf-8")
    assert "-old\n" in patch
    assert "+new\n" in patch
    assert target.read_text(encoding="utf-8") == "hello\nnew\nbye\n"
    assert data["metadata"]["mode"] == "hashline"
    assert data["metadata"]["tag"] == tag


def test_zeta_tool_edit_applies_hashline_insert_and_delete(
    tmp_path: Path,
) -> None:
    target = tmp_path / "a.txt"
    target.write_text("one\nthree\nremove\n", encoding="utf-8")
    tag = tool_registry.invoke("read", {"path": str(target)})["metadata"]["tag"]

    data = tool_registry.invoke(
        "edit",
        {
            "input": (
                f"[{target}#{tag}]\n"
                "INS.POST 1:\n"
                "+two\n"
                "DEL 2..2\n"
                "INS.PRE 3:\n"
                "+inserted\n"
            )
        },
    )

    assert data["ok"] is True
    assert target.read_text(encoding="utf-8") == "one\ntwo\ninserted\nremove\n"
    assert data["metadata"]["mode"] == "hashline"


def test_zeta_tool_bash_honors_per_call_timeout() -> None:
    data = tool_registry.invoke(
        "bash",
        {"command": "sleep 5", "timeout": 1},
    )

    assert data["ok"] is False
    assert data["error"]["code"] == "bash-timeout"
    assert data["metadata"]["timed_out"] is True
    assert "1s" in data["error"]["message"]


def test_zeta_tool_edit_rejects_overlapping_operations(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    tag = tool_registry.invoke("read", {"path": str(target)})["metadata"]["tag"]

    data = tool_registry.invoke(
        "edit",
        {"input": f"[{target}#{tag}]\nSWAP 1..2:\n+x\nDEL 2..3\n"},
    )

    assert data["ok"] is False
    assert data["error"]["code"] == "overlapping-operations"
    assert target.read_text(encoding="utf-8") == "one\ntwo\nthree\nfour\n"


def test_zeta_tool_edit_rejects_stale_hashline_tag(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("old\n", encoding="utf-8")
    tag = tool_registry.invoke("read", {"path": str(target)})["metadata"]["tag"]
    target.write_text("changed\n", encoding="utf-8")

    data = tool_registry.invoke(
        "edit", {"input": f"[{target}#{tag}]\nSWAP 1..1:\n+new\n"}
    )

    assert data["ok"] is False
    assert data["error"]["code"] == "stale-tag"
    assert target.read_text(encoding="utf-8") == "changed\n"


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ("a.txt\nSWAP 1..1:\n+new\n", "missing-section-header"),
        ("[a.txt]\nSWAP 1..1:\n+new\n", "missing-tag"),
        ("[a.txt#abcd]\nMOVE 1..1:\n+new\n", "unknown-operation"),
        ("[a.txt#abcd]\nSWAP 1..1:\n-new\n", "invalid-body-line"),
        ("[a.txt#abcd]\nSWAP 4..4:\n+new\n", "line-out-of-range"),
    ],
)
def test_zeta_tool_edit_rejects_malformed_hashline_input(
    tmp_path: Path, payload: str, code: str
) -> None:
    target = tmp_path / "a.txt"
    target.write_text("old\n", encoding="utf-8")
    tag = tool_registry.invoke("read", {"path": str(target)})["metadata"]["tag"]
    if "a.txt#abcd" in payload:
        payload = payload.replace("a.txt#abcd", f"{target}#{tag}")
    else:
        payload = payload.replace("a.txt", str(target))

    data = tool_registry.invoke("edit", {"input": payload})

    assert data["ok"] is False
    assert data["error"]["code"] == code


def test_zeta_tool_edit_rejects_hashline_noop(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("old\n", encoding="utf-8")
    tag = tool_registry.invoke("read", {"path": str(target)})["metadata"]["tag"]

    data = tool_registry.invoke(
        "edit", {"input": f"[{target}#{tag}]\nSWAP 1..1:\n+old\n"}
    )

    assert data["ok"] is False
    assert data["error"]["code"] == "empty-edit"


def test_zeta_tool_edit_exact_replace_writes_file(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("hello\nold\nbye\n", encoding="utf-8")

    data = tool_registry.invoke(
        "edit",
        {"location": str(target), "old": "old\n", "new": "new\n"},
    )

    assert data["ok"] is True
    assert target.read_text(encoding="utf-8") == "hello\nnew\nbye\n"
    assert "handoff" not in data
    metadata = data["metadata"]
    assert metadata["operation"] == "exact_replace"
    artifact = Path(metadata["artifact"])
    assert artifact.exists()
    assert "+new\n" in artifact.read_text(encoding="utf-8")


def test_zeta_tool_edit_rejects_non_utf8_file(tmp_path: Path) -> None:
    target = tmp_path / "latin1.txt"
    target.write_bytes(b"caf\xe9 old\n")

    data = tool_registry.invoke(
        "edit",
        {"location": str(target), "old": "old", "new": "new"},
    )

    assert data["ok"] is False
    assert data["error"]["code"] == "not-utf8"
    assert target.read_bytes() == b"caf\xe9 old\n"


def test_zeta_tool_edit_reports_write_failure(tmp_path: Path) -> None:
    target = tmp_path / "readonly.txt"
    target.write_text("old\n", encoding="utf-8")
    target.chmod(0o444)

    data = tool_registry.invoke(
        "edit",
        {"location": str(target), "old": "old\n", "new": "new\n"},
    )

    target.chmod(0o644)
    assert data["ok"] is False
    assert data["error"]["code"] == "write-failed"
    assert target.read_text(encoding="utf-8") == "old\n"


def test_zeta_tool_edit_rejects_ambiguous_exact_replacement(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("old\nold\n", encoding="utf-8")

    data = tool_registry.invoke(
        "edit", {"location": str(target), "old": "old\n", "new": "new\n"}
    )

    assert data["ok"] is False
    assert data["error"]["code"] == "old-text-not-unique"


def test_zeta_tool_edit_marks_no_newline_exact_replacement(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("old", encoding="utf-8")

    data = tool_registry.invoke(
        "edit", {"location": str(target), "old": "old", "new": "new"}
    )

    artifact = Path(data["metadata"]["artifact"])
    patch = artifact.read_text(encoding="utf-8")
    assert "-old\n\\ No newline at end of file\n" in patch
    assert "+new\n\\ No newline at end of file\n" in patch


def test_zeta_builtin_metadata_declares_model_shape() -> None:
    assert tool_metadata("bash")["name"] == "bash"
    assert tool_metadata("read")["name"] == "read"
    assert tool_metadata("edit")["name"] == "edit"


def query_log_event(
    event_id: str,
    event_type: str,
    *,
    run_id: str,
    timestamp_ms: int,
    session_id: str = "session-a",
    payload: dict[str, Any] | None = None,
) -> Event:
    return Event(
        id=event_id,
        event_type=event_type,
        source="zeta",
        payload=payload or {},
        idempotency_key=None,
        caused_by=None,
        session_id=session_id,
        run_id=run_id,
        timestamp_ms=timestamp_ms,
    )


def seed_query_log_runs() -> MemoryEventStore:
    store = MemoryEventStore()
    events = [
        query_log_event(
            "evt-old-user",
            "zeta.user_message",
            run_id="run-old-1111",
            timestamp_ms=1_000,
            payload={"content": "fix the parser"},
        ),
        query_log_event(
            "evt-old-model",
            "zeta.model_call.completed",
            run_id="run-old-1111",
            timestamp_ms=2_000,
            payload={
                "_timeline_type": "model",
                "content": "parser fixed",
                "prompt_object_id": "sha256:prompt",
            },
        ),
        query_log_event(
            "evt-old-tool-started",
            "zeta.tool_call.started",
            run_id="run-old-1111",
            timestamp_ms=2_100,
            payload={
                "_timeline_type": "tool_call",
                "tool_call_id": "call-edit",
                "name": "edit",
                "input": {"location": "parser.py"},
            },
        ),
        query_log_event(
            "evt-old-tool",
            "zeta.tool_call.completed",
            run_id="run-old-1111",
            timestamp_ms=2_200,
            payload={
                "_timeline_type": "tool_result",
                "tool_call_id": "call-edit",
                "name": "edit",
                "result": {
                    "ok": True,
                    "content": [{"type": "text", "text": "updated parser.py"}],
                },
                "model_telemetry": {"usage": {"input_tokens": 12, "output_tokens": 3}},
            },
        ),
        query_log_event(
            "evt-old-done",
            "runtime.queue_item.completed",
            run_id="run-old-1111",
            timestamp_ms=3_000,
            payload={
                "target_agent": "zeta.session.turn",
                "result": {
                    "outcome": "completed",
                    "final_answer": "parser fixed",
                },
            },
        ),
        query_log_event(
            "evt-failed-user",
            "zeta.user_message",
            run_id="run-failed-2222",
            timestamp_ms=4_000,
            payload={"content": "deploy it"},
        ),
        query_log_event(
            "evt-failed-tool-started",
            "zeta.tool_call.started",
            run_id="run-failed-2222",
            timestamp_ms=4_500,
            payload={
                "_timeline_type": "tool_call",
                "tool_call_id": "call-bash",
                "name": "bash",
                "input": {"command": "uv run pytest"},
            },
        ),
        query_log_event(
            "evt-failed-tool",
            "zeta.tool_call.failed",
            run_id="run-failed-2222",
            timestamp_ms=4_750,
            payload={
                "_timeline_type": "tool_result",
                "tool_call_id": "call-bash",
                "name": "bash",
                "result": {
                    "ok": False,
                    "error": {
                        "code": "timeout",
                        "message": "deadline exceeded",
                    },
                },
            },
        ),
        query_log_event(
            "evt-failed",
            "zeta.turn.failed",
            run_id="run-failed-2222",
            timestamp_ms=5_000,
            payload={"reason": "deadline exceeded"},
        ),
        query_log_event(
            "evt-other",
            "zeta.user_message",
            run_id="run-secret-3333",
            timestamp_ms=6_000,
            session_id="session-b",
            payload={"content": "other session secret"},
        ),
        query_log_event(
            "evt-current",
            "zeta.user_message",
            run_id="run-current-4444",
            timestamp_ms=7_000,
            payload={"content": "inspect the log"},
        ),
    ]
    for event in events:
        store.append(event)
    return store


def test_zeta_query_log_metadata_declares_session_scoped_run_history() -> None:
    metadata = tool_metadata("query_log")
    schema = metadata["input_schema"]

    assert metadata["id"] == "zeta.query_log"
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"since", "failed", "run_id", "limit"}
    assert schema["properties"]["limit"]["maximum"] == 50
    assert "session" in metadata["description"]


def test_zeta_query_log_lists_only_prior_runs_in_the_bound_session() -> None:
    result = query_run_log(
        {},
        event_reader=seed_query_log_runs(),
        session_id="session-a",
        current_run_id="run-current-4444",
    )

    assert result["ok"] is True
    text = result["content"][0]["text"]
    assert text.index("run-failed-2222") < text.index("run-old-1111")
    assert "run-current-4444" not in text
    assert "run-secret-3333" not in text
    assert "other session secret" not in text
    assert result["metadata"] == {
        "runs": 2,
        "run_ids": ["run-failed-2222", "run-old-1111"],
        "session_id": "session-a",
        "limit": 20,
    }


def test_zeta_query_log_filters_failed_since_and_caps_limit() -> None:
    store = seed_query_log_runs()

    failed = query_run_log(
        {"failed": True},
        event_reader=store,
        session_id="session-a",
        current_run_id="run-current-4444",
    )
    recent = query_run_log(
        {"since": "1m"},
        event_reader=store,
        session_id="session-a",
        current_run_id="run-current-4444",
        now=datetime.fromtimestamp(64, tz=UTC),
    )
    dated = query_run_log(
        {"since": "1970-01-01"},
        event_reader=store,
        session_id="session-a",
        current_run_id="run-current-4444",
    )
    capped = query_run_log(
        {"limit": 500},
        event_reader=store,
        session_id="session-a",
        current_run_id="run-current-4444",
    )

    assert failed["metadata"]["run_ids"] == ["run-failed-2222"]
    assert recent["metadata"]["run_ids"] == ["run-failed-2222"]
    assert dated["metadata"]["run_ids"] == ["run-failed-2222", "run-old-1111"]
    assert capped["metadata"]["limit"] == 50


def test_zeta_query_log_expands_one_run_by_prefix() -> None:
    result = query_run_log(
        {"run_id": "run-old"},
        event_reader=seed_query_log_runs(),
        session_id="session-a",
        current_run_id="run-current-4444",
    )

    assert result["ok"] is True
    text = result["content"][0]["text"]
    assert "run      run-old-1111" in text
    assert "objective fix the parser" in text
    assert "outcome  completed" in text
    assert "usage" not in text
    assert "edit: ok · parser.py" in text
    assert "updated parser.py" not in text
    assert "answer   parser fixed" in text
    assert "sha256:prompt" in text
    assert result["metadata"]["run_id"] == "run-old-1111"


def test_zeta_query_log_expands_compact_tool_failure_details() -> None:
    result = query_run_log(
        {"run_id": "run-failed"},
        event_reader=seed_query_log_runs(),
        session_id="session-a",
        current_run_id="run-current-4444",
    )

    text = result["content"][0]["text"]
    assert "bash: failed · uv run pytest · timeout: deadline exceeded" in text


def test_zeta_query_log_rejects_bad_since_and_scoped_run_ids() -> None:
    store = seed_query_log_runs()
    store.append(
        query_log_event(
            "evt-failed-also",
            "zeta.user_message",
            run_id="run-failed-9999",
            timestamp_ms=5_500,
            payload={"content": "fail differently"},
        )
    )
    store.append(
        query_log_event(
            "evt-exact",
            "zeta.user_message",
            run_id="run-exact",
            timestamp_ms=5_600,
            payload={"content": "the exact run"},
        )
    )
    store.append(
        query_log_event(
            "evt-exact-longer",
            "zeta.user_message",
            run_id="run-exact-longer",
            timestamp_ms=5_700,
            payload={"content": "a longer run id"},
        )
    )

    ambiguous = query_run_log(
        {"run_id": "run-failed"},
        event_reader=store,
        session_id="session-a",
        current_run_id="run-current-4444",
    )
    unknown = query_run_log(
        {"run_id": "run-secret"},
        event_reader=store,
        session_id="session-a",
        current_run_id="run-current-4444",
    )
    invalid_since = query_run_log(
        {"since": "yesterday-ish"},
        event_reader=store,
        session_id="session-a",
        current_run_id="run-current-4444",
    )
    overflowing_since = query_run_log(
        {"since": "999999999999999999999d"},
        event_reader=store,
        session_id="session-a",
        current_run_id="run-current-4444",
    )
    exact = query_run_log(
        {"run_id": "run-exact"},
        event_reader=store,
        session_id="session-a",
        current_run_id="run-current-4444",
    )

    assert ambiguous["error"]["code"] == "ambiguous-run-id"
    assert unknown["error"] == {
        "code": "unknown-run-id",
        "message": "no run matches 'run-secret'",
    }
    assert invalid_since["error"]["code"] == "invalid-since"
    assert overflowing_since["error"]["code"] == "invalid-since"
    assert exact["metadata"]["run_id"] == "run-exact"


def test_zeta_query_log_handles_empty_history_and_omits_large_tool_results() -> None:
    empty = query_run_log(
        {},
        event_reader=MemoryEventStore(),
        session_id="session-a",
        current_run_id="run-current-4444",
    )
    store = seed_query_log_runs()
    store.append(
        query_log_event(
            "evt-large",
            "zeta.tool_call.completed",
            run_id="run-old-1111",
            timestamp_ms=2_500,
            payload={
                "_timeline_type": "tool_result",
                "tool_call_id": "call-read",
                "name": "read",
                "result": {
                    "ok": True,
                    "content": [
                        {
                            "type": "text",
                            "text": "x" * (MAX_QUERY_LOG_OUTPUT_CHARS * 2),
                        }
                    ],
                },
            },
        )
    )
    store.append(
        query_log_event(
            "evt-large-answer",
            "runtime.queue_item.completed",
            run_id="run-old-1111",
            timestamp_ms=2_750,
            payload={
                "target_agent": "zeta.session.turn",
                "result": {
                    "outcome": "completed",
                    "final_answer": "y" * (MAX_QUERY_LOG_OUTPUT_CHARS * 2),
                },
            },
        )
    )
    expanded = query_run_log(
        {"run_id": "run-old-1111"},
        event_reader=store,
        session_id="session-a",
        current_run_id="run-current-4444",
    )

    assert empty["ok"] is True
    assert empty["content"][0]["text"] == "no prior runs recorded"
    assert empty["metadata"]["runs"] == 0
    assert len(expanded["content"][0]["text"]) <= MAX_QUERY_LOG_OUTPUT_CHARS
    assert expanded["content"][0]["text"].endswith("…")
    assert "x" * 100 not in expanded["content"][0]["text"]


def test_zeta_query_log_bounds_the_newest_event_scan() -> None:
    captured_filters: list[Any] = []
    store = seed_query_log_runs()

    class RecordingReader:
        def list_events(self, filter: Any) -> list[Event]:
            captured_filters.append(filter)
            return store.list_events(filter)

    result = query_run_log(
        {},
        event_reader=RecordingReader(),
        session_id="session-a",
        current_run_id="run-current-4444",
    )

    assert result["ok"] is True
    assert len(captured_filters) == 1
    assert captured_filters[0].newest_first is True
    assert captured_filters[0].limit == MAX_QUERY_LOG_EVENTS


def test_zeta_query_log_expands_authored_agent_terminal_results() -> None:
    store = MemoryEventStore()
    store.append(
        query_log_event(
            "evt-authored-user",
            "zeta.user_message",
            run_id="run-authored-1111",
            timestamp_ms=1_000,
            payload={"content": "review the release"},
        )
    )
    store.append(
        query_log_event(
            "evt-authored-terminal",
            "runtime.queue_item.completed",
            run_id="run-authored-1111",
            timestamp_ms=2_000,
            payload={
                "target_agent": "agent:release-reviewer",
                "result": {
                    "outcome": "stopped",
                    "final_answer": "release needs changes",
                },
            },
        )
    )

    result = query_run_log(
        {"run_id": "run-authored"},
        event_reader=store,
        session_id="session-a",
        current_run_id="run-current-4444",
    )

    text = result["content"][0]["text"]
    assert "outcome  stopped" in text
    assert "answer   release needs changes" in text


def test_zeta_tool_bash_records_duration() -> None:
    data = tool_registry.invoke(
        "bash",
        {"command": "printf timed"},
    )

    duration = data["metadata"]["duration_ms"]
    assert isinstance(duration, int)
    assert duration >= 0


def test_zeta_tool_write_records_content_hashes(tmp_path: Path) -> None:
    target = tmp_path / "written.txt"
    target.write_text("old\n", encoding="utf-8")

    data = tool_registry.invoke(
        "write",
        {"path": str(target), "content": "hello\n"},
    )

    metadata = data["metadata"]
    assert metadata["before_hash"] == "sha256:" + hashlib.sha256(b"old\n").hexdigest()
    assert metadata["after_hash"] == "sha256:" + hashlib.sha256(b"hello\n").hexdigest()


def test_zeta_tool_write_omits_before_hash_for_new_file(tmp_path: Path) -> None:
    target = tmp_path / "fresh.txt"

    data = tool_registry.invoke(
        "write",
        {"path": str(target), "content": "hello\n"},
    )

    metadata = data["metadata"]
    assert "before_hash" not in metadata
    assert metadata["after_hash"] == "sha256:" + hashlib.sha256(b"hello\n").hexdigest()


def test_zeta_tool_edit_records_content_hashes(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("hello\nold\nbye\n", encoding="utf-8")

    data = tool_registry.invoke(
        "edit",
        {"location": str(target), "old": "old\n", "new": "new\n"},
    )

    metadata = data["metadata"]
    before = "sha256:" + hashlib.sha256(b"hello\nold\nbye\n").hexdigest()
    after = "sha256:" + hashlib.sha256(b"hello\nnew\nbye\n").hexdigest()
    assert metadata["before_hash"] == before
    assert metadata["after_hash"] == after


def test_registered_capabilities_expands_only_scoped_mcp_wildcards() -> None:
    registry = CapabilityRegistry()
    registry.register(_test_capability("linear.search_issues", provider="mcp"))
    registry.register(_test_capability("linear.get_issue", provider="mcp"))
    registry.register(_test_capability("google_calendar.list_events", provider="mcp"))

    assert registered_capabilities(
        ("mcp.linear.*", "mcp.google_calendar.list_events"),
        tool_registry=registry,
    ) == (
        "mcp.linear.get_issue",
        "mcp.linear.search_issues",
        "mcp.google_calendar.list_events",
    )
    assert registered_capabilities(("mcp.*",), tool_registry=registry) == ()
