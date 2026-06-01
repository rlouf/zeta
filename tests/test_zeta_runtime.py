from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from click.testing import CliRunner

from sigil.zeta import runner as zeta_runner
from sigil.zeta import runtime as zeta
from sigil.zeta.cli import cli


def test_zeta_tools_list_exposes_v1_builtins() -> None:
    result = CliRunner().invoke(cli, ["tools", "list", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    names = {tool["name"] for tool in data["tools"]}
    assert {"read", "grep", "bash", "edit", "write"} <= names
    assert data["tools"][0]["origin"] == "builtin"


def test_zeta_tool_read_schema_and_run(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("hello zeta\n", encoding="utf-8")

    schema = CliRunner().invoke(cli, ["tool", "read", "--schema"])
    assert schema.exit_code == 0
    assert json.loads(schema.output)["required"] == ["path"]

    result = CliRunner().invoke(
        cli,
        ["tool", "read"],
        input=json.dumps({"path": str(target)}),
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["content"][0]["text"] == "hello zeta\n"


def test_zeta_tool_bash_returns_handoff() -> None:
    result = CliRunner().invoke(
        cli,
        ["tool", "bash"],
        input=json.dumps({"command": "uv run pytest", "reason": "Run tests."}),
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["handoff"]["command"] == "uv run pytest"
    assert data["handoff"]["reason"] == "Run tests."


def test_zeta_tool_edit_writes_patch_artifact() -> None:
    patch = "--- a/a.txt\n+++ b/a.txt\n@@\n-old\n+new\n"
    result = CliRunner().invoke(
        cli,
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
        cli,
        ["transcript", "append"],
        input=json.dumps({"type": "tool_call", "name": "read"}),
    )
    assert appended.exit_code == 0

    tail = runner.invoke(cli, ["transcript", "tail", "--limit", "1"])
    assert tail.exit_code == 0
    data = json.loads(tail.output)
    assert data["events"][0]["type"] == "tool_call"
    assert data["events"][0]["name"] == "read"


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
    assert captured["system"] == "custom system"


def test_zeta_next_model_action_filters_available_tools(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_chat_json(
        system: str, user: str, schema: dict[str, object]
    ) -> dict[str, object]:
        del system
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
    user_prompt = str(captured["user"])
    assert '"type": "function"' in user_prompt
    assert '"name": "read"' in user_prompt
    assert '"name": "grep"' in user_prompt
    assert '"parameters": {"type": "object"' in user_prompt
    assert '"schema"' not in user_prompt
    assert '"name": "bash"' not in user_prompt


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

    monkeypatch.setattr(zeta_runner, "ensure_server", lambda: True)
    monkeypatch.setattr(
        zeta_runner.runtime, "next_model_action", fake_next_model_action
    )
    monkeypatch.setattr(
        zeta_runner.runtime,
        "run_tool",
        lambda name, params: {
            "ok": True,
            "content": [{"type": "text", "text": "[project]\nname = 'sigil'\n"}],
        },
    )

    code = zeta_runner.run_question_answer(
        "question system",
        "What does pyproject.toml contain?",
    )

    assert code == 0
    assert "project metadata" in capsys.readouterr().out
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

    monkeypatch.setattr(zeta_runner, "ensure_server", lambda: True)
    monkeypatch.setattr(
        zeta_runner.runtime, "next_model_action", fake_next_model_action
    )
    monkeypatch.setattr(
        zeta_runner.runtime,
        "run_tool",
        lambda name, params: {
            "ok": True,
            "content": [{"type": "text", "text": "Sigil docs"}],
        },
    )
    monkeypatch.setattr(
        zeta_runner,
        "chat_text",
        lambda system, prompt, max_tokens: "It contains Sigil docs.",
    )

    code = zeta_runner.run_question_answer(
        "question system",
        "What does README.md contain?",
        max_steps=1,
    )

    output = capsys.readouterr().out
    assert code == 0
    assert "It contains Sigil docs." in output
    assert "question tool budget" not in output
