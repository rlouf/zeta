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

import sigil
import sigil.display.state as display_state
from sigil import agent_io
from sigil import handoff as sigil_handoff
from sigil.cli import cli as sigil_cli
from sigil.protocols import (
    EFFECT_KIND_COMMAND,
    SHELL_HANDOFF_CANCEL_EXPECTED_NOT_EXECUTED,
    SHELL_HANDOFF_OUTCOME_CANCELLED,
    SHELL_HANDOFF_OUTCOME_EXECUTED,
    SHELL_HANDOFF_OUTCOME_NO_PENDING,
    SHELL_HANDOFF_RESULT_SCHEMA,
    SHELL_HANDOFF_RESULT_TYPE,
    TURN_OUTCOME_ABORTED,
    TURN_OUTCOME_EXECUTED,
    TURN_OUTCOME_FAILED,
    TURN_OUTCOME_STAGED,
    turn_contract,
)
from sigil.sessions import record_turn, session_dir
from sigil.state import history_view, read_events
from sigil.workflows import ask as ask_runner
from sigil.workflows import step as zeta_runner
from zeta import events as zeta_timeline
from zeta import loop as zeta_agent
from zeta import substrate as zeta_trace
from zeta.context import PromptTrace
from zeta.context.components import chat_messages
from zeta.events import Filter, SqliteEventStore, event_store_path
from zeta.history import (
    effect_record,
    history_event_record,
    is_effect_record,
    is_turn_record,
    turn_record,
)
from zeta.models import profiles as zeta_models


def record_sigil_event(event: dict[str, Any]) -> dict[str, Any]:
    return zeta_timeline.record_event(
        event,
        runtime_context=sigil.zeta_session_for_sigil(),
    )


def current_sigil_timeline() -> list[dict[str, Any]]:
    return zeta_timeline.current_timeline(
        runtime_context=sigil.zeta_session_for_sigil()
    )


def test_sigil_step_writes_handoff_file(
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
                        "effect": {
                            "kind": "command",
                            "status": "proposed",
                            "command": "uv run pytest",
                            "reason": "Run tests.",
                        },
                    },
                },
            ],
            staged_effect={
                "kind": "command",
                "status": "proposed",
                "command": "uv run pytest",
                "reason": "Run tests.",
            },
        ),
    )

    result = CliRunner().invoke(
        sigil_cli,
        [
            "step",
            "--workflow",
            "propose",
            "--handoff-file",
            str(handoff_file),
            "repair",
        ],
    )

    assert result.exit_code == 0
    assert "✓ uv run pytest · staged" in result.output
    assert handoff_file.read_text(encoding="utf-8") == "uv run pytest\n"


def test_sigil_step_keeps_trace_off_stdout(monkeypatch) -> None:
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
                {"type": "model", "content": "summary"},
            ],
        ),
    )

    result = CliRunner().invoke(
        sigil_cli, ["step", "--workflow", "propose", "summarize"]
    )

    assert result.exit_code == 0
    assert result.stdout == "\nsummary\n\n"
    assert "✓ read" in result.stderr
    assert "✓ read" not in result.stdout


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
                {"type": "model", "content": "The answer."},
            ],
        )

    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)
    monkeypatch.setattr(zeta_runner, "load_project_instructions", lambda: "ctx")

    code = zeta_runner.step("answer me", workflow="propose")

    assert code == 0
    output = capsys.readouterr()
    assert output.out.count("The answer.") == 1
    assert "❯" not in output.out
    assert "✓ read" in output.err
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

    code = zeta_runner.step("answer me", workflow="propose")

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

    code = zeta_runner.step("answer me", workflow="propose", trace_output=sys.stdout)

    assert code == 0
    output = capsys.readouterr().out
    assert output.index("done") < output.index("context  [")
    assert output.index("Done in") < output.index("context  [")


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

    code = zeta_runner.step(
        "inspect",
        workflow="propose",
        trace_output=sys.stdout,
    )

    assert code == 0
    output = capsys.readouterr().out
    assert ("✓ read a.md · 1 lines\n✓ read b.md · 1 lines") in output
    assert output.count("context  [") == 1
    assert "123 / 262,144 tokens" not in output
    assert output.index("Done in") < output.index("context  [░░░░░░░░░░░░░░░░░░░░] 0%")


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

    code = zeta_runner.step("answer me", workflow="propose")

    assert code == 0
    assert cast(list[dict[str, Any]], captured["transcript"]) == []
    assert current_sigil_timeline()[-1]["type"] == "user_message"
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

    code = zeta_runner.step("review", workflow="propose")

    assert code == 0
    config = cast(zeta_agent.AgentConfig, captured["config"])
    assert config.execution_mode == "stage"
    assert config.max_turns is None


