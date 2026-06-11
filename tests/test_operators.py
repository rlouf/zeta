from __future__ import annotations

from io import StringIO

import pytest
from _patch import patch
from click.testing import CliRunner

from sigil.cli import cli, main
from sigil.cli._base import MODEL_ERROR_EXIT_CODE
from sigil.cli.step import DEFAULT_QUESTION


def test_command_verb_is_not_registered() -> None:
    result = CliRunner().invoke(cli, ["command", "update example"])

    assert result.exit_code == 2
    assert "No such command" in result.stderr


def test_ask_verb_accepts_piped_input() -> None:
    ask_calls = []

    def fake_ask(*args: object, **kwargs: object) -> int:
        ask_calls.append((args, kwargs))
        return 0

    with patch("sigil.cli.step.ask", side_effect=fake_ask):
        ask_result = CliRunner().invoke(
            cli,
            ["ask", "review"],
            input="diff\n",
        )

    assert ask_result.exit_code == 0, ask_result.output
    assert ask_result.output == ""
    assert ask_calls == [
        (
            ("review\n\nPiped input:\ndiff\n",),
            {},
        )
    ]


def test_ask_without_question_uses_default_summary_prompt() -> None:
    calls = []

    def fake_ask(*args: object, **kwargs: object) -> int:
        calls.append((args, kwargs))
        return 0

    with patch("sigil.cli.step.ask", side_effect=fake_ask):
        result = CliRunner().invoke(cli, ["ask"])

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            (DEFAULT_QUESTION,),
            {},
        )
    ]


def test_main_reports_model_runtime_error(capsys, monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", StringIO(""))
    with patch(
        "sigil.cli.step.ask",
        side_effect=RuntimeError("model request failed: connection reset"),
    ):
        code = main(["ask", "why"])

    captured = capsys.readouterr()
    assert code == MODEL_ERROR_EXIT_CODE
    assert "sigil: model request failed: connection reset" in captured.err
    assert "sigil doctor" in captured.err


def test_patch_side_effect_raises_exception_classes() -> None:
    from sigil.cli import step as step_module

    with patch("sigil.cli.step.ask", side_effect=RuntimeError):
        with pytest.raises(RuntimeError):
            step_module.ask("boom")
