from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from click.testing import CliRunner

from sigil import answers as answers_runner
from sigil import display as sigil_display
from sigil import handoff as sigil_handoff
from sigil import zeta_runner
from sigil.cli import cli as sigil_cli
from sigil.protocol import (
    SHELL_HANDOFF_CANCEL_EXPECTED_NOT_EXECUTED,
    SHELL_HANDOFF_OUTCOME_CANCELLED,
    SHELL_HANDOFF_OUTCOME_EXECUTED,
    SHELL_HANDOFF_OUTCOME_NO_PENDING,
    SHELL_HANDOFF_RESULT_SCHEMA,
    SHELL_HANDOFF_RESULT_TYPE,
    SHELL_PROMPT_HANDOFF_TYPE,
)
from sigil.session import recent_turns, record_turn
from sigil.zeta import runtime as zeta
from sigil.zeta import model as zeta_model
from sigil.zeta.cli import cli as zeta_cli


def test_zeta_model_config_uses_zeta_env(monkeypatch) -> None:
    monkeypatch.setenv("SIGIL_MODEL_URL", "http://legacy.invalid/v1/chat/completions")
    monkeypatch.setenv("SIGIL_MODEL_NAME", "legacy-model")
    monkeypatch.delenv("ZETA_MODEL_URL", raising=False)
    monkeypatch.delenv("ZETA_MODEL_NAME", raising=False)

    assert zeta_model.model_url() == zeta_model.DEFAULT_MODEL_URL
    assert zeta_model.model_name() == zeta_model.DEFAULT_MODEL_NAME

    monkeypatch.setenv("ZETA_MODEL_URL", "http://zeta.invalid/v1/chat/completions")
    monkeypatch.setenv("ZETA_MODEL_NAME", "zeta-model")

    assert zeta_model.model_url() == "http://zeta.invalid/v1/chat/completions"
    assert zeta_model.model_name() == "zeta-model"