def test_zeta_agent_step_supplies_the_workflow_persona(
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

    code = zeta_runner.step("review", workflow="propose")

    assert code == 0
    config = cast(zeta_agent.AgentConfig, captured["config"])
    assert config.system_prompt == zeta_runner.STEP_SYSTEM_PROMPT
    assert (
        "You are Zeta, a shell-native coding agent." in zeta_runner.STEP_SYSTEM_PROMPT
    )
    assert SHELL_HANDOFF_RESULT_SCHEMA in zeta_runner.STEP_SYSTEM_PROMPT
    user_event = current_sigil_timeline()[-1]
    assert user_event["type"] == "user_message"
    assert "You are Zeta, a shell-native coding agent." in user_event["system"]


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
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)

    code = ask_runner.ask("question")

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

    code = zeta_runner.step(
        "Review the changes",
        workflow="propose",
        allowed_tools=("bash",),
        handoff_path=handoff_file,
        handoff_output="summary",
    )

    assert code == 0
    output = capsys.readouterr()
    assert "· staged" in output.err
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
        assert capsys.readouterr().err == ""
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

    for workflow in ("propose", "do"):
        code = zeta_runner.step("inspect", workflow=workflow)

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

    code = zeta_runner.step("inspect", workflow="propose")

    assert code == 0
    output = capsys.readouterr()
    assert output.out.startswith("\nI'll inspect README.\n\n")
    assert "\nIt is a README.\n" in output.out
    assert "Done in" in output.err


@pytest.mark.parametrize("workflow", ["propose", "do"])
def test_zeta_agent_step_separates_tool_result_from_later_streamed_text(
    workflow: zeta_runner.Workflow,
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

    code = zeta_runner.step(
        "inspect",
        workflow=workflow,
        trace_output=sys.stdout,
    )

    assert code == 0
    output = capsys.readouterr().out
    assert output.index("I'll inspect README.") < output.index("✓ read README.md")
    assert output.index("✓ read README.md") < output.index("It is a README.")


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

    code = zeta_runner.step(
        "inspect",
        workflow="propose",
        trace_output=sys.stdout,
    )

    assert code == 0
    output = capsys.readouterr().out
    assert "✓ read a.md · 1 lines\n✓ read b.md · 1 lines" in output
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

    code = zeta_runner.step(
        "inspect",
        workflow="propose",
        trace_output=output,
    )

    assert code == 0
    out_text = capsys.readouterr().out
    assert out_text.count("Done.") == 1
    assert "❯" not in out_text
    trace_text = visible_terminal_text(output.getvalue())
    assert "✓ read README.md · 1 lines" in trace_text
    assert "mapping repo · 1 events · last: README.md" in trace_text
    assert "prefill 0s" in trace_text


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
                {"type": "model", "content": "edited and verified"},
            ],
        ),
    )

    code = zeta_runner.step("edit", workflow="do")

    assert code == 0
    output = capsys.readouterr()
    assert output.out.count("edited and verified") == 1
    assert "❯" not in output.out
    assert "+ a.txt" in output.err


