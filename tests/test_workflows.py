"""Ask and step workflow tests, including shell handoff resolution."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from _zeta_helpers import (
    TtyBuffer,
    required_stream_sink,
    visible_terminal_text,
    write_models_config,
    write_skill,
)
from click.testing import CliRunner

import sigil.display.render as display_render
from sigil import agent_io
from sigil import handoff as sigil_handoff
from sigil.cli import cli as sigil_cli
from sigil.protocols import (
    SHELL_HANDOFF_CANCEL_EXPECTED_NOT_EXECUTED,
    SHELL_HANDOFF_OUTCOME_CANCELLED,
    SHELL_HANDOFF_OUTCOME_EXECUTED,
    SHELL_HANDOFF_OUTCOME_NO_PENDING,
    SHELL_HANDOFF_RESULT_SCHEMA,
    SHELL_HANDOFF_RESULT_TYPE,
    SHELL_PROMPT_HANDOFF_TYPE,
)
from sigil.session import read_event_log, recent_turns, record_turn
from sigil.state import read_jsonl
from sigil.workflows import ask as ask_runner
from sigil.workflows import step as zeta_runner
from sigil.zeta import agent as zeta_agent
from sigil.zeta import models as zeta_models
from sigil.zeta import timeline as zeta_timeline


def test_sigil_zeta_step_writes_handoff_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    handoff_file = tmp_path / "handoff.txt"

    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(
        zeta_runner,
        "run_agent_turn",
        lambda *args, **kwargs: zeta_agent.AgentTurnResult(
            events=[
                {
                    "type": "tool_call",
                    "id": "call-1",
                    "tool_call_id": "call-1",
                    "name": "bash",
                    "input": {"command": "uv run pytest", "reason": "Run tests."},
                },
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
                },
            ],
            handoff={
                "type": SHELL_PROMPT_HANDOFF_TYPE,
                "command": "uv run pytest",
                "reason": "Run tests.",
            },
        ),
    )

    result = CliRunner().invoke(
        sigil_cli,
        ["zeta-step", "--handoff-file", str(handoff_file), "repair"],
    )

    assert result.exit_code == 0
    assert "❯ bash   uv run pytest  (staged)" in result.output
    assert handoff_file.read_text(encoding="utf-8") == "uv run pytest\n"


def test_sigil_zeta_step_keeps_trace_off_stdout(monkeypatch) -> None:
    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(
        zeta_runner,
        "run_agent_turn",
        lambda *args, **kwargs: zeta_agent.AgentTurnResult(
            final_text="summary",
            events=[
                {
                    "type": "tool_call",
                    "tool_call_id": "call-1",
                    "name": "read",
                    "input": {"path": "README.md"},
                },
                {
                    "type": "tool_result",
                    "tool_call_id": "call-1",
                    "name": "read",
                    "result": {
                        "ok": True,
                        "content": [{"type": "text", "text": "a\n"}],
                    },
                },
                {"type": "assistant_message", "content": "summary"},
            ],
        ),
    )

    result = CliRunner().invoke(sigil_cli, ["zeta-step", "summarize"])

    assert result.exit_code == 0
    assert result.stdout == "\nsummary\n\n"
    assert "❯ read" in result.stderr
    assert "❯ read" not in result.stdout


def test_zeta_agent_step_separates_trace_from_final_answer(
    monkeypatch,
    capsys,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config
        captured["context"] = kwargs.get("context")
        return zeta_agent.AgentTurnResult(
            final_text="The answer.",
            events=[
                {
                    "type": "tool_call",
                    "tool_call_id": "call-1",
                    "name": "read",
                    "input": {"path": "README.md"},
                },
                {
                    "type": "tool_result",
                    "tool_call_id": "call-1",
                    "name": "read",
                    "result": {
                        "ok": True,
                        "content": [{"type": "text", "text": "a\n"}],
                    },
                },
                {"type": "assistant_message", "content": "The answer."},
            ],
        )

    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)
    monkeypatch.setattr(zeta_runner, "load_project_context", lambda: "ctx")

    code = zeta_runner.run_agent_step("answer me", glyph=",,")

    assert code == 0
    output = capsys.readouterr()
    assert output.out.count("The answer.") == 1
    assert "❯" not in output.out
    assert "❯ read" in output.err
    assert captured["context"] == "ctx"


def test_zeta_agent_step_renders_context_usage_on_trace_stream(
    monkeypatch,
    capsys,
) -> None:
    telemetry = {
        "usage": {
            "prompt_tokens": 18_432,
            "completion_tokens": 391,
            "total_tokens": 18_823,
        },
        "model_context_tokens": 262_144,
    }

    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config, kwargs
        return zeta_agent.AgentTurnResult(
            final_text="done",
            model_telemetry=telemetry,
        )

    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)

    code = zeta_runner.run_agent_step("answer me", glyph=",,")

    assert code == 0
    output = capsys.readouterr()
    assert output.out.count("done") == 1
    assert "context  [" not in output.out
    assert "context  [" in output.err
    assert "7%" in output.err
    assert "18,823 / 262,144 tokens" not in output.err


def test_zeta_agent_step_renders_context_usage_after_buffered_answer(
    monkeypatch,
    capsys,
) -> None:
    telemetry = {
        "usage": {"prompt_tokens": 18_432, "completion_tokens": 391},
        "model_context_tokens": 262_144,
    }

    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config, kwargs
        return zeta_agent.AgentTurnResult(
            final_text="done",
            model_telemetry=telemetry,
        )

    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)

    code = zeta_runner.run_agent_step("answer me", glyph=",,", trace_output=sys.stdout)

    assert code == 0
    output = capsys.readouterr().out
    assert output.index("done") < output.index("context  [")


def test_zeta_agent_step_renders_context_usage_at_bottom_after_tools(
    monkeypatch,
    capsys,
) -> None:
    tool_telemetry = {
        "usage": {"prompt_tokens": 123, "completion_tokens": 4},
        "model_context_tokens": 262_144,
    }

    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config
        event_sink = cast("Callable[[dict[str, Any]], None]", kwargs.get("event_sink"))
        first_call = {
            "type": "tool_call",
            "id": "call-1",
            "tool_call_id": "call-1",
            "name": "read",
            "input": {"path": "a.md"},
        }
        first_result = {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "name": "read",
            "result": {
                "ok": True,
                "content": [{"type": "text", "text": "A\n"}],
            },
        }
        second_call = {
            "type": "tool_call",
            "id": "call-2",
            "tool_call_id": "call-2",
            "name": "read",
            "input": {"path": "b.md"},
        }
        second_result = {
            "type": "tool_result",
            "tool_call_id": "call-2",
            "name": "read",
            "result": {
                "ok": True,
                "content": [{"type": "text", "text": "B\n"}],
            },
            "model_telemetry": tool_telemetry,
        }
        events = [first_call, first_result, second_call, second_result]
        for event in events:
            event_sink(event)
        return zeta_agent.AgentTurnResult(
            final_text="done",
            events=events,
            model_telemetry={
                "usage": {"prompt_tokens": 456, "completion_tokens": 4},
                "model_context_tokens": 262_144,
            },
        )

    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)

    code = zeta_runner.run_agent_step(
        "inspect",
        glyph=",,",
        trace_output=sys.stdout,
    )

    assert code == 0
    output = capsys.readouterr().out
    assert ("❯ read   a.md  (1 lines)\n❯ read   b.md  (1 lines)") in output
    assert output.count("context  [") == 1
    assert "123 / 262,144 tokens" not in output
    assert output.index("done") < output.index("context  [░░░░░░░░░░░░░░░░░░░░] 0%")


def test_zeta_agent_step_does_not_pass_current_user_event_as_transcript(
    monkeypatch,
    capsys,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, config, kwargs
        captured["transcript"] = transcript
        return zeta_agent.AgentTurnResult(final_text="done")

    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)

    code = zeta_runner.run_agent_step("answer me", glyph=",,")

    assert code == 0
    assert cast(list[dict[str, Any]], captured["transcript"]) == []
    assert zeta_timeline.current_timeline()[-1]["type"] == "user_message"
    assert capsys.readouterr().out.count("done") == 1


def test_zeta_agent_step_double_comma_uses_handoff_mode(
    monkeypatch,
    capsys,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, kwargs
        captured["config"] = config
        return zeta_agent.AgentTurnResult(final_text="done")

    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)

    code = zeta_runner.run_agent_step("review", glyph=",,")

    assert code == 0
    config = cast(zeta_agent.AgentConfig, captured["config"])
    assert config.edit_mode == "review_patch"
    assert config.execution_mode == "handoff"
    assert config.max_turns is None


def test_zeta_ask_workflow_has_no_default_step_budget(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, kwargs
        captured["config"] = config
        return zeta_agent.AgentTurnResult(final_text="done")

    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(ask_runner, "run_agent_turn", fake_run_agent_turn)

    code = ask_runner.run_tool_ask("system", "question")

    assert code == 0
    config = cast(zeta_agent.AgentConfig, captured["config"])
    assert config.max_turns is None


def test_zeta_agent_step_double_comma_stages_bash_handoff(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    handoff_file = tmp_path / "handoff.txt"

    responses = iter(
        [
            {
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "bash",
                            "arguments": '{"command":"echo Review complete"}',
                        },
                    }
                ]
            }
        ]
    )

    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        lambda *args, **kwargs: next(responses),
    )

    code = zeta_runner.run_agent_step(
        "Review the changes",
        glyph=",,",
        allowed_tools=("bash",),
        handoff_path=handoff_file,
        handoff_output="summary",
    )

    assert code == 0
    output = capsys.readouterr()
    assert "(staged)" in output.err
    assert "exit 0" not in output.err
    assert "Review complete" not in output.out
    assert handoff_file.read_text(encoding="utf-8") == "echo Review complete\n"


def test_zeta_agent_step_prints_tool_start_while_agent_runs(
    monkeypatch,
    capsys,
) -> None:
    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config
        event_sink = cast("Callable[[dict[str, Any]], None]", kwargs.get("event_sink"))
        assert callable(event_sink)
        tool_call = {
            "type": "tool_call",
            "id": "call-1",
            "tool_call_id": "call-1",
            "name": "read",
            "input": {"path": "README.md"},
        }
        event_sink(tool_call)
        assert "❯ read   README.md" in capsys.readouterr().err
        tool_result = {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "name": "read",
            "result": {
                "ok": True,
                "content": [{"type": "text", "text": "README"}],
            },
        }
        event_sink(tool_result)
        return zeta_agent.AgentTurnResult(
            final_text="It is a README.",
            events=[tool_call, tool_result],
        )

    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)

    for glyph in (",,", ",,,"):
        code = zeta_runner.run_agent_step("inspect", glyph=glyph)

        assert code == 0
        assert capsys.readouterr().out.count("It is a README.") == 1


def test_zeta_agent_step_streams_text_before_tool_trace(
    monkeypatch,
    capsys,
) -> None:
    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config
        stream_sink = required_stream_sink(kwargs)
        stream_sink.content_delta("I'll inspect README.")
        event_sink = cast("Callable[[dict[str, Any]], None]", kwargs.get("event_sink"))
        tool_call = {
            "type": "tool_call",
            "id": "call-1",
            "tool_call_id": "call-1",
            "name": "read",
            "input": {"path": "README.md"},
        }
        event_sink(tool_call)
        return zeta_agent.AgentTurnResult(
            final_text="It is a README.",
            events=[tool_call],
        )

    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)

    code = zeta_runner.run_agent_step("inspect", glyph=",,")

    assert code == 0
    output = capsys.readouterr()
    assert output.out.startswith("\nI'll inspect README.\n\n")
    assert "\nIt is a README.\n" in output.out
    assert "❯ read   README.md" in output.err


@pytest.mark.parametrize("glyph", [",,", ",,,"])
def test_zeta_agent_step_separates_tool_result_from_later_streamed_text(
    glyph: str,
    monkeypatch,
    capsys,
) -> None:
    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config
        stream_sink = required_stream_sink(kwargs)
        stream_sink.content_delta("I'll inspect README.")
        event_sink = cast("Callable[[dict[str, Any]], None]", kwargs.get("event_sink"))
        tool_call = {
            "type": "tool_call",
            "id": "call-1",
            "tool_call_id": "call-1",
            "name": "read",
            "input": {"path": "README.md"},
        }
        tool_result = {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "name": "read",
            "result": {
                "ok": True,
                "content": [{"type": "text", "text": "README\n"}],
            },
        }
        event_sink(tool_call)
        event_sink(tool_result)
        stream_sink.content_delta("It is a README.")
        return zeta_agent.AgentTurnResult(
            final_text="It is a README.",
            events=[tool_call, tool_result],
            final_text_streamed=True,
        )

    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)

    code = zeta_runner.run_agent_step(
        "inspect",
        glyph=glyph,
        trace_output=sys.stdout,
    )

    assert code == 0
    output = capsys.readouterr().out
    assert output.index("I'll inspect README.") < output.index("❯ read   README.md")
    assert output.index("❯ read   README.md") < output.index("It is a README.")


def test_zeta_agent_step_does_not_insert_blank_lines_between_tool_calls(
    monkeypatch,
    capsys,
) -> None:
    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config
        event_sink = cast("Callable[[dict[str, Any]], None]", kwargs.get("event_sink"))
        events = [
            {
                "type": "tool_call",
                "id": "call-1",
                "tool_call_id": "call-1",
                "name": "read",
                "input": {"path": "a.md"},
            },
            {
                "type": "tool_result",
                "tool_call_id": "call-1",
                "name": "read",
                "result": {
                    "ok": True,
                    "content": [{"type": "text", "text": "A\n"}],
                },
            },
            {
                "type": "tool_call",
                "id": "call-2",
                "tool_call_id": "call-2",
                "name": "read",
                "input": {"path": "b.md"},
            },
            {
                "type": "tool_result",
                "tool_call_id": "call-2",
                "name": "read",
                "result": {
                    "ok": True,
                    "content": [{"type": "text", "text": "B\n"}],
                },
            },
        ]
        for event in events:
            event_sink(event)
        return zeta_agent.AgentTurnResult(final_text="Done.", events=events)

    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)

    code = zeta_runner.run_agent_step(
        "inspect",
        glyph=",,",
        trace_output=sys.stdout,
    )

    assert code == 0
    output = capsys.readouterr().out
    assert "❯ read   a.md  (1 lines)\n❯ read   b.md  (1 lines)" in output
    assert output.count("Done.") == 1


def test_zeta_agent_step_aligns_thinking_status_after_tool_trace(
    monkeypatch,
    capsys,
) -> None:
    output = TtyBuffer()

    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config
        event_sink = cast("Callable[[dict[str, Any]], None]", kwargs.get("event_sink"))
        tool_call = {
            "type": "tool_call",
            "id": "call-1",
            "tool_call_id": "call-1",
            "name": "read",
            "input": {"path": "README.md"},
        }
        tool_result = {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "name": "read",
            "result": {
                "ok": True,
                "content": [{"type": "text", "text": "README\n"}],
            },
        }
        event_sink(tool_call)
        event_sink(tool_result)
        model_status = cast("Callable[[], Any]", kwargs.get("model_status"))
        with model_status():
            pass
        return zeta_agent.AgentTurnResult(final_text="Done.", events=[])

    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)

    code = zeta_runner.run_agent_step(
        "inspect",
        glyph=",,",
        trace_output=output,
    )

    assert code == 0
    out_text = capsys.readouterr().out
    assert out_text.count("Done.") == 1
    assert "❯" not in out_text
    trace_text = visible_terminal_text(output.getvalue())
    assert "❯ read   README.md  (1 lines)\n\n  thinking 0s" in trace_text


def test_zeta_agent_step_prints_final_answer_after_direct_edit(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(
        zeta_runner,
        "run_agent_turn",
        lambda *args, **kwargs: zeta_agent.AgentTurnResult(
            final_text="edited and verified",
            events=[
                {
                    "type": "tool_call",
                    "id": "call-1",
                    "tool_call_id": "call-1",
                    "name": "edit",
                    "input": {"location": "a.txt", "old": "old", "new": "new"},
                },
                {
                    "type": "tool_result",
                    "tool_call_id": "call-1",
                    "name": "edit",
                    "result": {
                        "ok": True,
                        "metadata": {"mode": "direct_replace", "location": "a.txt"},
                    },
                },
                {"type": "assistant_message", "content": "edited and verified"},
            ],
        ),
    )

    code = zeta_runner.run_agent_step("edit", glyph=",,,")

    assert code == 0
    output = capsys.readouterr()
    assert output.out.count("edited and verified") == 1
    assert "❯" not in output.out
    assert "❯ edit   a.txt  (applied · a.txt)" in output.err


def test_sigil_handoff_shell_turn_records_recent_turn(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "zeta-test")

    result = CliRunner().invoke(
        sigil_cli,
        [
            "handoff",
            "shell-turn",
            "--command",
            "uv run pytest",
            "--status",
            "1",
            "--cwd",
            "/repo",
        ],
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


def test_zeta_step_glyph_selects_edit_mode() -> None:
    assert zeta_runner.edit_mode_for_glyph(",,") == "review_patch"
    assert zeta_runner.edit_mode_for_glyph(",,,") == "direct_replace"
    assert zeta_runner.execution_mode_for_glyph(",,") == "handoff"
    assert zeta_runner.execution_mode_for_glyph(",,,") == "direct"


def test_zeta_skill_directive_expands_through_agent_step_workflow(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "repo"
    project.mkdir()
    write_skill(
        project / ".agents" / "skills",
        "step-skill",
        description="Step work.",
        body="Step skill body.\n",
    )
    captured: dict[str, str] = {}

    def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        **kwargs: object,
    ) -> dict[str, Any]:
        del kwargs
        captured["user"] = str(messages[1]["content"])
        return {"content": "done"}

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(project)
    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        fake_chat_completion_messages,
    )

    code = zeta_runner.run_agent_step("@step-skill: do step work", glyph=",,")

    assert code == 0
    assert '<skill name="step-skill"' in captured["user"]
    assert "Step skill body." in captured["user"]
    assert "do step work" in captured["user"]


def test_zeta_agent_step_workflow_uses_active_session_model(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    write_models_config(
        home,
        """
