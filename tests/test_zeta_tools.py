"""Builtin tool tests."""

from __future__ import annotations

import ast
import hashlib
import shutil
from pathlib import Path
from typing import Any

import pytest

from sigil.tools import bash as bash_tool
from sigil.tools import ensure_builtin_tools_registered, register_builtin_tools
from sigil.tools import grep as grep_tool
from sigil.tools import read as read_tool
from sigil.tools import web as web_tool
from zeta.tools.base import (
    Capability,
    CapabilityId,
    CapabilityPolicy,
    CapabilitySpec,
    EffectKind,
    InProcessCapabilityExecutor,
    TrustLevel,
)
from zeta.tools.registry import CapabilityRegistry
from zeta.tools.registry import registry as tool_registry

ensure_builtin_tools_registered()


def tool_metadata(name: str) -> dict[str, Any]:
    capability = tool_registry.get_by_alias(name)
    assert capability is not None
    metadata = capability.spec.metadata()
    metadata["supports_staging"] = capability.policy.supports_staging
    metadata["supports_direct"] = capability.policy.supports_direct
    metadata["timeout_seconds"] = capability.policy.timeout_seconds
    return metadata


def _test_capability(
    name: str,
    *,
    provider: str = "test",
    schema: dict[str, Any] | None = None,
    effects: tuple[EffectKind, ...] = (),
    aliases: tuple[str, ...] | None = None,
    run_result: dict[str, Any] | None = None,
    supports_staging: bool = False,
    supports_direct: bool = True,
    trust: TrustLevel = "host",
) -> Capability:
    return Capability(
        CapabilitySpec(
            CapabilityId(provider, name),
            "Unit test capability.",
            schema or {"type": "object"},
            effects=effects,
            aliases=aliases or (name,),
        ),
        CapabilityPolicy(
            supports_staging=supports_staging,
            supports_direct=supports_direct,
            trust=trust,
        ),
        InProcessCapabilityExecutor(
            lambda params: run_result or {"ok": True, "metadata": params},
            (lambda params: {"ok": True, "effect": {"status": "proposed"}})
            if supports_staging
            else None,
        ),
    )


def test_zeta_tool_registry_registers_and_lists_tools() -> None:
    capability = _test_capability("unit", effects=("read",))
    registry = CapabilityRegistry()

    registry.register(capability)

    assert registry.get("test.unit") is capability
    assert registry.list_capability_ids() == ["test.unit"]
    assert capability.spec.metadata()["id"] == "test.unit"


def test_zeta_tool_registry_rejects_invalid_tool_schema() -> None:
    registry = CapabilityRegistry()
    capability = _test_capability(
        "bad",
        schema={"type": "definitely-not-json-schema"},
    )

    with pytest.raises(ValueError, match="invalid schema for capability 'test.bad'"):
        registry.register(capability)


def test_zeta_tool_registry_refuses_undeclared_effects_in_stage_mode() -> None:
    registry = CapabilityRegistry()
    registry.register(_test_capability("unit"))

    result = registry.invoke("unit", {})

    assert result["ok"] is False
    assert result["error"]["code"] == "staging-unsupported"
    assert "undeclared" in result["error"]["message"]


def test_zeta_tool_registry_requires_direct_execution_permission() -> None:
    registry = CapabilityRegistry()
    registry.register(
        _test_capability(
            "unit",
            effects=("write",),
            supports_staging=True,
            supports_direct=False,
        )
    )

    result = registry.invoke("unit", {}, execution_mode="direct")

    assert result == {
        "ok": False,
        "error": {
            "code": "direct-execution-disallowed",
            "message": "capability test.unit does not allow direct execution",
        },
    }


