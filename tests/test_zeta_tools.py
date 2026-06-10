"""Builtin and plugin tool tests."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
from _zeta_helpers import (
    write_cli_plugin,
    write_tools_config,
)

from sigil.zeta import tools as zeta_tools
from sigil.zeta.tools import bash as bash_tool
from sigil.zeta.tools import grep as grep_tool
from sigil.zeta.tools import read as read_tool
from sigil.zeta.tools import validate_tool_args


def test_zeta_tools_list_exposes_v1_builtins() -> None:
    data = zeta_tools.tools_list()
    names = {tool["name"] for tool in data["tools"]}
    assert {"read", "grep", "ls", "bash", "edit", "write"} <= names
    assert data["tools"][0]["origin"] == "builtin"


def test_zeta_grep_metadata_guides_model_tool_choice() -> None:
    metadata = zeta_tools.tool_metadata("grep")
    schema = metadata["schema"]

    assert (
        metadata["description"]
        == "Search file contents recursively. Use before read when looking for symbols, errors, strings, or definitions."
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


def test_zeta_plugin_tool_flows_through_registry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    script = tmp_path / "plugin.py"
    write_cli_plugin(script)
    write_tools_config(home, [sys.executable, str(script)])
    monkeypatch.setenv("HOME", str(home))

    tools = zeta_tools.tools_list()["tools"]
    plugin = next(tool for tool in tools if tool["name"] == "docs_search")
    assert plugin["origin"] == "plugin"
    assert plugin["plugin"] == sys.executable

    descriptors = zeta_tools.model_tool_descriptors(("docs_search",))
    assert descriptors == [
        {
            "type": "function",
            "function": {
                "name": "docs_search",
                "description": "Search project docs.",
                "parameters": plugin["schema"],
            },
        }
    ]
    assert validate_tool_args("docs_search", {}) == [
        "$: 'query' is a required property"
    ]
    assert validate_tool_args("docs_search", {"query": "install"}) == []

    analysis = zeta_tools.analyze_tool("docs_search", {"query": "install"})
    assert analysis["valid"] is True
    assert analysis["effects"][0]["target"] == "install"

    data = zeta_tools.run_tool("docs_search", {"query": "install"})
    assert data["ok"] is True
    assert data["content"][0]["text"] == "docs:install"


def test_zeta_plugin_name_collision_is_ignored(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    script = tmp_path / "plugin.py"
    write_cli_plugin(script, name="read")
    write_tools_config(home, [sys.executable, str(script)])
    monkeypatch.setenv("HOME", str(home))

    data = zeta_tools.tools_list()
    read_tools = [tool for tool in data["tools"] if tool["name"] == "read"]
    assert len(read_tools) == 1
    assert read_tools[0]["origin"] == "builtin"
    assert data["diagnostics"][0]["code"] == "plugin-name-collision"


def test_zeta_plugin_invalid_metadata_reports_diagnostic(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    script = tmp_path / "plugin.py"
    write_cli_plugin(script, invalid_metadata=True)
    write_tools_config(home, [sys.executable, str(script)])
    monkeypatch.setenv("HOME", str(home))

    data = zeta_tools.tools_list()
    assert "docs_search" not in {tool["name"] for tool in data["tools"]}
    assert data["diagnostics"][0]["code"] == "plugin-metadata-invalid-json"


def test_zeta_plugin_missing_command_reports_diagnostic(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    write_tools_config(home, [str(tmp_path / "missing-tool")])
    monkeypatch.setenv("HOME", str(home))

    data = zeta_tools.tools_list()
    assert data["diagnostics"][0]["code"] == "plugin-metadata-failed"


def test_zeta_plugin_metadata_timeout_reports_diagnostic(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    script = tmp_path / "plugin.py"
    write_cli_plugin(script, sleep_metadata=True)
    write_tools_config(home, [sys.executable, str(script)], timeout_ms=10)
    monkeypatch.setenv("HOME", str(home))

    data = zeta_tools.tools_list()
    assert data["diagnostics"][0]["code"] == "plugin-metadata-timeout"


def test_zeta_plugin_nonzero_execution_returns_tool_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    script = tmp_path / "plugin.py"
    write_cli_plugin(script, fail_run=True)
    write_tools_config(home, [sys.executable, str(script)])
    monkeypatch.setenv("HOME", str(home))

    data = zeta_tools.run_tool("docs_search", {"query": "install"})
    assert data["ok"] is False
    assert data["error"]["code"] == "plugin-run-failed"
    assert "status 7" in data["error"]["message"]


def test_zeta_tool_read_schema_and_run(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("hello zeta\n", encoding="utf-8")

    assert zeta_tools.tool_metadata("read")["schema"]["required"] == ["path"]

    data = zeta_tools.run_tool("read", {"path": str(target)})
    assert data["ok"] is True
    assert data["content"][0]["text"] == "hello zeta\n"


def test_zeta_tool_read_offset_and_limit_select_lines(tmp_path: Path) -> None:
    target = tmp_path / "lines.txt"
    target.write_text("one\ntwo\nthree\nfour\nfive\n", encoding="utf-8")

    data = zeta_tools.run_tool("read", {"path": str(target), "offset": 1, "limit": 2})

    assert data["ok"] is True
    assert data["content"][0]["text"] == "two\nthree\n"
    assert data["metadata"]["offset"] == 1
    assert data["metadata"]["limit"] == 2


def test_zeta_tool_read_limit_past_end_returns_remaining_lines(tmp_path: Path) -> None:
    target = tmp_path / "short.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")

    data = zeta_tools.run_tool("read", {"path": str(target), "offset": 1, "limit": 10})

    assert data["content"][0]["text"] == "beta\n"


def test_zeta_tool_read_rejects_binary_file(tmp_path: Path) -> None:
    target = tmp_path / "image.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")

    data = zeta_tools.run_tool("read", {"path": str(target)})

    assert data["ok"] is False
    assert data["error"]["code"] == "binary-file"


def test_zeta_tool_read_caps_returned_characters(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(read_tool, "MAX_READ_CHARS", 100)
    target = tmp_path / "wide.txt"
    target.write_text("x" * 1_000 + "\n", encoding="utf-8")

    data = zeta_tools.run_tool("read", {"path": str(target)})

    assert data["ok"] is True
    assert len(data["content"][0]["text"]) == 100
    assert data["metadata"]["truncated"] is True


def test_zeta_tool_grep_reports_total_limited_metadata(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("needle one\nneedle two\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("needle three\n", encoding="utf-8")

    data = zeta_tools.run_tool(
        "grep", {"path": str(tmp_path), "pattern": "needle", "limit": 2}
    )

    assert data["ok"] is True
    assert data["content"][0]["text"].count("needle") == 2
    assert data["metadata"]["matches"] == 2
    assert data["metadata"]["files"] == 1
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

    data = zeta_tools.run_tool("grep", {"path": str(target), "pattern": "needle"})

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

    data = zeta_tools.run_tool("grep", {"path": str(tmp_path), "pattern": "needle"})

    assert data["ok"] is True
    assert data["metadata"]["matches"] == 2
    lines = data["content"][0]["text"].splitlines()
    assert lines[0].endswith("needle one")
    assert lines[1].endswith("needle two")


def test_zeta_tool_grep_fallback_stops_at_limit(tmp_path: Path, monkeypatch) -> None:
    for index in range(20):
        (tmp_path / f"file-{index:02}.txt").write_text("needle\n", encoding="utf-8")

    def missing_rg(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("rg")

    monkeypatch.setattr(grep_tool.subprocess, "Popen", missing_rg)

    data = zeta_tools.run_tool(
        "grep", {"path": str(tmp_path), "pattern": "needle", "limit": 3}
    )

    assert data["metadata"]["matches"] == 3
    assert data["metadata"]["truncated"] is True


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is not installed")
def test_zeta_tool_grep_reports_invalid_pattern_error(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("text\n", encoding="utf-8")

    data = zeta_tools.run_tool("grep", {"path": str(tmp_path), "pattern": "("})

    assert data["ok"] is False
    assert data["metadata"]["status"] not in {0, 1}
    assert data["content"][0]["text"]


def test_zeta_tool_bash_returns_handoff() -> None:
    data = zeta_tools.run_tool(
        "bash", {"command": "uv run pytest", "reason": "Run tests."}
    )

    assert data["handoff"]["command"] == "uv run pytest"
    assert data["handoff"]["reason"] == "Run tests."


def test_zeta_tool_bash_direct_executes_command() -> None:
    data = zeta_tools.run_tool(
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
    data = zeta_tools.run_tool(
        "bash",
        {"command": "printf '\\xff\\xfe'"},
        execution_mode="direct",
    )

    assert data["ok"] is True
    assert "�" in data["content"][0]["text"]


def test_zeta_tool_bash_direct_kills_command_on_timeout(monkeypatch) -> None:
    monkeypatch.setattr(bash_tool, "DEFAULT_TIMEOUT_SECONDS", 0.2)

    data = zeta_tools.run_tool(
        "bash",
        {"command": "sleep 5"},
        execution_mode="direct",
    )

    assert data["ok"] is False
    assert data["error"]["code"] == "bash-timeout"
    assert data["metadata"]["timed_out"] is True
    assert "timed out" in data["content"][0]["text"]


def test_zeta_tool_bash_direct_truncates_large_output() -> None:
    data = zeta_tools.run_tool(
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

    data = zeta_tools.run_tool(
        "write",
        {"path": str(target), "content": "hello\n"},
        execution_mode="direct",
    )

    assert data["ok"] is True
    assert data["metadata"] == {"mode": "direct", "path": str(target)}
    assert target.read_text(encoding="utf-8") == "hello\n"


def test_zeta_tool_ls_lists_directory_contents(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    data = zeta_tools.run_tool("ls", {"path": str(tmp_path)})

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

    data = zeta_tools.run_tool(
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

    data = zeta_tools.run_tool(
        "edit", {"location": str(target), "old": "old\n", "new": "new\n"}
    )
    artifact = Path(data["handoff"]["artifact"])
    assert artifact.exists()
    patch = artifact.read_text(encoding="utf-8")
    assert "-old\n" in patch
    assert "+new\n" in patch
    assert data["handoff"]["command"].startswith("git apply ")


def test_zeta_tool_edit_accepts_exact_replacement(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("hello\nold\nbye\n", encoding="utf-8")
    payload = {
        "location": str(target),
        "old": "old\n",
        "new": "new\n",
        "reason": "Replace one line.",
    }

    data = zeta_tools.run_tool("edit", payload)

    assert validate_tool_args("edit", payload) == []
    artifact = Path(data["handoff"]["artifact"])
    patch = artifact.read_text(encoding="utf-8")
    assert data["handoff"]["command"].startswith("git apply ")
    assert data["handoff"]["reason"] == "Replace one line."
    assert "-old\n" in patch
    assert "+new\n" in patch


def test_zeta_tool_edit_direct_replace_writes_file(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("hello\nold\nbye\n", encoding="utf-8")

    data = zeta_tools.run_tool(
        "edit",
        {"location": str(target), "old": "old\n", "new": "new\n"},
        edit_mode="direct_replace",
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

    data = zeta_tools.run_tool(
        "edit",
        {"location": str(target), "old": "old", "new": "new"},
        edit_mode="direct_replace",
    )

    assert data["ok"] is False
    assert data["error"]["code"] == "not-utf8"
    assert target.read_bytes() == b"caf\xe9 old\n"


def test_zeta_tool_edit_direct_reports_write_failure(tmp_path: Path) -> None:
    target = tmp_path / "readonly.txt"
    target.write_text("old\n", encoding="utf-8")
    target.chmod(0o444)

    data = zeta_tools.run_tool(
        "edit",
        {"location": str(target), "old": "old\n", "new": "new\n"},
        edit_mode="direct_replace",
    )

    target.chmod(0o644)
    assert data["ok"] is False
    assert data["error"]["code"] == "write-failed"
    assert target.read_text(encoding="utf-8") == "old\n"


def test_zeta_tool_edit_rejects_ambiguous_exact_replacement(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("old\nold\n", encoding="utf-8")

    data = zeta_tools.run_tool(
        "edit", {"location": str(target), "old": "old\n", "new": "new\n"}
    )

    assert data["ok"] is False
    assert data["error"]["code"] == "old-text-not-unique"


def test_zeta_tool_edit_marks_no_newline_exact_replacement(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("old", encoding="utf-8")

    data = zeta_tools.run_tool(
        "edit", {"location": str(target), "old": "old", "new": "new"}
    )

    artifact = Path(data["handoff"]["artifact"])
    patch = artifact.read_text(encoding="utf-8")
    assert "-old\n\\ No newline at end of file\n" in patch
    assert "+new\n\\ No newline at end of file\n" in patch


def test_zeta_edit_analysis_reports_location() -> None:
    data = zeta_tools.analyze_tool(
        "edit",
        {"location": "src/new.py", "old": "x", "new": "y"},
    )
    assert data["valid"] is True
    assert data["resolved"] is True
    assert [effect["target"] for effect in data["effects"]] == ["src/new.py"]