[[models]]
name = "coder"
model = "coder-model"
url = "http://127.0.0.1:8082/v1/chat/completions"
""",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SIGIL_SESSION_ID", "agent-model")
    zeta_models.set_active_model_profile("coder")
    captured: dict[str, Any] = {}

    def fake_ensure_server(**kwargs: object) -> bool:
        captured["server"] = kwargs
        return True

    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, kwargs
        captured["config"] = config
        return zeta_agent.AgentTurnResult(final_text="done")

    monkeypatch.setattr(agent_io, "ensure_server", fake_ensure_server)
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)

    code = zeta_runner.run_agent_step("do work", glyph=",,")

    assert code == 0
    assert capsys.readouterr().out.count("done") == 1
    assert captured["server"] == {
        "selected_url": "http://127.0.0.1:8082/v1/chat/completions",
        "selected_model": "coder-model",
    }
    config = cast(zeta_agent.AgentConfig, captured["config"])
    assert config.model_profile == "coder"
    assert config.model_name == "coder-model"
    assert config.model_url == "http://127.0.0.1:8082/v1/chat/completions"


def test_zeta_skill_directive_expands_through_ask_workflow(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "repo"
    project.mkdir()
    write_skill(
        project / ".agents" / "skills",
        "answer-skill",
        description="Answer work.",
        body="Answer skill body.\n",
    )
    captured: dict[str, str] = {}

    def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        **kwargs: object,
    ) -> dict[str, Any]:
        del kwargs
        captured["user"] = str(messages[1]["content"])
        return {"content": "answered"}

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(project)
    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        fake_chat_completion_messages,
    )

    code = ask_runner.run_tool_ask(
        "system",
        "@answer-skill: do answer work",
        input_text="@answer-skill: do answer work",
    )

    assert code == 0
    assert '<skill name="answer-skill"' in captured["user"]
    assert "Answer skill body." in captured["user"]
    assert "do answer work" in captured["user"]


def test_zeta_ask_workflow_uses_active_session_model(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    write_models_config(
        home,
        """