def test_zeta_tool_registry_normalizes_malformed_executor_result() -> None:
    registry = CapabilityRegistry()
    registry.register(
        _test_capability(
            "unit",
            effects=("read",),
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


def test_zeta_tool_registry_converts_executor_exception_to_error_result() -> None:
    registry = CapabilityRegistry()

    def crash(params: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("boom")

    registry.register(
        Capability(
            CapabilitySpec(
                CapabilityId("test", "crash"),
                "Crash test capability.",
                {"type": "object"},
                effects=("read",),
                aliases=("crash",),
            ),
            CapabilityPolicy(
                supports_staging=False,
                supports_direct=True,
                trust="host",
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


def test_zeta_tool_registry_rejects_low_trust_mutating_direct_execution() -> None:
    registry = CapabilityRegistry()
    registry.register(
        _test_capability(
            "write",
            effects=("write",),
            run_result={"ok": True, "content": [{"type": "text", "text": "wrote"}]},
            supports_staging=True,
            supports_direct=True,
            trust="client",
        )
    )

    result = registry.invoke("write", {}, execution_mode="direct")

    assert result == {
        "ok": False,
        "error": {
            "code": "trust-direct-disallowed",
            "message": "capability test.write with client trust cannot run mutating effects directly",
        },
    }


def test_zeta_tool_registry_allows_low_trust_mutating_stage_execution() -> None:
    registry = CapabilityRegistry()
    registry.register(
        _test_capability(
            "write",
            effects=("write",),
            supports_staging=True,
            supports_direct=True,
            trust="client",
        )
    )

    result = registry.invoke("write", {}, execution_mode="stage")

    assert result == {"ok": True, "effect": {"status": "proposed"}}


def test_zeta_in_process_capability_executor_runs_read_capability() -> None:
    capability = _test_capability("read", effects=("read",))

    result = capability.executor.invoke(
        capability.spec,
        {"path": "README.md"},
        mode="stage",
    )

    assert result.payload == {"ok": True, "metadata": {"path": "README.md"}}


def test_zeta_in_process_capability_executor_stages_mutating_capability() -> None:
    capability = _test_capability(
        "write",
        effects=("write",),
        supports_staging=True,
    )

    result = capability.executor.invoke(
        capability.spec,
        {"path": "README.md"},
        mode="stage",
    )

    assert result.payload == {"ok": True, "effect": {"status": "proposed"}}


def test_zeta_capability_registry_rejects_duplicate_canonical_ids() -> None:
    registry = CapabilityRegistry()
    registry.register(_test_capability("read"))

    with pytest.raises(
        ValueError, match="capability 'test.read' is already registered"
    ):
        registry.register(_test_capability("read"))


def test_zeta_capability_projection_rejects_ambiguous_aliases() -> None:
    registry = CapabilityRegistry()
    registry.register(_test_capability("read", provider="host", aliases=("read",)))
    registry.register(_test_capability("read", provider="rpc", aliases=("read",)))

    with pytest.raises(ValueError, match="ambiguous capability alias 'read'"):
        registry.project(("host.read", "rpc.read"))


def test_zeta_capability_projection_can_use_qualified_aliases() -> None:
    registry = CapabilityRegistry()
    registry.register(_test_capability("read", provider="host", aliases=("read",)))
    registry.register(_test_capability("read", provider="rpc", aliases=("read",)))

    projection = registry.project(
        ("host.read", "rpc.read"),
        alias_overrides={
            "host.read": "host.read",
            "rpc.read": "rpc.read",
        },
    )

    assert projection.alias_to_id == {
        "host.read": "host.read",
        "rpc.read": "rpc.read",
    }
    assert [
        descriptor["function"]["name"] for descriptor in projection.descriptors
    ] == ["host.read", "rpc.read"]


def test_zeta_tool_registry_starts_empty() -> None:
    registry = CapabilityRegistry()

    assert registry.list_capability_ids() == []


def test_sigil_registers_builtin_tools_explicitly() -> None:
    registry = CapabilityRegistry()

    register_builtin_tools(registry)

    assert {
        "sigil.ast_grep",
        "sigil.read",
        "sigil.grep",
        "sigil.ls",
        "sigil.bash",
        "sigil.edit",
        "sigil.write",
        "sigil.query_log",
        "sigil.web_search",
    } <= set(registry.list_capability_ids())
    assert "sigil.web_fetch" not in set(registry.list_capability_ids())


def test_sigil_ensures_shared_zeta_registry_has_builtins() -> None:
    ensure_builtin_tools_registered()

    names = set(tool_registry.list_capability_ids())
    assert {
        "sigil.read",
        "sigil.grep",
        "sigil.ast_grep",
        "sigil.ls",
        "sigil.bash",
        "sigil.edit",
        "sigil.write",
        "sigil.web_search",
    } <= names
    assert "sigil.web_fetch" not in names


def test_zeta_tool_registry_does_not_import_sigil_tools() -> None:
    source = Path("src/zeta/tools/registry.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.append(node.module)
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)

    assert all(not module.startswith("sigil.tools") for module in imports)


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

    assert metadata["effects"] == ["search"]
    assert metadata["description"] == (
        "Search code structurally with ast-grep. Use when looking for syntax "
        "patterns rather than plain text. Results include [path#tag] snapshot "
        "headers and numbered matched lines for grounded edits."
    )
    assert schema["required"] == ["pattern", "lang"]
    assert schema["properties"]["pattern"]["description"].startswith(
        "ast-grep structural pattern"
    )


def test_sigil_web_search_schema_matches_codex_contract() -> None:
    metadata = web_tool.SEARCH_SPEC.metadata()
    schema = metadata["input_schema"]

    assert metadata["id"] == "sigil.web_search"
    assert metadata["aliases"] == ["web_search"]
    assert metadata["effects"] == ["search"]
    assert schema["required"] == ["query"]
    assert schema["properties"]["query"]["type"] == "string"
    assert schema["properties"]["limit"]["minimum"] == 1


def test_sigil_web_search_reports_missing_codex_credentials(monkeypatch) -> None:
    def missing_credentials() -> web_tool.CodexCredentials:
        raise RuntimeError("no Codex credentials at ~/.codex/auth.json")

    monkeypatch.setattr(web_tool, "load_codex_credentials", missing_credentials)

    result = web_tool.search({"query": "parallel api docs"})

    assert result == {
        "ok": False,
        "error": {
            "code": "codex-auth-missing",
            "message": "no Codex credentials at ~/.codex/auth.json",
        },
    }


def test_sigil_web_search_posts_codex_payload(monkeypatch) -> None:
    calls: list[tuple[str, web_tool.WebConfig]] = []

    monkeypatch.setenv("SIGIL_WEB_SEARCH_MODEL", "gpt-test")
    monkeypatch.setattr(
        web_tool,
        "load_codex_credentials",
        lambda: web_tool.CodexCredentials(access_token="tok-1", account_id="acct-1"),
    )

    def fake_request(query: str, config: web_tool.WebConfig) -> web_tool.CodexSearch:
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

    result = web_tool.search({"query": "parallel search api", "limit": 5})

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


def test_sigil_read_fetches_public_url(monkeypatch) -> None:
    class FakeResponse:
        headers = {"content-type": "text/html"}

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"<html><head><title>Example</title></head><body><h1>Hello</h1><p>World</p></body></html>"

    requests: list[Any] = []

    def fake_urlopen(request: Any, timeout: float) -> FakeResponse:
        requests.append((request, timeout))
        return FakeResponse()

    monkeypatch.setattr(read_tool.urllib.request, "urlopen", fake_urlopen)

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
        execution_mode="direct",
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
        execution_mode="direct",
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


def test_zeta_tool_bash_returns_proposed_command_effect() -> None:
    data = tool_registry.invoke(
        "bash", {"command": "uv run pytest", "reason": "Run tests."}
    )

    assert "handoff" not in data
    assert data["effect"] == {
        "kind": "command",
        "status": "proposed",
        "command": "uv run pytest",
        "reason": "Run tests.",
    }


def test_zeta_tool_bash_direct_executes_command() -> None:
    data = tool_registry.invoke(
        "bash",
        {"command": "printf direct-bash"},
        execution_mode="direct",
    )

    assert data["ok"] is True
    assert data["metadata"]["mode"] == "direct"
    assert data["metadata"]["status"] == 0
    assert "stdout" not in data["metadata"]
    assert "stderr" not in data["metadata"]
    assert "direct-bash" in data["content"][0]["text"]


def test_zeta_tool_bash_direct_replaces_invalid_utf8_output() -> None:
    data = tool_registry.invoke(
        "bash",
        {"command": "printf '\\377\\376'"},
        execution_mode="direct",
    )

    assert data["ok"] is True
    assert "�" in data["content"][0]["text"]


def test_zeta_tool_bash_direct_kills_command_on_timeout(monkeypatch) -> None:
    monkeypatch.setattr(bash_tool, "DEFAULT_TIMEOUT_SECONDS", 0.2)

    data = tool_registry.invoke(
        "bash",
        {"command": "sleep 5"},
        execution_mode="direct",
    )

    assert data["ok"] is False
    assert data["error"]["code"] == "bash-timeout"
    assert data["metadata"]["timed_out"] is True
    assert "timed out" in data["content"][0]["text"]


def test_zeta_tool_bash_direct_truncates_large_output() -> None:
    data = tool_registry.invoke(
        "bash",
        {"command": "head -c 100000 /dev/zero | tr '\\0' 'x'"},
        execution_mode="direct",
    )

    assert data["ok"] is True
    assert data["metadata"]["stdout_truncated"] is True
    text = data["content"][0]["text"]
    assert len(text) < 2 * bash_tool.MAX_OUTPUT_CHARS
    assert "truncated" in text


def test_zeta_tool_write_direct_writes_file(tmp_path: Path) -> None:
    target = tmp_path / "direct.txt"

    data = tool_registry.invoke(
        "write",
        {"path": str(target), "content": "hello\n"},
        execution_mode="direct",
    )

    assert data["ok"] is True
    metadata = data["metadata"]
    assert metadata["mode"] == "direct"
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
    artifact = Path(data["effect"]["artifact"])
    assert artifact.exists()
    patch = artifact.read_text(encoding="utf-8")
    assert "-old\n" in patch
    assert "+new\n" in patch
    assert data["effect"]["command"].startswith("git apply ")


def test_zeta_tool_edit_accepts_exact_replacement(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("hello\nold\nbye\n", encoding="utf-8")
    payload = {
        "location": str(target),
        "old": "old\n",
        "new": "new\n",
        "reason": "Replace one line.",
    }

    data = tool_registry.invoke("edit", payload)

    assert tool_registry.validate_capability_args("edit", payload) == []
    artifact = Path(data["effect"]["artifact"])
    patch = artifact.read_text(encoding="utf-8")
    assert data["effect"]["command"].startswith("git apply ")
    assert data["effect"]["reason"] == "Replace one line."
    assert "-old\n" in patch
    assert "+new\n" in patch


def test_zeta_tool_edit_stages_hashline_swap_from_read_tag(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("hello\nold\nbye\n", encoding="utf-8")
    read = tool_registry.invoke("read", {"path": str(target)})
    tag = read["metadata"]["tag"]

    data = tool_registry.invoke(
        "edit",
        {"input": f"[{target}#{tag}]\nSWAP 2..2:\n+new\n", "reason": "Use tag."},
    )

    assert data["ok"] is True
    assert data["effect"]["command"].startswith("git apply ")
    assert data["effect"]["reason"] == "Use tag."
    patch = Path(data["effect"]["artifact"]).read_text(encoding="utf-8")
    assert "-old\n" in patch
    assert "+new\n" in patch
    assert target.read_text(encoding="utf-8") == "hello\nold\nbye\n"
    assert data["metadata"]["mode"] == "hashline"
    assert data["metadata"]["tag"] == tag


def test_zeta_tool_edit_direct_applies_hashline_insert_and_delete(
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
        execution_mode="direct",
    )

    assert data["ok"] is True
    assert target.read_text(encoding="utf-8") == "one\ntwo\ninserted\nremove\n"
    assert data["metadata"]["mode"] == "hashline"


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


def test_zeta_tool_edit_direct_replace_writes_file(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("hello\nold\nbye\n", encoding="utf-8")

    data = tool_registry.invoke(
        "edit",
        {"location": str(target), "old": "old\n", "new": "new\n"},
        execution_mode="direct",
    )

    assert data["ok"] is True
    assert target.read_text(encoding="utf-8") == "hello\nnew\nbye\n"
    assert "handoff" not in data
    metadata = data["metadata"]
    assert metadata["mode"] == "direct_replace"
    artifact = Path(metadata["artifact"])
    assert artifact.exists()
    assert "+new\n" in artifact.read_text(encoding="utf-8")


def test_zeta_tool_edit_rejects_non_utf8_file(tmp_path: Path) -> None:
    target = tmp_path / "latin1.txt"
    target.write_bytes(b"caf\xe9 old\n")

    data = tool_registry.invoke(
        "edit",
        {"location": str(target), "old": "old", "new": "new"},
        execution_mode="direct",
    )

    assert data["ok"] is False
    assert data["error"]["code"] == "not-utf8"
    assert target.read_bytes() == b"caf\xe9 old\n"


def test_zeta_tool_edit_direct_reports_write_failure(tmp_path: Path) -> None:
    target = tmp_path / "readonly.txt"
    target.write_text("old\n", encoding="utf-8")
    target.chmod(0o444)

    data = tool_registry.invoke(
        "edit",
        {"location": str(target), "old": "old\n", "new": "new\n"},
        execution_mode="direct",
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

    artifact = Path(data["effect"]["artifact"])
    patch = artifact.read_text(encoding="utf-8")
    assert "-old\n\\ No newline at end of file\n" in patch
    assert "+new\n\\ No newline at end of file\n" in patch


def test_zeta_builtin_metadata_declares_effects() -> None:
    assert tool_metadata("bash")["effects"] == ["execute"]
    assert tool_metadata("write")["effects"] == ["write"]
    assert tool_metadata("edit")["effects"] == ["write"]
    assert tool_metadata("read")["effects"] == ["read"]
    assert tool_metadata("grep")["effects"] == ["search"]
    assert tool_metadata("ast_grep")["effects"] == ["search"]
    assert tool_metadata("ls")["effects"] == ["read"]


def test_zeta_builtin_metadata_declares_execution_capabilities() -> None:
    assert tool_metadata("bash")["supports_staging"] is True
    assert tool_metadata("bash")["supports_direct"] is True
    assert tool_metadata("bash")["timeout_seconds"] == 120.0
    assert tool_metadata("write")["supports_staging"] is True
    assert tool_metadata("edit")["supports_staging"] is True
    assert tool_metadata("read")["supports_staging"] is False
    assert tool_metadata("read")["supports_direct"] is True
    assert tool_metadata("read")["timeout_seconds"] is None


def test_zeta_tool_bash_direct_records_duration() -> None:
    data = tool_registry.invoke(
        "bash",
        {"command": "printf timed"},
        execution_mode="direct",
    )

    duration = data["metadata"]["duration_ms"]
    assert isinstance(duration, int)
    assert duration >= 0


def test_zeta_tool_write_direct_records_content_hashes(tmp_path: Path) -> None:
    target = tmp_path / "direct.txt"
    target.write_text("old\n", encoding="utf-8")

    data = tool_registry.invoke(
        "write",
        {"path": str(target), "content": "hello\n"},
        execution_mode="direct",
    )

    metadata = data["metadata"]
    assert metadata["before_hash"] == "sha256:" + hashlib.sha256(b"old\n").hexdigest()
    assert metadata["after_hash"] == "sha256:" + hashlib.sha256(b"hello\n").hexdigest()


def test_zeta_tool_write_stage_records_staged_hashes(tmp_path: Path) -> None:
    target = tmp_path / "staged.txt"
    target.write_text("old\n", encoding="utf-8")

    data = tool_registry.invoke("write", {"path": str(target), "content": "hello\n"})

    assert data["effect"]["command"].startswith("cp ")
    metadata = data["metadata"]
    assert metadata["path"] == str(target)
    assert metadata["before_hash"] == "sha256:" + hashlib.sha256(b"old\n").hexdigest()
    assert metadata["after_hash"] == "sha256:" + hashlib.sha256(b"hello\n").hexdigest()
    assert target.read_text(encoding="utf-8") == "old\n"


def test_zeta_tool_write_omits_before_hash_for_new_file(tmp_path: Path) -> None:
    target = tmp_path / "fresh.txt"

    data = tool_registry.invoke(
        "write",
        {"path": str(target), "content": "hello\n"},
        execution_mode="direct",
    )

    metadata = data["metadata"]
    assert "before_hash" not in metadata
    assert metadata["after_hash"] == "sha256:" + hashlib.sha256(b"hello\n").hexdigest()


def test_zeta_tool_edit_direct_records_content_hashes(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("hello\nold\nbye\n", encoding="utf-8")

    data = tool_registry.invoke(
        "edit",
        {"location": str(target), "old": "old\n", "new": "new\n"},
        execution_mode="direct",
    )

    metadata = data["metadata"]
    before = "sha256:" + hashlib.sha256(b"hello\nold\nbye\n").hexdigest()
    after = "sha256:" + hashlib.sha256(b"hello\nnew\nbye\n").hexdigest()
    assert metadata["before_hash"] == before
    assert metadata["after_hash"] == after


def test_zeta_tool_edit_stage_records_staged_hashes(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("hello\nold\nbye\n", encoding="utf-8")

    data = tool_registry.invoke(
        "edit", {"location": str(target), "old": "old\n", "new": "new\n"}
    )

    assert data["effect"]["command"].startswith("git apply ")
    metadata = data["metadata"]
    assert metadata["path"] == str(target)
    before = "sha256:" + hashlib.sha256(b"hello\nold\nbye\n").hexdigest()
    after = "sha256:" + hashlib.sha256(b"hello\nnew\nbye\n").hexdigest()
    assert metadata["before_hash"] == before
    assert metadata["after_hash"] == after
    assert target.read_text(encoding="utf-8") == "hello\nold\nbye\n"


def seed_query_log_history(monkeypatch) -> None:
    from sigil.protocols import turn_contract
    from sigil.sessions import session_id
    from sigil.state import append_event, event_store_path
    from zeta.history import effect_record, publish_effect_record, turn_record

    monkeypatch.setenv("SIGIL_SESSION_ID", "query-log-here")
    append_event(
        {
            **turn_record(
                "turn-do-1111",
                workflow="do",
                objective="refactor the staging path",
                contract=turn_contract("do", ("read", "edit"), staged=False),
                outcome="executed",
                cost={
                    "input_tokens": 1000,
                    "output_tokens": 200,
                    "model_calls": 3,
                },
                prompt_object_ids=["sha256:" + "70da571d" + "0" * 56],
            ),
            "time": 100.0,
        }
    )
    append_event(
        {
            **turn_record(
                "turn-ask-2222",
                workflow="ask",
                objective="why did the test fail?",
                contract=turn_contract("ask", (), staged=False),
                outcome="failed",
            ),
            "time": 200.0,
            "session": "query-log-there",
        }
    )
    publish_effect_record(
        effect_record(
            "effect-edit",
            turn_id="turn-do-1111",
            kind="file_edit",
            staged=False,
            path="/tmp/notes.txt",
        ),
        path=event_store_path(),
        session_id=session_id(),
    )


def test_zeta_tool_query_log_lists_all_sessions_with_cited_ids(monkeypatch) -> None:
    seed_query_log_history(monkeypatch)
    from sigil.tools import query_log as query_log_tool

    result = query_log_tool.run({})

    assert result["ok"] is True
    text = result["content"][0]["text"]
    lines = text.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("turn-ask")
    assert lines[1].startswith("turn-do-")
    assert "1200 tok" in lines[1]
    assert result["metadata"]["turns"] == 2
    assert result["metadata"]["scope"] == "all-sessions"


def test_zeta_tool_query_log_narrows_to_the_current_session(monkeypatch) -> None:
    seed_query_log_history(monkeypatch)
    from sigil.tools import query_log as query_log_tool

    result = query_log_tool.run({"current_session": True})

    text = result["content"][0]["text"]
    assert "turn-do-" in text
    assert "turn-ask" not in text
    assert result["metadata"]["scope"] == "query-log-here"


def test_zeta_tool_query_log_filters_and_caps_limit(monkeypatch) -> None:
    seed_query_log_history(monkeypatch)
    from sigil.tools import query_log as query_log_tool

    failed = query_log_tool.run({"failed": True})
    touched = query_log_tool.run({"touched": "/tmp/notes.txt"})
    capped = query_log_tool.run({"limit": 500})

    assert "turn-ask" in failed["content"][0]["text"]
    assert "turn-do-" not in failed["content"][0]["text"]
    assert "turn-do-" in touched["content"][0]["text"]
    assert capped["metadata"]["limit"] == 50


def test_zeta_tool_query_log_expands_one_turn_by_prefix(monkeypatch) -> None:
    seed_query_log_history(monkeypatch)
    from sigil.tools import query_log as query_log_tool

    result = query_log_tool.run({"turn_id": "turn-do"})

    assert result["ok"] is True
    text = result["content"][0]["text"]
    assert "turn     turn-do-1111" in text
    assert "tools: read, edit" in text
    assert "file_edit" in text
    assert "70da571d" in text
    assert result["metadata"]["turn_id"] == "turn-do-1111"


def test_zeta_tool_query_log_reports_bad_ids_and_bad_since(monkeypatch) -> None:
    seed_query_log_history(monkeypatch)
    from sigil.tools import query_log as query_log_tool

    ambiguous = query_log_tool.run({"turn_id": "turn-"})
    unknown = query_log_tool.run({"turn_id": "nope"})
    bad_since = query_log_tool.run({"since": "yesterday-ish"})

    assert ambiguous["ok"] is False
    assert ambiguous["error"]["code"] == "ambiguous-turn-id"
    assert "turn-do-1111" in ambiguous["error"]["message"]
    assert unknown["ok"] is False
    assert unknown["error"]["code"] == "unknown-turn-id"
    assert bad_since["ok"] is False
    assert bad_since["error"]["code"] == "invalid-since"


def test_zeta_tool_query_log_reports_an_empty_history() -> None:
    from sigil.tools import query_log as query_log_tool

    result = query_log_tool.run({})

    assert result["ok"] is True
    assert "no turns recorded" in result["content"][0]["text"]
    assert result["metadata"]["turns"] == 0


def test_zeta_tool_query_log_is_a_readonly_ask_builtin() -> None:
    from sigil.tools import query_log as query_log_tool
    from sigil.workflows.ask import ASK_TOOLS

    assert query_log_tool.SPEC.mutates() is False
    assert tool_registry.get_by_alias("query_log") is not None
    assert "query_log" in ASK_TOOLS
