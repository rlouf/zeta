"""Builtin and plugin tool tests."""

from __future__ import annotations

import hashlib
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

    data = zeta_tools.run_tool(
        "docs_search", {"query": "install"}, execution_mode="direct"
    )
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

    data = zeta_tools.run_tool(
        "docs_search", {"query": "install"}, execution_mode="direct"
    )
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
        {"command": "printf '\\377\\376'"},
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
    metadata = data["metadata"]
    assert metadata["mode"] == "direct"
    assert metadata["path"] == str(target)
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

    data = zeta_tools.run_tool(
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

    data = zeta_tools.run_tool(
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


def test_zeta_plugin_without_declared_effects_is_refused_in_propose_mode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    script = tmp_path / "plugin.py"
    write_cli_plugin(script)
    write_tools_config(home, [sys.executable, str(script)])
    monkeypatch.setenv("HOME", str(home))

    data = zeta_tools.run_tool("docs_search", {"query": "install"})

    assert data["ok"] is False
    assert data["error"]["code"] == "staging-unsupported"


def test_zeta_plugin_with_read_only_effects_runs_in_propose_mode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    script = tmp_path / "plugin.py"
    write_cli_plugin(script, effects=["search"])
    write_tools_config(home, [sys.executable, str(script)])
    monkeypatch.setenv("HOME", str(home))

    data = zeta_tools.run_tool("docs_search", {"query": "install"})

    assert data["ok"] is True
    assert data["content"][0]["text"] == "docs:install"


def test_zeta_plugin_with_invalid_effects_reports_diagnostic(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    script = tmp_path / "plugin.py"
    write_cli_plugin(script, effects=["network"])
    write_tools_config(home, [sys.executable, str(script)])
    monkeypatch.setenv("HOME", str(home))

    data = zeta_tools.tools_list()
    names = {tool["name"] for tool in data["tools"]}

    assert "docs_search" not in names
    assert data["diagnostics"][0]["code"] == "plugin-metadata-invalid"


def test_zeta_builtin_metadata_declares_effects() -> None:
    assert zeta_tools.tool_metadata("bash")["effects"] == ["execute"]
    assert zeta_tools.tool_metadata("write")["effects"] == ["write"]
    assert zeta_tools.tool_metadata("edit")["effects"] == ["write"]
    assert zeta_tools.tool_metadata("read")["effects"] == ["read"]
    assert zeta_tools.tool_metadata("grep")["effects"] == ["search"]
    assert zeta_tools.tool_metadata("ls")["effects"] == ["read"]


def test_zeta_tool_bash_direct_records_duration() -> None:
    data = zeta_tools.run_tool(
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

    data = zeta_tools.run_tool(
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

    data = zeta_tools.run_tool("write", {"path": str(target), "content": "hello\n"})

    assert data["handoff"]["command"].startswith("cp ")
    metadata = data["metadata"]
    assert metadata["path"] == str(target)
    assert metadata["before_hash"] == "sha256:" + hashlib.sha256(b"old\n").hexdigest()
    assert metadata["after_hash"] == "sha256:" + hashlib.sha256(b"hello\n").hexdigest()
    assert target.read_text(encoding="utf-8") == "old\n"


def test_zeta_tool_write_omits_before_hash_for_new_file(tmp_path: Path) -> None:
    target = tmp_path / "fresh.txt"

    data = zeta_tools.run_tool(
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

    data = zeta_tools.run_tool(
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

    data = zeta_tools.run_tool(
        "edit", {"location": str(target), "old": "old\n", "new": "new\n"}
    )

    assert data["handoff"]["command"].startswith("git apply ")
    metadata = data["metadata"]
    assert metadata["path"] == str(target)
    before = "sha256:" + hashlib.sha256(b"hello\nold\nbye\n").hexdigest()
    after = "sha256:" + hashlib.sha256(b"hello\nnew\nbye\n").hexdigest()
    assert metadata["before_hash"] == before
    assert metadata["after_hash"] == after
    assert target.read_text(encoding="utf-8") == "hello\nold\nbye\n"


def seed_query_log_ledger(monkeypatch) -> None:
    from sigil.ledger import default_ledger_index
    from sigil.protocols import effect_record, turn_contract, turn_record
    from sigil.state import append_event

    monkeypatch.setenv("SIGIL_SESSION_ID", "query-log-here")
    index = default_ledger_index()
    index.index_record(
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
    )
    index.index_record(
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
            }
        )
        | {"session": "query-log-there"}
    )
    index.index_record(
        append_event(
            effect_record(
                "effect-edit",
                turn_id="turn-do-1111",
                kind="file_edit",
                staged=False,
                path="/tmp/notes.txt",
            )
        )
    )


def test_zeta_tool_query_log_lists_all_sessions_with_cited_ids(monkeypatch) -> None:
    seed_query_log_ledger(monkeypatch)
    from sigil.zeta.tools import query_log as query_log_tool

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
    seed_query_log_ledger(monkeypatch)
    from sigil.zeta.tools import query_log as query_log_tool

    result = query_log_tool.run({"current_session": True})

    text = result["content"][0]["text"]
    assert "turn-do-" in text
    assert "turn-ask" not in text
    assert result["metadata"]["scope"] == "query-log-here"


def test_zeta_tool_query_log_filters_and_caps_limit(monkeypatch) -> None:
    seed_query_log_ledger(monkeypatch)
    from sigil.zeta.tools import query_log as query_log_tool

    failed = query_log_tool.run({"failed": True})
    touched = query_log_tool.run({"touched": "/tmp/notes.txt"})
    capped = query_log_tool.run({"limit": 500})

    assert "turn-ask" in failed["content"][0]["text"]
    assert "turn-do-" not in failed["content"][0]["text"]
    assert "turn-do-" in touched["content"][0]["text"]
    assert capped["metadata"]["limit"] == 50


def test_zeta_tool_query_log_expands_one_turn_by_prefix(monkeypatch) -> None:
    seed_query_log_ledger(monkeypatch)
    from sigil.zeta.tools import query_log as query_log_tool

    result = query_log_tool.run({"turn_id": "turn-do"})

    assert result["ok"] is True
    text = result["content"][0]["text"]
    assert "turn     turn-do-1111" in text
    assert "tools: read, edit" in text
    assert "file_edit" in text
    assert "70da571d" in text
    assert result["metadata"]["turn_id"] == "turn-do-1111"


def test_zeta_tool_query_log_reports_bad_ids_and_bad_since(monkeypatch) -> None:
    seed_query_log_ledger(monkeypatch)
    from sigil.zeta.tools import query_log as query_log_tool

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


def test_zeta_tool_query_log_reports_an_empty_ledger() -> None:
    from sigil.zeta.tools import query_log as query_log_tool

    result = query_log_tool.run({})

    assert result["ok"] is True
    assert "no turns recorded" in result["content"][0]["text"]
    assert result["metadata"]["turns"] == 0


def test_zeta_tool_query_log_is_a_readonly_ask_builtin() -> None:
    from sigil.workflows.ask import ASK_TOOLS
    from sigil.zeta.tools import query_log as query_log_tool

    assert query_log_tool.SPEC.mutates() is False
    assert "query_log" in zeta_tools.BUILTIN_TOOL_IMPLS
    assert "query_log" in ASK_TOOLS
