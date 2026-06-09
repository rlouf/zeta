from __future__ import annotations

from io import StringIO

from click.testing import CliRunner

from _patch import patch
from sigil.cli import cli, main
from sigil.cli.ask import DEFAULT_QUESTION


def test_command_verb_is_not_registered() -> None:
    result = CliRunner().invoke(cli, ["command", "update example"])

    assert result.exit_code == 2
    assert "No such command" in result.stderr


def test_ask_verb_accepts_piped_input() -> None:
    ask_calls = []

    def fake_ask(*args: object, **kwargs: object) -> int:
        ask_calls.append((args, kwargs))
        return 0

    with patch("sigil.cli.ask.ask", side_effect=fake_ask):
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
            {
                "glyph": "ask",
                "tools": "read,grep,ls",
                "json_output": False,
            },
        )
    ]


def test_ask_follow_up_sends_piped_input_without_confirmation() -> None:
    calls = []

    def fake_ask(*args: object, **kwargs: object) -> int:
        calls.append((args, kwargs))
        return 0

    with patch("sigil.cli.ask.ask", side_effect=fake_ask):
        result = CliRunner().invoke(
            cli,
            ["ask", "--follow-up", "review"],
            input="diff\n",
        )

    assert result.exit_code == 0
    assert calls == [
        (
            ("review\n\nPiped input:\ndiff\n",),
            {
                "glyph": "ask",
                "tools": "read,grep,ls",
                "follow_up": True,
                "history": [],
                "json_output": False,
            },
        )
    ]


def test_ask_without_question_uses_default_summary_prompt() -> None:
    calls = []

    def fake_ask(*args: object, **kwargs: object) -> int:
        calls.append((args, kwargs))
        return 0

    with patch("sigil.cli.ask.ask", side_effect=fake_ask):
        result = CliRunner().invoke(cli, ["ask"])

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            (DEFAULT_QUESTION,),
            {
                "glyph": "ask",
                "tools": "read,grep,ls",
                "json_output": False,
            },
        )
    ]


def test_main_reports_model_runtime_error(capsys, monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", StringIO(""))
    with patch(
        "sigil.cli.ask.ask",
        side_effect=RuntimeError("model request failed: connection reset"),
    ):
        code = main(["ask", "why"])

    captured = capsys.readouterr()
    assert code == 1
    assert "sigil: model request failed: connection reset" in captured.err
    assert "sigil doctor" in captured.err