[[models]]
name = "fast"
model = "fast-model"
url = "http://127.0.0.1:8081/v1/chat/completions"
""",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SIGIL_SESSION_ID", "answer-model")
    zeta_models.set_active_model_profile("fast")
    captured: dict[str, Any] = {}

    def fake_ensure_server(**kwargs: object) -> bool:
        captured["server"] = kwargs
        return True

    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, kwargs
        captured["transcript"] = transcript
        captured["config"] = config
        return zeta_agent.AgentTurnResult(final_text="answered")

    monkeypatch.setattr(agent_io, "ensure_server", fake_ensure_server)
    monkeypatch.setattr(ask_runner, "run_agent_turn", fake_run_agent_turn)

    code = ask_runner.run_tool_ask("system", "prompt")

    assert code == 0
    assert captured["server"] == {
        "selected_url": "http://127.0.0.1:8081/v1/chat/completions",
        "selected_model": "fast-model",
    }
    config = cast(zeta_agent.AgentConfig, captured["config"])
    assert config.model_profile == "fast"
    assert config.model_name == "fast-model"
    assert config.model_url == "http://127.0.0.1:8081/v1/chat/completions"
    transcript = cast(list[dict[str, Any]], captured["transcript"])
    assert transcript == []
    assert zeta_timeline.current_timeline()[-1]["model"] == {
        "profile": "fast",
        "model": "fast-model",
        "url": "http://127.0.0.1:8081/v1/chat/completions",
    }


def test_append_shell_result_appends_tool_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "zeta-test")
    zeta_timeline.record_event(
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

    event = sigil_handoff.append_shell_result()

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


def test_resolved_shell_handoff_context_keeps_tool_call_with_shell_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "zeta-test")
    zeta_timeline.record_event(
        {
            "type": "assistant_message",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "bash",
                        "arguments": '{"command":"uv run pytest"}',
                    },
                }
            ],
        }
    )
    zeta_timeline.record_event(
        {
            "type": "tool_call",
            "id": "call-1",
            "tool_call_id": "call-1",
            "name": "bash",
            "input": {"command": "uv run pytest"},
        }
    )
    zeta_timeline.record_event(
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

    sigil_handoff.append_shell_result()
    messages = zeta_timeline.chat_messages(zeta_timeline.current_timeline())

    assert messages[0]["role"] == "assistant"
    assert messages[0]["tool_calls"][0]["id"] == "call-1"
    tool_messages = [message for message in messages if message["role"] == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "call-1"
    tool_content = json.loads(tool_messages[0]["content"])
    assert tool_content["type"] == SHELL_HANDOFF_RESULT_TYPE
    assert tool_content["executed_command"] == "uv run pytest"


def test_sigil_transcript_shell_result_reports_extended_handoff_as_edited(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "zeta-test")
    zeta_timeline.record_event(
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
    assert event["result"]["ok"] is True
    assert event["result"]["outcome"] == SHELL_HANDOFF_OUTCOME_EXECUTED
    assert event["result"]["edited"] is True
    assert event["result"]["expected_command"] == "uv run pytest"
    assert event["result"]["executed_command"] == "uv run pytest -q"
    assert "edited" in event["result"]["content"][0]["text"]


def test_sigil_transcript_shell_result_matches_despite_whitespace_edits(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "zeta-test")
    zeta_timeline.record_event(
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
    record_turn("uv  run   pytest ", 0, "/repo", stdout_snippet="191 passed")

    event = sigil_handoff.append_shell_result()

    assert event["result"]["outcome"] == SHELL_HANDOFF_OUTCOME_EXECUTED
    assert event["result"]["edited"] is False
    assert event["result"]["executed_command"] == "uv  run   pytest "


def test_sigil_transcript_shell_result_cancels_unrelated_command(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "zeta-test")
    zeta_timeline.record_event(
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
    record_turn("git status --short", 0, "/repo")

    event = sigil_handoff.append_shell_result()

    assert event["result"]["ok"] is False
    assert event["result"]["schema"] == SHELL_HANDOFF_RESULT_SCHEMA
    assert event["result"]["type"] == SHELL_HANDOFF_RESULT_TYPE
    assert event["result"]["outcome"] == SHELL_HANDOFF_OUTCOME_CANCELLED
    assert (
        event["result"]["cancellation_reason"]
        == SHELL_HANDOFF_CANCEL_EXPECTED_NOT_EXECUTED
    )
    assert event["result"]["expected_command"] == "uv run pytest"
    assert event["result"]["actual_command"] == "git status --short"
    assert event["result"]["shell_turns"][0]["command"] == "git status --short"


def test_sigil_transcript_shell_result_includes_intervening_shell_turns(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "zeta-test")
    zeta_timeline.record_event(
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
    zeta_timeline.record_event(
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

    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, config, kwargs
        transcripts.append(transcript)
        return zeta_agent.AgentTurnResult(
            final_text="It contains project metadata.",
            events=[
                {
                    "type": "assistant_message",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "read",
                                "arguments": '{"path":"pyproject.toml"}',
                            },
                        }
                    ],
                },
                {
                    "type": "tool_call",
                    "id": "call-1",
                    "tool_call_id": "call-1",
                    "name": "read",
                    "input": {"path": "pyproject.toml"},
                },
                {
                    "type": "tool_result",
                    "tool_call_id": "call-1",
                    "name": "read",
                    "result": {
                        "ok": True,
                        "content": [
                            {"type": "text", "text": "[project]\nname = 'sigil'\n"}
                        ],
                    },
                },
                {
                    "type": "assistant_message",
                    "content": "It contains project metadata.",
                },
            ],
        )

    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(ask_runner, "run_agent_turn", fake_run_agent_turn)

    code = ask_runner.run_tool_ask(
        "question system",
        "What does pyproject.toml contain?",
    )

    assert code == 0
    output = capsys.readouterr()
    assert "❯ read   pyproject.toml" in output.err
    assert "It contains project metadata." in output.out
    assert "❯" not in output.out
    assert len(transcripts) == 1


def test_zeta_ask_workflow_prints_context_usage_and_records_telemetry(
    monkeypatch,
    capsys,
) -> None:
    telemetry = {
        "usage": {
            "prompt_tokens": 18_432,
            "completion_tokens": 391,
            "total_tokens": 18_823,
        },
        "model_context_tokens": 262_144,
    }

    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config, kwargs
        return zeta_agent.AgentTurnResult(
            final_text="It contains project metadata.",
            model_telemetry=telemetry,
        )

    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(ask_runner, "run_agent_turn", fake_run_agent_turn)

    code = ask_runner.run_tool_ask(
        "question system",
        "What does pyproject.toml contain?",
    )

    assert code == 0
    output = capsys.readouterr()
    assert "It contains project metadata." in output.out
    assert "context  [" in output.err
    assert "7%" in output.err
    assert "context  [" not in output.out
    assert "18,823 / 262,144 tokens" not in output.err
    answer_event = read_event_log()[-1]
    assert answer_event["usage"] == telemetry["usage"]
    assert answer_event["model_context_tokens"] == 262_144
    assert read_jsonl("last-tools.jsonl") == []


def test_zeta_ask_workflow_json_includes_context_telemetry(
    monkeypatch,
    capsys,
) -> None:
    telemetry = {
        "usage": {
            "prompt_tokens": 123,
            "completion_tokens": 4,
            "total_tokens": 127,
        },
        "model_context_tokens": 262_144,
    }

    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config, kwargs
        return zeta_agent.AgentTurnResult(
            final_text="buffered answer",
            model_telemetry=telemetry,
        )

    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(ask_runner, "run_agent_turn", fake_run_agent_turn)

    code = ask_runner.run_tool_ask(
        "question system",
        "Question?",
        json_output=True,
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["answer"] == "buffered answer"
    assert payload["usage"] == telemetry["usage"]
    assert payload["model_context_tokens"] == 262_144
    assert payload["tools"] == []


def test_zeta_ask_workflow_streams_final_text_without_duplicate(
    monkeypatch,
    capsys,
) -> None:
    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config
        stream_sink = required_stream_sink(kwargs)
        assert isinstance(stream_sink, display_render.TraceAwareStreamRenderer)
        assert isinstance(stream_sink.renderer, display_render.TerminalStreamRenderer)
        stream_sink.content_delta("streamed answer")
        return zeta_agent.AgentTurnResult(
            final_text="streamed answer",
            events=[{"type": "assistant_message", "content": "streamed answer"}],
            final_text_streamed=True,
        )

    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(ask_runner, "run_agent_turn", fake_run_agent_turn)

    code = ask_runner.run_tool_ask("question system", "Question?")

    assert code == 0
    assert capsys.readouterr().out.count("streamed answer") == 1


def test_zeta_ask_workflow_streams_markdown_with_rich_for_tty(
    monkeypatch,
) -> None:
    output = TtyBuffer()

    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config
        stream_sink = required_stream_sink(kwargs)
        assert isinstance(stream_sink, display_render.TraceAwareStreamRenderer)
        assert isinstance(stream_sink.renderer, display_render.RichStreamRenderer)
        stream_sink.content_delta("**streamed** answer")
        return zeta_agent.AgentTurnResult(
            final_text="streamed answer",
            events=[{"type": "assistant_message", "content": "streamed answer"}],
            final_text_streamed=True,
        )

    monkeypatch.setattr(ask_runner.sys, "stdout", output)
    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(ask_runner, "run_agent_turn", fake_run_agent_turn)

    code = ask_runner.run_tool_ask(
        "question system",
        "Question?",
        input_text="Question?",
    )

    assert code == 0
    assert "streamed answer" in visible_terminal_text(output.getvalue())
    timeline = zeta_timeline.current_timeline()
    assert timeline[-1]["type"] == "assistant_message"
    assert timeline[-1]["content"] == "streamed answer"


def test_zeta_ask_workflow_streams_text_before_tool_trace(
    monkeypatch,
    capsys,
) -> None:
    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config
        stream_sink = required_stream_sink(kwargs)
        stream_sink.content_delta("I'll inspect README.")
        event_sink = cast("Callable[[dict[str, Any]], None]", kwargs.get("event_sink"))
        tool_call = {
            "type": "tool_call",
            "id": "call-1",
            "tool_call_id": "call-1",
            "name": "read",
            "input": {"path": "README.md"},
        }
        tool_result = {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "name": "read",
            "result": {
                "ok": True,
                "content": [{"type": "text", "text": "README"}],
            },
        }
        event_sink(tool_call)
        event_sink(tool_result)
        stream_sink.content_delta("It is a README.")
        return zeta_agent.AgentTurnResult(
            final_text="It is a README.",
            events=[tool_call, tool_result],
            final_text_streamed=True,
        )

    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(ask_runner, "run_agent_turn", fake_run_agent_turn)

    code = ask_runner.run_tool_ask("question system", "Question?")

    assert code == 0
    output = capsys.readouterr()
    assert "I'll inspect README." in output.out
    assert "It is a README." in output.out
    assert "❯ read   README.md  (1 lines)" in output.err
    assert "❯" not in output.out
    assert '{"path"' not in output.out
    assert '{"path"' not in output.err


def test_zeta_ask_workflow_renders_context_usage_at_bottom_after_tools(
    monkeypatch,
    capsys,
) -> None:
    telemetry = {
        "usage": {
            "prompt_tokens": 18_432,
            "completion_tokens": 391,
            "total_tokens": 18_823,
        },
        "model_context_tokens": 262_144,
    }

    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config
        event_sink = cast("Callable[[dict[str, Any]], None]", kwargs.get("event_sink"))
        first_call = {
            "type": "tool_call",
            "id": "call-1",
            "tool_call_id": "call-1",
            "name": "read",
            "input": {"path": "a.md"},
        }
        first_result = {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "name": "read",
            "result": {
                "ok": True,
                "content": [{"type": "text", "text": "A\n"}],
            },
        }
        second_call = {
            "type": "tool_call",
            "id": "call-2",
            "tool_call_id": "call-2",
            "name": "read",
            "input": {"path": "b.md"},
        }
        second_result = {
            "type": "tool_result",
            "tool_call_id": "call-2",
            "name": "read",
            "result": {
                "ok": True,
                "content": [{"type": "text", "text": "B\n"}],
            },
            "model_telemetry": telemetry,
        }
        events = [first_call, first_result, second_call, second_result]
        for event in events:
            event_sink(event)
        return zeta_agent.AgentTurnResult(
            final_text="It is a README.",
            events=events,
            model_telemetry=telemetry,
        )

    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(ask_runner, "run_agent_turn", fake_run_agent_turn)

    code = ask_runner.run_tool_ask("question system", "Question?")

    assert code == 0
    output = capsys.readouterr()
    assert ("❯ read   a.md  (1 lines)\n❯ read   b.md  (1 lines)") in output.err
    assert output.err.count("context  [") == 1
    assert "It is a README." in output.out
    assert "context  [" not in output.out
    assert output.err.index("❯ read   b.md") < output.err.index("context  [")
    tools = read_jsonl("last-tools.jsonl")
    assert [(tool["type"], tool["tool"]) for tool in tools] == [
        ("tool_start", "read"),
        ("tool_end", "read"),
        ("tool_start", "read"),
        ("tool_end", "read"),
    ]


def test_zeta_ask_workflow_json_output_disables_live_streaming(
    monkeypatch,
    capsys,
) -> None:
    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config
        assert kwargs.get("stream_sink") is None
        return zeta_agent.AgentTurnResult(final_text="buffered answer")

    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(ask_runner, "run_agent_turn", fake_run_agent_turn)

    code = ask_runner.run_tool_ask(
        "question system",
        "Question?",
        json_output=True,
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["answer"] == "buffered answer"


def test_zeta_question_loop_prints_tool_start_while_agent_runs(
    monkeypatch,
    capsys,
) -> None:
    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config
        event_sink = cast("Callable[[dict[str, Any]], None]", kwargs.get("event_sink"))
        assert callable(event_sink)
        tool_call = {
            "type": "tool_call",
            "id": "call-1",
            "tool_call_id": "call-1",
            "name": "read",
            "input": {"path": "README.md"},
        }
        event_sink(tool_call)
        assert "❯ read   README.md" in capsys.readouterr().err
        tool_result = {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "name": "read",
            "result": {
                "ok": True,
                "content": [{"type": "text", "text": "README"}],
            },
        }
        event_sink(tool_result)
        return zeta_agent.AgentTurnResult(
            final_text="It is a README.",
            events=[tool_call, tool_result],
        )

    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(ask_runner, "run_agent_turn", fake_run_agent_turn)

    code = ask_runner.run_tool_ask(
        "question system",
        "What does README.md contain?",
    )

    assert code == 0
    assert "\nIt is a README.\n" in capsys.readouterr().out


def test_zeta_question_loop_passes_prior_timeline_as_turns(
    monkeypatch,
) -> None:
    transcripts: list[list[dict[str, Any]]] = []
    captured: dict[str, object] = {}

    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, config
        transcripts.append(transcript)
        captured["context"] = kwargs.get("context")
        return zeta_agent.AgentTurnResult(
            final_text="follow-up answer",
            events=[{"type": "assistant_message", "content": "follow-up answer"}],
        )

    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(ask_runner, "run_agent_turn", fake_run_agent_turn)
    monkeypatch.setattr(ask_runner, "load_project_context", lambda: "ctx")

    zeta_timeline.record_event({"type": "user_message", "content": "summarize README"})
    zeta_timeline.record_event(
        {"type": "assistant_message", "content": "It is a Sigil README."}
    )

    code = ask_runner.run_tool_ask(
        "question system",
        "and why?",
    )

    assert code == 0
    contents = [str(event.get("content") or "") for event in transcripts[0]]
    assert contents == ["summarize README", "It is a Sigil README."]
    assert captured["context"] == "ctx"
    timeline = zeta_timeline.current_timeline()
    assert [event["content"] for event in timeline[-2:]] == [
        "and why?",
        "follow-up answer",
    ]


def test_zeta_question_loop_falls_back_instead_of_budget_message(
    monkeypatch,
    capsys,
) -> None:
    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config, kwargs
        return zeta_agent.AgentTurnResult(
            events=[
                {
                    "type": "tool_call",
                    "id": "call-1",
                    "tool_call_id": "call-1",
                    "name": "read",
                    "input": {"path": "README.md"},
                },
                {
                    "type": "tool_result",
                    "tool_call_id": "call-1",
                    "name": "read",
                    "result": {
                        "ok": True,
                        "content": [{"type": "text", "text": "Sigil docs"}],
                    },
                },
            ]
        )

    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(ask_runner, "run_agent_turn", fake_run_agent_turn)

    def fake_chat_text(
        system: str,
        prompt: str,
        *,
        max_tokens: int = 8192,
        stream_sink: object | None = None,
        telemetry_sink: object | None = None,
    ) -> str:
        del system, prompt, max_tokens, stream_sink, telemetry_sink
        return "It contains Sigil docs."

    monkeypatch.setattr(ask_runner, "chat_text", fake_chat_text)

    code = ask_runner.run_tool_ask(
        "question system",
        "What does README.md contain?",
        max_steps=1,
    )

    output = capsys.readouterr().out
    assert code == 0
    assert "\nIt contains Sigil docs.\n" in output
    assert "It contains Sigil docs." in output
    assert "question tool budget" not in output
    timeline = zeta_timeline.current_timeline()
    assert timeline[-1]["type"] == "assistant_message"
    assert timeline[-1]["content"] == "It contains Sigil docs."


def test_zeta_answer_fallback_formats_evidence_instead_of_raw_json(
    monkeypatch,
) -> None:
    captured: dict[str, str] = {}

    def fake_chat_text(
        system: str,
        prompt: str,
        *,
        max_tokens: int = 8192,
        stream_sink: object | None = None,
        telemetry_sink: object | None = None,
    ) -> str:
        del system, max_tokens, stream_sink, telemetry_sink
        captured["prompt"] = prompt
        return "Use a clearer decision index."

    monkeypatch.setattr(ask_runner, "chat_text", fake_chat_text)

    answer = ask_runner.fallback_answer(
        "question system",
        "How would you improve it?",
        [
            {"type": "user_message", "content": "What is this vault about?"},
            {"type": "assistant_message", "content": "It is a CEO vault."},
        ],
        [
            {
                "type": "tool_result",
                "tool_call_id": "call-1",
                "name": "read",
                "result": {
                    "ok": True,
                    "content": [{"type": "text", "text": "Decision log"}],
                    "metadata": {"path": "/vault/DECISIONS.md"},
                },
            },
        ],
    )

    assert answer == "Use a clearer decision index."
    prompt = captured["prompt"]
    assert "Current question:\nHow would you improve it?" in prompt
    assert "Prior conversation:\nuser: What is this vault about?" in prompt
    assert "assistant: It is a CEO vault." in prompt
    assert "Tool result (read /vault/DECISIONS.md):\nDecision log" in prompt
    assert "Current turn transcript JSON" not in prompt


def test_zeta_answer_fallback_uses_active_session_model(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    write_models_config(
        home,
        """