def test_zeta_tools_list_exposes_v1_builtins() -> None:
    result = CliRunner().invoke(zeta_cli, ["tools", "list", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    names = {tool["name"] for tool in data["tools"]}
    assert {"read", "grep", "ls", "bash", "edit", "write"} <= names
    assert data["tools"][0]["origin"] == "builtin"


def test_zeta_help_frames_cli_as_bundled_runtime_service() -> None:
    result = CliRunner().invoke(zeta_cli, ["--help"])

    assert result.exit_code == 0
    assert "Bundled runtime service commands used by Sigil." in result.output


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
        {
            "ok": True,
            "handoff": {
                "type": SHELL_PROMPT_HANDOFF_TYPE,
                "command": "uv run pytest",
            },
        },
    ) == ["staged in prompt"]
    assert sigil_display.tool_result_summary(
        "read",
        {"ok": True, "content": [{"type": "text", "text": "a\nb\n"}]},
    ) == ["2 lines"]
    assert sigil_display.tool_result_summary(
        "grep",
        {"ok": True, "content": [{"type": "text", "text": "a.py:1:x\nb.py:2:y\n"}]},
    ) == ["2 matches · 2 files"]


def test_sigil_display_summarizes_shell_results() -> None:
    assert sigil_display.shell_result_summary(
        {
            "type": "tool_result",
            "result": {
                "outcome": SHELL_HANDOFF_OUTCOME_EXECUTED,
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
                "outcome": SHELL_HANDOFF_OUTCOME_CANCELLED,
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


def test_sigil_zeta_step_writes_handoff_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    handoff_file = tmp_path / "handoff.json"

    monkeypatch.setattr(zeta_runner, "ensure_server", lambda: True)
    monkeypatch.setattr(
        zeta_runner.runtime,
        "next_model_action",
        lambda *args, **kwargs: {
            "type": "tool_call",
            "name": "bash",
            "input": {"command": "uv run pytest", "reason": "Run tests."},
        },
    )
    monkeypatch.setattr(
        zeta_runner.runtime,
        "analyze_tool",
        lambda name, params: {"valid": True, "resolved": True},
    )
    monkeypatch.setattr(
        zeta_runner.runtime,
        "run_tool",
        lambda name, params: {
            "ok": True,
            "handoff": {
                "type": SHELL_PROMPT_HANDOFF_TYPE,
                "command": "uv run pytest",
                "reason": "Run tests.",
            },
        },
    )

    result = CliRunner().invoke(
        sigil_cli,
        ["zeta-step", "--handoff-file", str(handoff_file), "repair"],
    )

    assert result.exit_code == 0
    assert "❯ bash   uv run pytest" in result.output
    assert "  staged in prompt" in result.output
    assert json.loads(handoff_file.read_text(encoding="utf-8")) == {
        "type": SHELL_PROMPT_HANDOFF_TYPE,
        "command": "uv run pytest",
        "reason": "Run tests.",
    }


def test_sigil_zeta_step_keeps_trace_off_stdout(monkeypatch) -> None:
    monkeypatch.setattr(zeta_runner, "ensure_server", lambda: True)
    monkeypatch.setattr(
        zeta_runner.runtime,
        "next_model_action",
        lambda *args, **kwargs: {"type": "final", "content": "summary"},
    )

    result = CliRunner().invoke(sigil_cli, ["zeta-step", "summarize"])

    assert result.exit_code == 0
    assert result.stdout == "\nsummary\n"
    assert "❯ zeta ,, " in result.stderr
    assert "❯ zeta ,, " not in result.stdout


def test_zeta_agent_step_separates_trace_from_final_answer(
    monkeypatch,
    capsys,
) -> None:
    def fake_next_model_action(
        objective: str,
        transcript: list[dict[str, Any]],
        **kwargs: object,
    ) -> dict[str, object]:
        del objective, transcript, kwargs
        return {"type": "final", "content": "The answer."}

    monkeypatch.setattr(zeta_runner, "ensure_server", lambda: True)
    monkeypatch.setattr(
        zeta_runner.runtime, "next_model_action", fake_next_model_action
    )

    code = zeta_runner.run_agent_step("answer me", glyph=",,")

    assert code == 0
    captured = capsys.readouterr()
    assert captured.out == "\nThe answer.\n"
    assert "❯ zeta ,, " in captured.err


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

    def fake_chat_json_messages(
        messages: list[dict[str, object]],
        schema: dict[str, object],
    ) -> dict[str, object]:
        captured["messages"] = messages
        captured["schema"] = schema
        return {"type": "final", "content": "done"}

    monkeypatch.setattr(zeta, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(zeta, "chat_json_messages", fake_chat_json_messages)

    action = zeta.next_model_action("repair", [], system="custom system")

    assert action == {"type": "final", "content": "done"}
    messages = cast(list[dict[str, object]], captured["messages"])
    system_prompt = str(messages[0]["content"])
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

    def fake_chat_json_messages(
        messages: list[dict[str, object]],
        schema: dict[str, object],
    ) -> dict[str, object]:
        captured["messages"] = messages
        captured["schema"] = schema
        return {
            "type": "tool_call",
            "name": "read",
            "input": {"path": "pyproject.toml"},
        }

    monkeypatch.setattr(zeta, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(zeta, "chat_json_messages", fake_chat_json_messages)

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
    messages = cast(list[dict[str, object]], captured["messages"])
    system_prompt = str(messages[0]["content"])
    assert '"type":"function"' in system_prompt
    assert '"name":"read"' in system_prompt
    assert '"name":"grep"' in system_prompt
    assert '"parameters":{"type":"object"' in system_prompt
    assert '"schema"' not in system_prompt
    assert '"name":"bash"' not in system_prompt
    user_prompt = str(messages[1]["content"])
    assert "Available tools" not in user_prompt
    assert '"name":"read"' not in user_prompt


def test_zeta_next_model_action_rejects_disallowed_tool(monkeypatch) -> None:
    def fake_chat_json_messages(
        messages: list[dict[str, object]],
        schema: dict[str, object],
    ) -> dict[str, object]:
        del messages, schema
        return {
            "type": "tool_call",
            "name": "bash",
            "input": {"command": "cat pyproject.toml"},
        }

    monkeypatch.setattr(zeta, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(zeta, "chat_json_messages", fake_chat_json_messages)

    action = zeta.next_model_action("inspect file", [], allowed_tools=("read", "grep"))

    assert action == {
        "type": "final",
        "content": "I could not choose a valid Zeta tool for the next step.",
    }


def test_zeta_next_model_action_sends_transcript_as_chat_messages(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_chat_json_messages(
        messages: list[dict[str, object]],
        schema: dict[str, object],
    ) -> dict[str, object]:
        del schema
        captured["messages"] = messages
        return {"type": "final", "content": "done"}

    monkeypatch.setattr(zeta, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(zeta, "chat_json_messages", fake_chat_json_messages)

    action = zeta.next_model_action(
        "summarize README",
        [
            {"type": "user_message", "content": "can you summarize the README"},
            {
                "type": "tool_call",
                "id": "call-1",
                "name": "read",
                "input": {"path": "README.md"},
            },
            {
                "type": "tool_result",
                "tool_call_id": "call-1",
                "name": "read",
                "result": {"ok": True, "content": [{"type": "text", "text": "docs"}]},
            },
            {"type": "assistant_message", "content": "summary"},
        ],
    )

    assert action == {"type": "final", "content": "done"}
    messages = cast(list[dict[str, Any]], captured["messages"])
    assert messages[2] == {
        "role": "user",
        "content": "can you summarize the README",
    }
    tool_call = messages[3]
    assert tool_call["role"] == "assistant"
    assert tool_call["content"] is None
    tool_calls = cast(list[dict[str, Any]], tool_call["tool_calls"])
    assert tool_calls[0]["id"] == "call-1"
    assert tool_calls[0]["function"]["name"] == "read"
    assert tool_calls[0]["function"]["arguments"] == '{"path":"README.md"}'
    assert messages[4]["role"] == "tool"
    assert messages[4]["tool_call_id"] == "call-1"
    assert '"docs"' in str(messages[4]["content"])
    assert messages[5] == {"role": "assistant", "content": "summary"}


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
                    "type": SHELL_PROMPT_HANDOFF_TYPE,
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
    assert event["result"]["schema"] == SHELL_HANDOFF_RESULT_SCHEMA
    assert event["result"]["type"] == SHELL_HANDOFF_RESULT_TYPE
    assert event["result"]["outcome"] == SHELL_HANDOFF_OUTCOME_EXECUTED
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
                    "type": SHELL_PROMPT_HANDOFF_TYPE,
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
    assert event["result"]["schema"] == SHELL_HANDOFF_RESULT_SCHEMA
    assert event["result"]["type"] == SHELL_HANDOFF_RESULT_TYPE
    assert event["result"]["outcome"] == SHELL_HANDOFF_OUTCOME_CANCELLED
    assert (
        event["result"]["cancellation_reason"]
        == SHELL_HANDOFF_CANCEL_EXPECTED_NOT_EXECUTED
    )
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
                    "type": SHELL_PROMPT_HANDOFF_TYPE,
                    "command": "uv run pytest",
                    "reason": "Run tests.",
                },
            },
        }
    )
    record_turn("git status --short", 0, "/repo", stdout_snippet=" M README.md")
    record_turn("uv run pytest", 0, "/repo", stdout_snippet="191 passed")

    event = sigil_handoff.append_shell_result()

    assert event["result"]["outcome"] == SHELL_HANDOFF_OUTCOME_EXECUTED
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
                    "type": SHELL_PROMPT_HANDOFF_TYPE,
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
    assert first["result"]["outcome"] == SHELL_HANDOFF_OUTCOME_EXECUTED
    assert second["type"] == "shell_resume"
    assert second["result"]["outcome"] == SHELL_HANDOFF_OUTCOME_NO_PENDING
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
    assert "\n\nIt contains project metadata.\n" in output
    assert "project metadata" in output
    assert len(transcripts) == 2


def test_zeta_question_loop_passes_follow_up_history_as_turns(
    monkeypatch,
) -> None:
    transcripts: list[list[dict[str, Any]]] = []

    def fake_next_model_action(
        objective: str,
        transcript: list[dict[str, Any]],
        **kwargs: object,
    ) -> dict[str, object]:
        del objective, kwargs
        transcripts.append(transcript)
        return {"type": "final", "content": "follow-up answer"}

    monkeypatch.setattr(answers_runner, "ensure_server", lambda: True)
    monkeypatch.setattr(
        answers_runner.runtime, "next_model_action", fake_next_model_action
    )

    code = answers_runner.run_tool_answer(
        "question system",
        "and why?",
        history=[
            {"role": "user", "content": "summarize README"},
            {"role": "assistant", "content": "It is a Sigil README."},
        ],
    )

    assert code == 0
    assert transcripts[0][:3] == [
        {"role": "user", "content": "summarize README"},
        {"role": "assistant", "content": "It is a Sigil README."},
        {
            "type": "user_message",
            "content": "and why?",
            "runtime": "zeta",
            "route": "answer",
            "system": "question system",
            "available_tools": ["read", "grep", "ls"],
        },
    ]


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
    assert "\n\nIt contains Sigil docs.\n" in output
    assert "It contains Sigil docs." in output
    assert "question tool budget" not in output
