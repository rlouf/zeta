from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from click.testing import CliRunner

from sigil import answers as answers_runner
from sigil import display as sigil_display
from sigil import handoff as sigil_handoff
from sigil.cli import cli as sigil_cli
from sigil.session import recent_turns, record_turn
from sigil.zeta import runtime as zeta
from sigil.zeta.cli import cli as zeta_cli


def test_zeta_tools_list_exposes_v1_builtins() -> None:
    result = CliRunner().invoke(zeta_cli, ["tools", "list", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    names = {tool["name"] for tool in data["tools"]}
    assert {"read", "grep", "ls", "bash", "edit", "write"} <= names
    assert data["tools"][0]["origin"] == "builtin"


def test_zeta_tool_read_schema_and_run(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("hello zeta\n", encoding="utf-8")

    schema = CliRunner().invoke(zeta_cli, ["tool", "read", "--schema"])
    assert schema.exit_code == 0
    assert json.loads(schema.output)["required"] == ["path"]

    result = CliRunner().invoke(
        zeta_cli,
        ["tool", "read"],
        input=json.dumps({"path": str(target)}),
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["content"][0]["text"] == "hello zeta\n"


def test_zeta_tool_bash_returns_handoff() -> None:
    result = CliRunner().invoke(
        zeta_cli,
        ["tool", "bash"],
        input=json.dumps({"command": "uv run pytest", "reason": "Run tests."}),
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["handoff"]["command"] == "uv run pytest"
    assert data["handoff"]["reason"] == "Run tests."


def test_sigil_display_summarizes_tool_results() -> None:
    assert sigil_display.tool_result_summary(
        "bash",
        {"ok": True, "handoff": {"type": "shell_prompt", "command": "uv run pytest"}},
    ) == ["staged in prompt"]
    assert sigil_display.tool_result_summary(
        "read",
        {"ok": True, "content": [{"type": "text", "text": "a\nb\n"}]},
    ) == ["2 lines · 4 bytes"]
    assert sigil_display.tool_result_summary(
        "grep",
        {"ok": True, "content": [{"type": "text", "text": "a.py:1:x\nb.py:2:y\n"}]},
    ) == ["2 matches · 2 files"]


def test_sigil_display_summarizes_shell_results() -> None:
    assert sigil_display.shell_result_summary(
        {
            "type": "tool_result",
            "result": {
                "outcome": "executed",
                "executed_command": "uv run pytest",
                "status": 0,
                "shell_turns": [{"command": "uv run pytest"}],
            },
        }
    ) == ["❯ shell  captured", "  uv run pytest", "  exit 0 · 1 shell turn"]
    assert sigil_display.shell_result_summary(
        {
            "type": "tool_result",
            "result": {
                "outcome": "cancelled",
                "expected_command": "uv run pytest",
                "actual_command": "uv run pytest -q",
            },
        }
    ) == [
        "❯ shell  changed",
        "  expected: uv run pytest",
        "  ran:      uv run pytest -q",
    ]


def test_zeta_tool_ls_lists_directory_contents(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    result = CliRunner().invoke(
        zeta_cli,
        ["tool", "ls"],
        input=json.dumps({"path": str(tmp_path)}),
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
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

    result = CliRunner().invoke(
        zeta_cli,
        ["tool", "ls"],
        input=json.dumps(
            {
                "path": str(tmp_path),
                "recursive": True,
                "min_size_bytes": 10,
                "exclude": [".git"],
            }
        ),
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["content"][0]["text"].splitlines() == ["12\tfile\tsrc/large.bin"]
    assert data["metadata"]["entries"] == 1
    assert data["metadata"]["exclude"] == [".git"]


def test_zeta_tool_edit_writes_patch_artifact() -> None:
    patch = "--- a/a.txt\n+++ b/a.txt\n@@\n-old\n+new\n"
    result = CliRunner().invoke(
        zeta_cli,
        ["tool", "edit"],
        input=json.dumps({"patch": patch}),
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    artifact = Path(data["handoff"]["artifact"])
    assert artifact.exists()
    assert artifact.read_text(encoding="utf-8") == patch
    assert data["handoff"]["command"].startswith("git apply ")


def test_zeta_transcript_append_and_tail(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "zeta-test")

    runner = CliRunner()
    appended = runner.invoke(
        zeta_cli,
        ["transcript", "append"],
        input=json.dumps({"type": "tool_call", "name": "read"}),
    )
    assert appended.exit_code == 0

    tail = runner.invoke(zeta_cli, ["transcript", "tail", "--limit", "1"])
    assert tail.exit_code == 0
    data = json.loads(tail.output)
    assert data["events"][0]["type"] == "tool_call"
    assert data["events"][0]["name"] == "read"


def test_zeta_transcript_does_not_expose_shell_handoff_verbs() -> None:
    result = CliRunner().invoke(zeta_cli, ["transcript", "shell-result"])

    assert result.exit_code != 0


def test_sigil_transcript_shell_turn_records_recent_turn(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "zeta-test")

    result = CliRunner().invoke(
        sigil_cli,
        ["transcript", "shell-turn"],
        input=json.dumps(
            {
                "command": "uv run pytest",
                "status": 1,
                "cwd": "/repo",
                "stderr_snippet": "test failed",
            }
        ),
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["type"] == "shell_turn_recorded"
    assert data["command"] == "uv run pytest"
    turns = recent_turns()
    assert len(turns) == 1
    assert turns[0]["command"] == "uv run pytest"
    assert turns[0]["status"] == 1
    assert turns[0]["turn_cwd"] == "/repo"
    assert turns[0]["stderr_snippet"] == "test failed"


def test_zeta_patch_analysis_extracts_paths() -> None:
    patch = "--- a/src/old.py\n+++ b/src/new.py\n@@\n-x\n+y\n"
    data = zeta.analyze_tool("edit", {"patch": patch})
    assert data["valid"] is True
    assert data["resolved"] is True
    assert [effect["target"] for effect in data["effects"]] == [
        "src/old.py",
        "src/new.py",
    ]


def test_zeta_next_model_action_accepts_route_specific_system_prompt(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_chat_json(
        system: str, user: str, schema: dict[str, object]
    ) -> dict[str, object]:
        captured["system"] = system
        captured["user"] = user
        captured["schema"] = schema
        return {"type": "final", "content": "done"}

    monkeypatch.setattr(zeta, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta, "chat_json", fake_chat_json)

    action = zeta.next_model_action("repair", [], system="custom system")

    assert action == {"type": "final", "content": "done"}
    system_prompt = str(captured["system"])
    assert system_prompt.startswith("custom system")
    assert "Available tools with input JSON Schemas:" in system_prompt
    assert '"name":"read"' in system_prompt


def test_zeta_system_prompt_is_product_neutral_and_dynamic() -> None:
    prompt = zeta.zeta_system_prompt(allowed_tools=("read", "ls"))

    assert "Sigil" not in prompt
    assert "Available tools with input JSON Schemas:" in prompt
    assert '"name":"read"' in prompt
    assert '"name":"ls"' in prompt
    assert '"name":"bash"' not in prompt


def test_zeta_next_model_action_filters_available_tools(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_chat_json(
        system: str, user: str, schema: dict[str, object]
    ) -> dict[str, object]:
        captured["system"] = system
        captured["user"] = user
        captured["schema"] = schema
        return {
            "type": "tool_call",
            "name": "read",
            "input": {"path": "pyproject.toml"},
        }

    monkeypatch.setattr(zeta, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta, "chat_json", fake_chat_json)

    action = zeta.next_model_action(
        "What does this pyproject.toml file contain?",
        [],
        allowed_tools=("read", "grep"),
    )

    assert action == {
        "type": "tool_call",
        "name": "read",
        "input": {"path": "pyproject.toml"},
    }
    schema = cast(dict[str, Any], captured["schema"])
    properties = cast(dict[str, Any], schema["properties"])
    name_schema = cast(dict[str, Any], properties["name"])
    assert name_schema["enum"] == ["grep", "read"]
    system_prompt = str(captured["system"])
    assert '"type":"function"' in system_prompt
    assert '"name":"read"' in system_prompt
    assert '"name":"grep"' in system_prompt
    assert '"parameters":{"type":"object"' in system_prompt
    assert '"schema"' not in system_prompt
    assert '"name":"bash"' not in system_prompt
    user_prompt = str(captured["user"])
    assert "Available tools" not in user_prompt
    assert '"name":"read"' not in user_prompt


def test_zeta_next_model_action_rejects_disallowed_tool(monkeypatch) -> None:
    def fake_chat_json(
        system: str, user: str, schema: dict[str, object]
    ) -> dict[str, object]:
        del system, user, schema
        return {
            "type": "tool_call",
            "name": "bash",
            "input": {"command": "cat pyproject.toml"},
        }

    monkeypatch.setattr(zeta, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta, "chat_json", fake_chat_json)

    action = zeta.next_model_action("inspect file", [], allowed_tools=("read", "grep"))

    assert action == {
        "type": "final",
        "content": "I could not choose a valid Zeta tool for the next step.",
    }


def test_zeta_user_prompt_includes_explicit_context() -> None:
    context = "\n".join(
        [
            "Recent shell activity:",
            "- uv run pytest (exit 1)",
            "  stderr: test failed",
        ]
    )

    prompt = zeta.zeta_user_prompt(
        "Continue the active Zeta step.",
        [],
        context=context,
    )

    assert "Recent shell activity:" in prompt
    assert "uv run pytest (exit 1)" in prompt
    assert "stderr: test failed" in prompt


def test_sigil_transcript_shell_result_appends_tool_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "zeta-test")
    zeta.append_transcript(
        {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "name": "bash",
            "result": {
                "ok": True,
                "handoff": {
                    "type": "shell_prompt",
                    "command": "uv run pytest",
                    "reason": "Run tests.",
                },
            },
        }
    )
    record_turn("uv run pytest", 1, "/repo", stderr_snippet="test failed")

    result = CliRunner().invoke(sigil_cli, ["transcript", "shell-result"])

    assert result.exit_code == 0
    event = json.loads(result.output)
    assert event["type"] == "tool_result"
    assert event["tool_call_id"] == "call-1"
    assert event["name"] == "bash"
    assert event["result"]["ok"] is True
    assert event["result"]["schema"] == "zeta.shell_handoff_result.v1"
    assert event["result"]["type"] == "shell_handoff_result"
    assert event["result"]["outcome"] == "executed"
    assert event["result"]["command"] == "uv run pytest"
    assert event["result"]["expected_command"] == "uv run pytest"
    assert event["result"]["executed_command"] == "uv run pytest"
    assert event["result"]["status"] == 1
    assert event["result"]["shell_turns"][0]["command"] == "uv run pytest"
    assert "uv run pytest (exit 1)" in event["result"]["content"][0]["text"]


def test_sigil_transcript_shell_result_cancels_modified_handoff(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "zeta-test")
    zeta.append_transcript(
        {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "name": "bash",
            "result": {
                "ok": True,
                "handoff": {
                    "type": "shell_prompt",
                    "command": "uv run pytest",
                    "reason": "Run tests.",
                },
            },
        }
    )
    record_turn("uv run pytest -q", 1, "/repo", stderr_snippet="test failed")

    event = sigil_handoff.append_shell_result()

    assert event["type"] == "tool_result"
    assert event["tool_call_id"] == "call-1"
    assert event["result"]["ok"] is False
    assert event["result"]["schema"] == "zeta.shell_handoff_result.v1"
    assert event["result"]["type"] == "shell_handoff_result"
    assert event["result"]["outcome"] == "cancelled"
    assert event["result"]["cancellation_reason"] == "expected_command_not_executed"
    assert event["result"]["expected_command"] == "uv run pytest"
    assert event["result"]["actual_command"] == "uv run pytest -q"
    assert event["result"]["shell_turns"][0]["command"] == "uv run pytest -q"


def test_sigil_transcript_shell_result_includes_intervening_shell_turns(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "zeta-test")
    zeta.append_transcript(
        {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "name": "bash",
            "result": {
                "ok": True,
                "handoff": {
                    "type": "shell_prompt",
                    "command": "uv run pytest",
                    "reason": "Run tests.",
                },
            },
        }
    )
    record_turn("git status --short", 0, "/repo", stdout_snippet=" M README.md")
    record_turn("uv run pytest", 0, "/repo", stdout_snippet="191 passed")

    event = sigil_handoff.append_shell_result()

    assert event["result"]["outcome"] == "executed"
    assert event["result"]["executed_command"] == "uv run pytest"
    assert [turn["command"] for turn in event["result"]["shell_turns"]] == [
        "git status --short",
        "uv run pytest",
    ]
    assert "1 user shell turn" in event["result"]["content"][0]["text"]


def test_sigil_transcript_shell_result_does_not_reuse_resolved_handoff(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "zeta-test")
    zeta.append_transcript(
        {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "name": "bash",
            "result": {
                "ok": True,
                "handoff": {
                    "type": "shell_prompt",
                    "command": "uv run pytest",
                    "reason": "Run tests.",
                },
            },
        }
    )
    record_turn("uv run pytest", 0, "/repo", stdout_snippet="191 passed")

    first = sigil_handoff.append_shell_result()
    second = sigil_handoff.append_shell_result()

    assert first["type"] == "tool_result"
    assert first["result"]["outcome"] == "executed"
    assert second["type"] == "shell_resume"
    assert second["result"]["outcome"] == "no_pending_handoff"
    assert second["result"]["shell_turns"][0]["command"] == "uv run pytest"


def test_zeta_question_loop_feeds_current_tool_result_to_next_step(
    monkeypatch,
    capsys,
) -> None:
    transcripts: list[list[dict[str, Any]]] = []

    def fake_next_model_action(
        objective: str,
        transcript: list[dict[str, Any]],
        **kwargs: object,
    ) -> dict[str, object]:
        del objective, kwargs
        transcripts.append(transcript)
        if len(transcripts) == 1:
            return {
                "type": "tool_call",
                "name": "read",
                "input": {"path": "pyproject.toml"},
            }
        assert any(event.get("type") == "tool_result" for event in transcript)
        return {"type": "final", "content": "It contains project metadata."}

    monkeypatch.setattr(answers_runner, "ensure_server", lambda: True)
    monkeypatch.setattr(
        answers_runner.runtime, "next_model_action", fake_next_model_action
    )
    monkeypatch.setattr(
        answers_runner.runtime,
        "run_tool",
        lambda name, params: {
            "ok": True,
            "content": [{"type": "text", "text": "[project]\nname = 'sigil'\n"}],
        },
    )

    code = answers_runner.run_tool_answer(
        "question system",
        "What does pyproject.toml contain?",
    )

    assert code == 0
    output = capsys.readouterr().out
    assert "❯ read   pyproject.toml" in output
    assert "project metadata" in output
    assert len(transcripts) == 2


def test_zeta_question_loop_falls_back_instead_of_budget_message(
    monkeypatch,
    capsys,
) -> None:
    def fake_next_model_action(
        objective: str,
        transcript: list[dict[str, Any]],
        **kwargs: object,
    ) -> dict[str, object]:
        del objective, transcript, kwargs
        return {"type": "tool_call", "name": "read", "input": {"path": "README.md"}}

    monkeypatch.setattr(answers_runner, "ensure_server", lambda: True)
    monkeypatch.setattr(
        answers_runner.runtime, "next_model_action", fake_next_model_action
    )
    monkeypatch.setattr(
        answers_runner.runtime,
        "run_tool",
        lambda name, params: {
            "ok": True,
            "content": [{"type": "text", "text": "Sigil docs"}],
        },
    )
    monkeypatch.setattr(
        answers_runner,
        "chat_text",
        lambda system, prompt, max_tokens: "It contains Sigil docs.",
    )

    code = answers_runner.run_tool_answer(
        "question system",
        "What does README.md contain?",
        max_steps=1,
    )

    output = capsys.readouterr().out
    assert code == 0
    assert "It contains Sigil docs." in output
    assert "question tool budget" not in output