[[models]]
name = "fast"
model = "fast-model"
url = "http://127.0.0.1:8081/v1/chat/completions"
""",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SIGIL_SESSION_ID", "fallback-model")
    zeta_models.set_active_model_profile("fast")
    captured: dict[str, Any] = {}

    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config, kwargs
        return zeta_agent.AgentTurnResult(events=[])

    def fake_chat_text(
        system: str,
        prompt: str,
        *,
        max_tokens: int = 8192,
        selected_model: str | None = None,
        selected_url: str | None = None,
        stream_sink: object | None = None,
        telemetry_sink: object | None = None,
    ) -> str:
        del system, prompt, max_tokens, stream_sink, telemetry_sink
        captured["selected_model"] = selected_model
        captured["selected_url"] = selected_url
        return "Fallback answer."

    monkeypatch.setattr(agent_io, "ensure_server", lambda **kwargs: True)
    monkeypatch.setattr(ask_runner, "run_agent_turn", fake_run_agent_turn)
    monkeypatch.setattr(ask_runner, "chat_text", fake_chat_text)

    code = ask_runner.run_tool_ask("question system", "Question?", max_steps=1)

    output = capsys.readouterr().out
    assert code == 0
    assert "\nFallback answer.\n" in output
    assert captured["selected_model"] == "fast-model"
    assert captured["selected_url"] == "http://127.0.0.1:8081/v1/chat/completions"


def test_zeta_answer_model_failure_records_turn_abort(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "zeta-test")
    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)

    def failing_run_agent_turn(*args: object, **kwargs: object) -> None:
        raise RuntimeError("model stream failed: stream ended before [DONE]")

    monkeypatch.setattr(ask_runner, "run_agent_turn", failing_run_agent_turn)

    with pytest.raises(RuntimeError):
        ask_runner.run_tool_ask("system", "question")

    timeline = zeta_timeline.current_timeline()
    assert timeline[-1]["type"] == "turn_aborted"
    assert "model stream failed" in timeline[-1]["error"]
    assert timeline[-2]["type"] == "user_message"
    messages = zeta_timeline.chat_messages(timeline)
    assert messages[-1]["role"] == "assistant"
    assert "turn aborted" in messages[-1]["content"]


def test_zeta_step_model_failure_records_turn_abort(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "zeta-test")
    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)

    def failing_run_agent_turn(*args: object, **kwargs: object) -> None:
        raise RuntimeError("model request failed: connection reset")

    monkeypatch.setattr(zeta_runner, "run_agent_turn", failing_run_agent_turn)

    with pytest.raises(RuntimeError):
        zeta_runner.run_agent_step("do the thing", glyph=",,")

    timeline = zeta_timeline.current_timeline()
    assert timeline[-1]["type"] == "turn_aborted"
    assert timeline[-1]["glyph"] == ",,"
    assert "model request failed" in timeline[-1]["error"]
    assert timeline[-2]["type"] == "user_message"


def test_session_clear_removes_zeta_continuity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "zeta-test")
    zeta_timeline.record_event({"type": "user_message", "content": "hello"})
    record_turn("ls", 0, "/repo")
    session_root = tmp_path / "sessions" / "zeta-test"
    assert zeta_timeline.current_timeline() != []
    assert session_root.exists()

    result = CliRunner().invoke(sigil_cli, ["session", "clear"])

    assert result.exit_code == 0
    assert "zeta-trace.sqlite3" in result.output
    assert not session_root.exists()
    assert zeta_timeline.current_timeline() == []


def test_zeta_ask_workflow_keeps_stdout_clean_for_pipes(
    monkeypatch,
    capsys,
) -> None:
    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config
        event_sink = cast("Callable[[dict[str, Any]], None]", kwargs.get("event_sink"))
        assert callable(event_sink)
        events = [
            {
                "type": "tool_call",
                "id": "call-1",
                "tool_call_id": "call-1",
                "name": "read",
                "input": {"path": "README.md"},
            },
            {
                "type": "tool_result",
                "tool_call_id": "call-1",
                "name": "read",
                "result": {"ok": True, "content": [{"type": "text", "text": "A\n"}]},
            },
        ]
        for event in events:
            event_sink(event)
        return zeta_agent.AgentTurnResult(
            final_text="grep-safe answer",
            events=events,
            model_telemetry={
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
                "model_context_tokens": 1000,
            },
        )

    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(ask_runner, "run_agent_turn", fake_run_agent_turn)

    code = ask_runner.run_tool_ask("question system", "Question?")

    assert code == 0
    output = capsys.readouterr()
    assert "grep-safe answer" in output.out
    assert "❯" not in output.out
    assert "context  [" not in output.out