def test_zeta_step_only_the_do_workflow_executes_directly() -> None:
    assert zeta_runner.stages_mutations("stage", ("bash", "edit")) is True
    assert zeta_runner.stages_mutations("stage", ("read", "grep")) is False
    assert zeta_runner.stages_mutations("direct", ("bash", "edit")) is False


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

    code = zeta_runner.step("@step-skill: do step work", workflow="propose")

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
thinking = "low"
""",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SIGIL_SESSION_ID", "agent-model")
    zeta_models.set_active_model_profile("coder", session_dir=session_dir())
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

    code = zeta_runner.step("do work", workflow="propose")

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
    assert config.thinking == "low"


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

    code = ask_runner.ask("@answer-skill: do answer work")

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
    zeta_models.set_active_model_profile("fast", session_dir=session_dir())
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
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)

    code = ask_runner.ask("prompt")

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
    assert current_sigil_timeline()[-1]["model"] == {
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
    record_sigil_event(
        {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "name": "bash",
            "result": {
                "ok": True,
                "effect": {
                    "kind": "command",
                    "status": "proposed",
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
    assert event["result"]["effect"] == {
        "kind": "command",
        "status": "resolved",
        "outcome": SHELL_HANDOFF_OUTCOME_EXECUTED,
        "command": "uv run pytest",
        "proposed_command": "uv run pytest",
    }
    assert event["result"]["shell_turns"][0]["command"] == "uv run pytest"
    assert "uv run pytest (exit 1)" in event["result"]["content"][0]["text"]


def test_resolved_shell_handoff_context_keeps_tool_call_with_shell_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "zeta-test")
    record_sigil_event(
        {
            "type": "model",
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
    record_sigil_event(
        {
            "type": "tool_call",
            "id": "call-1",
            "tool_call_id": "call-1",
            "name": "bash",
            "input": {"command": "uv run pytest"},
        }
    )
    record_sigil_event(
        {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "name": "bash",
            "result": {
                "ok": True,
                "effect": {
                    "kind": "command",
                    "status": "proposed",
                    "command": "uv run pytest",
                    "reason": "Run tests.",
                },
            },
        }
    )
    record_turn("uv run pytest", 1, "/repo", stderr_snippet="test failed")

    sigil_handoff.append_shell_result()
    messages = chat_messages(current_sigil_timeline())

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
    record_sigil_event(
        {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "name": "bash",
            "result": {
                "ok": True,
                "effect": {
                    "kind": "command",
                    "status": "proposed",
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
    record_sigil_event(
        {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "name": "bash",
            "result": {
                "ok": True,
                "effect": {
                    "kind": "command",
                    "status": "proposed",
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
    record_sigil_event(
        {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "name": "bash",
            "result": {
                "ok": True,
                "effect": {
                    "kind": "command",
                    "status": "proposed",
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
    assert event["result"]["effect"] == {
        "kind": "command",
        "status": "cancelled",
        "outcome": SHELL_HANDOFF_OUTCOME_CANCELLED,
        "command": "uv run pytest",
        "actual_command": "git status --short",
    }
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
    record_sigil_event(
        {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "name": "bash",
            "result": {
                "ok": True,
                "effect": {
                    "kind": "command",
                    "status": "proposed",
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
    record_sigil_event(
        {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "name": "bash",
            "result": {
                "ok": True,
                "effect": {
                    "kind": "command",
                    "status": "proposed",
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
                    "type": "model",
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
                    "type": "model",
                    "content": "It contains project metadata.",
                },
            ],
        )

    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)

    code = ask_runner.ask("What does pyproject.toml contain?")

    assert code == 0
    output = capsys.readouterr()
    assert "✓ read pyproject.toml" in output.err
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
            model_telemetry_calls=[telemetry],
        )

    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)

    code = ask_runner.ask("What does pyproject.toml contain?")

    assert code == 0
    output = capsys.readouterr()
    assert "It contains project metadata." in output.out
    assert "context  [" in output.err
    assert "7%" in output.err
    assert "context  [" not in output.out
    assert "18,823 / 262,144 tokens" not in output.err
    (turn,) = history_turns()
    assert turn["cost"]["input_tokens"] == 18_432
    assert turn["cost"]["output_tokens"] == 391
    assert turn["cost"]["model_calls"] == 1


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
        assert isinstance(stream_sink, display_state.TraceAwareStreamRenderer)
        assert isinstance(stream_sink.renderer, display_state.TerminalStreamRenderer)
        stream_sink.content_delta("streamed answer")
        return zeta_agent.AgentTurnResult(
            final_text="streamed answer",
            events=[{"type": "model", "content": "streamed answer"}],
            final_text_streamed=True,
        )

    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)

    code = ask_runner.ask("Question?")

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
        assert isinstance(stream_sink, display_state.TraceAwareStreamRenderer)
        assert isinstance(stream_sink.renderer, display_state.RichStreamRenderer)
        stream_sink.content_delta("**streamed** answer")
        return zeta_agent.AgentTurnResult(
            final_text="streamed answer",
            events=[{"type": "model", "content": "streamed answer"}],
            final_text_streamed=True,
        )

    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)

    code = ask_runner.ask("Question?")

    assert code == 0
    assert "streamed answer" in visible_terminal_text(output.getvalue())
    timeline = current_sigil_timeline()
    assert timeline[-1]["type"] == "model"
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
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)

    code = ask_runner.ask("Question?")

    assert code == 0
    output = capsys.readouterr()
    assert "I'll inspect README." in output.out
    assert "It is a README." in output.out
    assert "✓ read README.md · 1 lines" in output.err
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
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)

    code = ask_runner.ask("Question?")

    assert code == 0
    output = capsys.readouterr()
    assert ("✓ read a.md · 1 lines\n✓ read b.md · 1 lines") in output.err
    assert output.err.count("context  [") == 1
    assert "It is a README." in output.out
    assert "context  [" not in output.out
    assert output.err.index("Done in") < output.err.index("context  [")


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
        assert capsys.readouterr().err == ""
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

    code = ask_runner.ask("What does README.md contain?")

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
            events=[{"type": "model", "content": "follow-up answer"}],
        )

    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)
    monkeypatch.setattr(zeta_runner, "load_project_instructions", lambda: "ctx")

    record_sigil_event({"type": "user_message", "content": "summarize README"})
    record_sigil_event({"type": "model", "content": "It is a Sigil README."})

    code = ask_runner.ask("and why?")

    assert code == 0
    contents = [str(event.get("content") or "") for event in transcripts[0]]
    assert contents == ["summarize README", "It is a Sigil README."]
    assert captured["context"] == "ctx"
    timeline = current_sigil_timeline()
    assert [event["content"] for event in timeline[-2:]] == [
        "and why?",
        "follow-up answer",
    ]


def test_zeta_ask_workflow_reports_stall_without_final_answer(
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
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)

    code = ask_runner.ask("What does README.md contain?")

    assert code == 1
    assert "Zeta stopped without a final answer." in capsys.readouterr().err
    (turn,) = history_turns()
    assert turn["workflow"] == "ask"
    assert turn["outcome"] == TURN_OUTCOME_FAILED


def test_zeta_answer_model_failure_records_turn_abort(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "zeta-test")
    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)

    def failing_run_agent_turn(*args: object, **kwargs: object) -> None:
        raise RuntimeError("model stream failed: stream ended before [DONE]")

    monkeypatch.setattr(zeta_runner, "run_agent_turn", failing_run_agent_turn)

    with pytest.raises(RuntimeError):
        ask_runner.ask("question")

    timeline = current_sigil_timeline()
    assert timeline[-1]["type"] == "turn_aborted"
    assert "model stream failed" in timeline[-1]["error"]
    assert timeline[-2]["type"] == "user_message"
    messages = chat_messages(timeline)
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
        zeta_runner.step("do the thing", workflow="propose")

    timeline = current_sigil_timeline()
    assert timeline[-1]["type"] == "turn_aborted"
    assert timeline[-1]["workflow"] == "propose"
    assert "model request failed" in timeline[-1]["error"]
    assert timeline[-2]["type"] == "user_message"


def test_session_clear_removes_zeta_continuity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "zeta-test")
    record_sigil_event({"type": "user_message", "content": "hello"})
    record_turn("ls", 0, "/repo")
    session_root = tmp_path / "sessions" / "zeta-test"
    assert current_sigil_timeline() != []
    assert session_root.exists()

    result = CliRunner().invoke(sigil_cli, ["session", "clear"])

    assert result.exit_code == 0
    assert "zeta.sqlite3" in result.output
    assert not session_root.exists()
    assert current_sigil_timeline() == []


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
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)

    code = ask_runner.ask("Question?")

    assert code == 0
    output = capsys.readouterr()
    assert "grep-safe answer" in output.out
    assert "❯" not in output.out
    assert "context  [" not in output.out


def test_turn_record_carries_schema_contract_and_optional_blocks() -> None:
    record = turn_record(
        "turn-1",
        workflow="propose",
        objective="fix the failing test",
        contract=turn_contract("propose", ("bash", "edit"), staged=True),
        outcome=TURN_OUTCOME_STAGED,
        agent={"profile": "local", "model": "m", "url": "http://localhost"},
        cost={"input_tokens": 10, "output_tokens": 2, "model_calls": 1, "wall_ms": 5},
        prompt_object_ids=["sha256:abc"],
        effect_ids=["effect-1"],
    )

    assert record["type"] == "zeta.turn.completed"
    assert record["schema"] == "zeta.turn"
    assert record["turn_id"] == "turn-1"
    assert record["contract"] == {
        "workflow": "propose",
        "allowed_tools": ["bash", "edit"],
        "staged": True,
    }
    assert record["agent"]["model"] == "m"
    assert record["cost"]["input_tokens"] == 10
    assert record["prompt_object_ids"] == ["sha256:abc"]
    assert record["effect_ids"] == ["effect-1"]
    assert is_turn_record(record)
    assert not is_effect_record(record)


def test_turn_record_omits_absent_agent_and_cost() -> None:
    record = turn_record(
        "turn-1",
        workflow="run",
        objective="ls",
        contract=turn_contract("run", (), staged=False),
        outcome=TURN_OUTCOME_EXECUTED,
    )

    assert "agent" not in record
    assert "cost" not in record
    assert record["prompt_object_ids"] == []
    assert record["effect_ids"] == []


def test_effect_record_keeps_only_set_optionals() -> None:
    record = effect_record(
        "effect-1",
        turn_id="turn-1",
        kind=EFFECT_KIND_COMMAND,
        staged=False,
        command="ls",
        exit_status=0,
    )

    assert record["type"] == "zeta.effect"
    assert record["schema"] == "zeta.effect"
    assert record["effect_id"] == "effect-1"
    assert record["turn_id"] == "turn-1"
    assert record["command"] == "ls"
    assert record["exit_status"] == 0
    assert record["staged"] is False
    for absent in ("path", "before_hash", "after_hash", "resolved_outcome"):
        assert absent not in record
    assert is_effect_record(record)
    assert not is_turn_record(record)


def history_turns() -> list[dict[str, Any]]:
    return [
        history_event_record(event)
        for event in read_events()
        if event.event_type.startswith("zeta.turn.")
    ]


def history_effects() -> list[dict[str, Any]]:
    return history_view().effects()


def zeta_tool_events() -> list[Any]:
    return [
        event
        for event in read_zeta_events()
        if event.event_type == "zeta.tool.called"
        and event.payload.get("_timeline_type") == "tool_result"
    ]


def read_zeta_events() -> list[Any]:
    store = SqliteEventStore(event_store_path())
    try:
        return store.list_events(Filter())
    finally:
        store.close()


def test_zeta_step_threads_durable_event_causality(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config
        prompt_event_id = cast(str, kwargs["caused_by"])
        captured["prompt_event_id"] = prompt_event_id
        return zeta_agent.AgentTurnResult(
            final_text="done",
            events=[
                {
                    "type": "model",
                    "id": "model-event",
                    "content": "",
                    "caused_by": prompt_event_id,
                },
                {
                    "type": "tool_call",
                    "id": "call-1",
                    "tool_call_id": "call-1",
                    "name": "write",
                    "input": {"path": "a.txt", "content": "hello\n"},
                    "caused_by": "model-event",
                },
                {
                    "type": "tool_result",
                    "id": "tool-event",
                    "tool_call_id": "call-1",
                    "name": "write",
                    "result": {
                        "ok": True,
                        "metadata": {"mode": "direct", "path": "a.txt"},
                    },
                    "caused_by": "model-event",
                },
            ],
        )

    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)

    code = zeta_runner.step("write the file", workflow="do")

    assert code == 0
    events = read_events()
    zeta_events = read_zeta_events()
    (prompt_event,) = [
        event for event in events if event.event_type == "zeta.prompt.submitted"
    ]
    (model_event,) = [
        event for event in zeta_events if event.event_type == "zeta.model.called"
    ]
    (tool_event,) = zeta_tool_events()
    (turn_event,) = [
        event for event in events if event.event_type == "zeta.turn.completed"
    ]
    assert captured["prompt_event_id"] == prompt_event.id
    assert model_event.id == "model-event"
    assert model_event.caused_by == prompt_event.id
    assert tool_event.id == "tool-event"
    assert tool_event.caused_by == model_event.id
    assert turn_event.caused_by == tool_event.id


def test_zeta_step_records_staged_turn_record(monkeypatch) -> None:
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
                        "effect": {
                            "kind": "command",
                            "status": "proposed",
                            "command": "uv run pytest",
                            "reason": "Run tests.",
                        },
                    },
                },
            ],
            staged_effect={
                "kind": "command",
                "status": "proposed",
                "command": "uv run pytest",
                "reason": "Run tests.",
            },
            model_telemetry_calls=[
                {"usage": {"prompt_tokens": 7, "completion_tokens": 3}},
                {"usage": {"prompt_tokens": 11, "completion_tokens": 5}},
            ],
            prompt_traces=[PromptTrace(prompt_object_id="sha256:p1")],
        ),
    )

    code = zeta_runner.step("repair the tests", workflow="propose")

    assert code == 0
    (turn,) = history_turns()
    assert turn["workflow"] == "propose"
    assert turn["objective"] == "repair the tests"
    assert turn["outcome"] == TURN_OUTCOME_STAGED
    assert turn["contract"]["staged"] is True
    assert "bash" in turn["contract"]["allowed_tools"]
    assert turn["prompt_object_ids"] == ["sha256:p1"]
    assert turn["cost"]["input_tokens"] == 18
    assert turn["cost"]["output_tokens"] == 8
    assert turn["cost"]["model_calls"] == 2
    assert turn["cost"]["wall_ms"] >= 0
    (effect,) = history_effects()
    assert turn["effect_ids"] == [effect["effect_id"]]
    assert effect["turn_id"] == turn["turn_id"]
    assert effect["kind"] == EFFECT_KIND_COMMAND
    assert effect["staged"] is True
    assert effect["command"] == "uv run pytest"
    assert effect["tool_call_id"] == "call-1"
    assert "exit_status" not in effect
    (tool_event,) = zeta_tool_events()
    assert tool_event.payload["effects"] == [
        {
            key: value
            for key, value in effect.items()
            if key not in {"time", "session", "cwd"}
        }
    ]
    assert all(event.event_type != "zeta.effect" for event in read_events())
    history = history_view()
    assert history.turn(turn["turn_id"]) == turn
    assert history.effects_for_turn(turn["turn_id"]) == [effect]


def test_do_step_records_executed_turn_with_file_effect(monkeypatch) -> None:
    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(
        zeta_runner,
        "run_agent_turn",
        lambda *args, **kwargs: zeta_agent.AgentTurnResult(
            final_text="done",
            events=[
                {
                    "type": "tool_call",
                    "id": "call-1",
                    "tool_call_id": "call-1",
                    "name": "write",
                    "input": {"path": "a.txt", "content": "hello\n"},
                },
                {
                    "type": "tool_result",
                    "tool_call_id": "call-1",
                    "name": "write",
                    "result": {
                        "ok": True,
                        "content": [{"type": "text", "text": "wrote a.txt"}],
                        "metadata": {
                            "mode": "direct",
                            "path": "a.txt",
                            "before_hash": "sha256:before",
                            "after_hash": "sha256:after",
                        },
                    },
                },
            ],
            model_telemetry_calls=[
                {"usage": {"prompt_tokens": 9, "completion_tokens": 4}}
            ],
        ),
    )

    code = zeta_runner.step("write the file", workflow="do")

    assert code == 0
    (turn,) = history_turns()
    assert turn["workflow"] == "do"
    assert turn["outcome"] == TURN_OUTCOME_EXECUTED
    assert turn["contract"]["staged"] is False
    (effect,) = history_effects()
    assert turn["effect_ids"] == [effect["effect_id"]]
    assert effect["kind"] == "file_write"
    assert effect["staged"] is False
    assert effect["path"] == "a.txt"
    assert effect["before_hash"] == "sha256:before"
    assert effect["after_hash"] == "sha256:after"


def test_zeta_step_bridges_turn_record_into_trace_graph(monkeypatch) -> None:
    prompt_object_id = "sha256:" + "2" * 64
    tool_result_object_id = "sha256:" + "1" * 64
    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(
        zeta_runner,
        "run_agent_turn",
        lambda *args, **kwargs: zeta_agent.AgentTurnResult(
            final_text="done",
            events=[
                {
                    "type": "tool_call",
                    "id": "call-1",
                    "tool_call_id": "call-1",
                    "name": "write",
                    "input": {"path": "a.txt", "content": "hello\n"},
                },
                {
                    "type": "tool_result",
                    "tool_call_id": "call-1",
                    "name": "write",
                    "tool_result_object_id": tool_result_object_id,
                    "result": {
                        "ok": True,
                        "metadata": {"mode": "direct", "path": "a.txt"},
                    },
                },
            ],
            prompt_traces=[PromptTrace(prompt_object_id=prompt_object_id)],
        ),
    )

    code = zeta_runner.step("write the file", workflow="do")

    assert code == 0
    (turn,) = history_turns()
    store = sigil.zeta_session_for_sigil().trace_store
    turn_object_id = zeta_trace.resolve_object_id(store, f"turn/{turn['turn_id']}")
    turn_object = store.get_object(turn_object_id)
    assert turn_object is not None
    assert turn_object.kind == "zeta.turn"
    assert turn_object.schema == "zeta.turn"
    assert turn_object.data["turn_id"] == turn["turn_id"]
    assert turn_object.data["effects"] == history_effects()
    assert turn_object.links == (prompt_object_id, tool_result_object_id)
    derivations = [
        derivation
        for derivation in store.derivations_for_input(prompt_object_id)
        if derivation.producer == "TurnRecord"
    ]
    assert [derivation.output_id for derivation in derivations] == [turn_object_id]
    assert derivations[0].params == {"workflow": "do", "outcome": "executed"}


def test_turn_bridge_failure_does_not_break_the_step(monkeypatch) -> None:
    class BrokenStore:
        def get_ref(self, name: str) -> str | None:
            raise RuntimeError("trace store unavailable")

        def batch(self):
            raise RuntimeError("trace store unavailable")

    class BrokenContext:
        def __init__(self, base) -> None:
            self.session_id = base.session_id
            self.event_sink = base.event_sink
            self.tool_registry = base.tool_registry
            self.state_dir = base.state_dir
            self.session_dir = base.session_dir
            self.trace_store = BrokenStore()

    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    base_context = sigil.zeta_session_for_sigil()
    monkeypatch.setattr(
        sigil, "zeta_session_for_sigil", lambda: BrokenContext(base_context)
    )
    monkeypatch.setattr(
        zeta_runner,
        "run_agent_turn",
        lambda *args, **kwargs: zeta_agent.AgentTurnResult(
            final_text="done",
            events=[{"type": "model", "content": "done"}],
        ),
    )

    code = zeta_runner.step("answer", workflow="propose")

    assert code == 0
    (turn,) = history_turns()
    assert turn["outcome"] == "answered"


def test_zeta_step_tags_timeline_events_with_turn_id(monkeypatch) -> None:
    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(
        zeta_runner,
        "run_agent_turn",
        lambda *args, **kwargs: zeta_agent.AgentTurnResult(
            final_text="done",
            events=[{"type": "model", "content": "done"}],
        ),
    )

    code = zeta_runner.step("answer", workflow="propose")

    assert code == 0
    (turn,) = history_turns()
    tagged = [
        event
        for event in current_sigil_timeline()
        if event.get("turn_id") == turn["turn_id"]
    ]
    assert tagged


def test_zeta_step_records_failed_turn_without_answer(monkeypatch) -> None:
    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(
        zeta_runner,
        "run_agent_turn",
        lambda *args, **kwargs: zeta_agent.AgentTurnResult(events=[]),
    )

    code = zeta_runner.step("do nothing", workflow="propose")

    assert code == 1
    (turn,) = history_turns()
    assert turn["outcome"] == TURN_OUTCOME_FAILED


def test_zeta_step_records_aborted_turn_on_runtime_error(monkeypatch) -> None:
    def raise_runtime_error(*args, **kwargs) -> zeta_agent.AgentTurnResult:
        raise RuntimeError("model endpoint is not reachable")

    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_runner, "run_agent_turn", raise_runtime_error)

    with pytest.raises(RuntimeError):
        zeta_runner.step("crash", workflow="propose")

    (turn,) = history_turns()
    assert turn["outcome"] == TURN_OUTCOME_ABORTED
    assert set(turn["cost"]) == {"wall_ms"}
    assert turn["effect_ids"] == []


def test_zeta_step_records_aborted_turn_on_keyboard_interrupt(monkeypatch) -> None:
    def raise_keyboard_interrupt(*args, **kwargs) -> zeta_agent.AgentTurnResult:
        raise KeyboardInterrupt()

    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_runner, "run_agent_turn", raise_keyboard_interrupt)

    with pytest.raises(KeyboardInterrupt):
        zeta_runner.step("stop", workflow="propose")

    timeline = current_sigil_timeline()
    assert timeline[-1]["type"] == "turn_aborted"
    assert timeline[-1]["reason"] == "keyboard_interrupt"
    (turn,) = history_turns()
    assert turn["outcome"] == TURN_OUTCOME_ABORTED
    assert turn["caused_by"] == timeline[-1]["id"]


def test_ask_records_answered_turn_record(monkeypatch, capsys) -> None:
    monkeypatch.setattr(agent_io, "ensure_server", lambda: True)
    monkeypatch.setattr(
        zeta_runner,
        "run_agent_turn",
        lambda *args, **kwargs: zeta_agent.AgentTurnResult(
            final_text="the answer",
            events=[{"type": "model", "content": "the answer"}],
            model_telemetry_calls=[
                {"usage": {"prompt_tokens": 21, "completion_tokens": 6}}
            ],
            prompt_traces=[PromptTrace(prompt_object_id="sha256:ask1")],
        ),
    )

    code = ask_runner.ask("Why did it fail?")

    assert code == 0
    capsys.readouterr()
    (turn,) = history_turns()
    assert turn["workflow"] == "ask"
    assert turn["objective"] == "Why did it fail?"
    assert turn["outcome"] == "answered"
    assert turn["contract"]["staged"] is False
    assert turn["contract"]["allowed_tools"] == [
        "read",
        "grep",
        "ls",
        "query_log",
        "web_search",
    ]
    assert turn["prompt_object_ids"] == ["sha256:ask1"]
    assert turn["cost"]["model_calls"] == 1
    assert history_effects() == []


def test_record_turn_emits_run_turn_and_command_effect() -> None:
    record_turn(
        "uv run pytest",
        1,
        "/repo",
        stderr_snippet="test failed",
        duration_ms=42,
    )

    (turn,) = history_turns()
    assert turn["workflow"] == "run"
    assert turn["objective"] == "uv run pytest"
    assert turn["outcome"] == TURN_OUTCOME_FAILED
    assert turn["contract"] == {
        "workflow": "run",
        "allowed_tools": [],
        "staged": False,
    }
    assert "agent" not in turn
    assert "cost" not in turn
    assert turn["cwd"] == "/repo"
    (effect,) = history_effects()
    assert turn["effect_ids"] == [effect["effect_id"]]
    assert effect["turn_id"] == turn["turn_id"]
    assert effect["kind"] == EFFECT_KIND_COMMAND
    assert effect["command"] == "uv run pytest"
    assert effect["exit_status"] == 1
    assert effect["duration_ms"] == 42
    assert effect["staged"] is False
    history = history_view()
    assert history.turn(turn["turn_id"]) == turn
    assert history.effects_for_turn(turn["turn_id"]) == [effect]


def test_record_turn_marks_clean_exit_executed() -> None:
    record_turn("git status", 0, "/repo")

    (turn,) = history_turns()
    assert turn["outcome"] == TURN_OUTCOME_EXECUTED
    (effect,) = history_effects()
    assert effect["exit_status"] == 0
    assert "duration_ms" not in effect


def test_record_turn_skips_history_for_skippable_commands() -> None:
    record_turn(", why did this fail", 0, "/repo")

    assert history_turns() == []
    assert history_effects() == []


def test_shell_handoff_resolution_emits_handoff_effect() -> None:
    handoff_meta = {
        "tool_call_id": "call-1",
        "name": "bash",
        "command": "uv run pytest",
        "reason": "Run tests.",
        "time": 100.0,
        "turn_id": "turn-stage-1",
    }
    turns = [
        {
            "id": "t1",
            "time": 101.0,
            "command": "uv run pytest",
            "status": 2,
            "turn_cwd": "/repo",
        }
    ]

    result = sigil_handoff.shell_handoff_result(handoff_meta, turns)

    assert result["outcome"] == SHELL_HANDOFF_OUTCOME_EXECUTED
    (effect,) = history_effects()
    assert effect["kind"] == "handoff"
    assert effect["turn_id"] == "turn-stage-1"
    assert effect["tool_call_id"] == "call-1"
    assert effect["resolved_outcome"] == SHELL_HANDOFF_OUTCOME_EXECUTED
    assert effect["command"] == "uv run pytest"
    assert effect["exit_status"] == 2
    assert effect["staged"] is True
    assert history_view().effects_for_turn("turn-stage-1") == [effect]


def test_cancelled_handoff_resolution_emits_cancelled_effect() -> None:
    handoff_meta = {
        "tool_call_id": "call-1",
        "name": "bash",
        "command": "uv run pytest",
        "reason": "Run tests.",
        "time": 100.0,
        "turn_id": "turn-stage-1",
    }
    turns = [
        {
            "id": "t1",
            "time": 101.0,
            "command": "git status",
            "status": 0,
            "turn_cwd": "/repo",
        }
    ]

    result = sigil_handoff.shell_handoff_result(handoff_meta, turns)

    assert result["outcome"] == SHELL_HANDOFF_OUTCOME_CANCELLED
    (effect,) = history_effects()
    assert effect["resolved_outcome"] == SHELL_HANDOFF_OUTCOME_CANCELLED
    assert effect["command"] == "uv run pytest"
    assert effect["turn_id"] == "turn-stage-1"
    assert "exit_status" not in effect


def test_latest_unresolved_shell_handoff_surfaces_turn_id() -> None:
    timeline = [
        {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "name": "bash",
            "turn_id": "turn-stage-1",
            "time": 100.0,
            "result": {
                "ok": True,
                "effect": {
                    "kind": "command",
                    "status": "proposed",
                    "command": "make",
                    "reason": "Build.",
                },
            },
        }
    ]

    handoff_meta = sigil_handoff.latest_unresolved_shell_handoff(timeline)

    assert handoff_meta["turn_id"] == "turn-stage-1"


def test_model_server_ready_skips_probe_for_codex_api(monkeypatch) -> None:
    def fail_probe(**kwargs: object) -> bool:
        raise AssertionError("codex selections must not probe a local endpoint")

    monkeypatch.setattr(agent_io, "ensure_server", fail_probe)

    selection = zeta_models.ModelSelection(
        profile="codex",
        model="gpt-5.5",
        url=zeta_models.DEFAULT_CODEX_BASE_URL,
        api=zeta_models.CODEX_RESPONSES_API,
    )

    assert agent_io.model_server_ready(selection) is True


def test_command_matches_staged_ignores_whitespace_runs() -> None:
    assert sigil_handoff.command_matches_staged("uv  run   pytest", "uv run pytest")


def test_command_matches_staged_accepts_extended_arguments() -> None:
    assert sigil_handoff.command_matches_staged("uv run pytest", "uv run pytest -q")


def test_command_matches_staged_rejects_unrelated_command() -> None:
    assert not sigil_handoff.command_matches_staged("uv run pytest", "git status")


def test_command_matches_staged_rejects_empty_staged_command() -> None:
    assert not sigil_handoff.command_matches_staged("", "git status")


def staged_handoff_event(command: str) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_call_id": "call-resume",
        "name": "bash",
        "result": {
            "ok": True,
            "effect": {
                "kind": "command",
                "status": "proposed",
                "command": command,
                "reason": "Run it.",
            },
        },
    }


def test_matching_pending_handoff_returns_staged_handoff(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "resume-test")
    record_sigil_event(staged_handoff_event("uv run pytest"))

    handoff = sigil_handoff.matching_pending_handoff(
        "uv run pytest -q",
        current_sigil_timeline(),
    )

    assert handoff["command"] == "uv run pytest"
    assert handoff["tool_call_id"] == "call-resume"


def test_matching_pending_handoff_ignores_unrelated_command(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "resume-test")
    record_sigil_event(staged_handoff_event("uv run pytest"))

    handoff = sigil_handoff.matching_pending_handoff(
        "git status",
        current_sigil_timeline(),
    )

    assert handoff == {}


def test_matching_pending_handoff_without_pending_handoff(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "resume-test")

    handoff = sigil_handoff.matching_pending_handoff(
        "uv run pytest",
        current_sigil_timeline(),
    )

    assert handoff == {}


def test_sigil_run_writes_resume_file_for_staged_command(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "resume-test")
    record_sigil_event(staged_handoff_event("echo staged"))
    resume_file = tmp_path / "resume"

    result = CliRunner().invoke(
        sigil_cli,
        ["run", "--resume-file", str(resume_file), "--shell", "echo staged"],
    )

    assert result.exit_code == 0
    assert resume_file.read_text(encoding="utf-8") == "echo staged\n"


def test_sigil_run_leaves_resume_file_untouched_for_unrelated_command(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "resume-test")
    record_sigil_event(staged_handoff_event("echo staged"))
    resume_file = tmp_path / "resume"

    result = CliRunner().invoke(
        sigil_cli,
        ["run", "--resume-file", str(resume_file), "--shell", "echo other"],
    )

    assert result.exit_code == 0
    assert not resume_file.exists()


def test_sigil_run_skips_resume_for_interrupted_command(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "resume-test")
    record_sigil_event(staged_handoff_event("exit 130"))
    resume_file = tmp_path / "resume"

    result = CliRunner().invoke(
        sigil_cli,
        ["run", "--resume-file", str(resume_file), "--shell", "exit 130"],
    )

    assert result.exit_code == 130
    assert not resume_file.exists()
