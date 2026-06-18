import sys
import textwrap
from collections.abc import Iterator
from io import StringIO
from pathlib import Path

import pytest
from _patch import patch
from click.testing import CliRunner

from sigil.cli import cli, main
from sigil.cli._base import EXIT_ERROR, EXIT_MODEL_UNAVAILABLE, EXIT_OK, EXIT_USAGE
from sigil.cli.step import CONTINUE_OBJECTIVE


def editor_command(tmp_path: Path, body: str) -> str:
    """Write a Python script standing in for $EDITOR; argv[1] is the buffer."""
    script = tmp_path / "editor.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return f"{sys.executable} {script}"


@pytest.fixture
def recorded_asks(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    monkeypatch.delenv("VISUAL", raising=False)
    calls: list[str] = []

    def fake_ask(prompt: str) -> int:
        calls.append(prompt)
        return EXIT_OK

    with patch("sigil.cli.step.ask", side_effect=fake_ask):
        yield calls


def test_command_verb_is_not_registered() -> None:
    result = CliRunner().invoke(cli, ["command", "update example"])

    assert result.exit_code == EXIT_USAGE
    assert "No such command" in result.stderr


def test_ask_verb_accepts_piped_input() -> None:
    ask_calls = []

    def fake_ask(*args: object, **kwargs: object) -> int:
        ask_calls.append((args, kwargs))
        return EXIT_OK

    with patch("sigil.cli.step.ask", side_effect=fake_ask):
        ask_result = CliRunner().invoke(
            cli,
            ["ask", "review"],
            input="diff\n",
        )

    assert ask_result.exit_code == EXIT_OK, ask_result.output
    assert ask_result.output == ""
    assert ask_calls == [
        (
            ("review\n\nPiped input:\ndiff\n",),
            {},
        )
    ]


def test_ask_without_question_composes_in_editor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recorded_asks: list[str],
) -> None:
    seen = tmp_path / "seen.txt"
    monkeypatch.setenv(
        "EDITOR",
        editor_command(
            tmp_path,
            f"""\
            import pathlib, sys
            buffer = pathlib.Path(sys.argv[1])
            pathlib.Path(r"{seen}").write_text(
                buffer.read_text(encoding="utf-8"), encoding="utf-8"
            )
            buffer.write_text("what changed?\\n# scratch note\\n", encoding="utf-8")
            """,
        ),
    )

    result = CliRunner().invoke(cli, ["ask"])

    assert result.exit_code == EXIT_OK, result.output
    assert recorded_asks == ["what changed?"]
    assert "# , (ask)" in seen.read_text(encoding="utf-8")


def test_ask_editor_aborts_when_saved_text_is_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recorded_asks: list[str],
) -> None:
    monkeypatch.setenv(
        "EDITOR",
        editor_command(
            tmp_path,
            """\
            import pathlib, sys
            pathlib.Path(sys.argv[1]).write_text(
                "# nothing to ask\\n\\n", encoding="utf-8"
            )
            """,
        ),
    )

    result = CliRunner().invoke(cli, ["ask"])

    assert result.exit_code == EXIT_ERROR
    assert "aborted: empty question" in result.stderr
    assert recorded_asks == []


def test_ask_piped_stdin_without_question_skips_editor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recorded_asks: list[str],
) -> None:
    marker = tmp_path / "editor-ran"
    monkeypatch.setenv(
        "EDITOR",
        editor_command(
            tmp_path,
            f"""\
            import pathlib
            pathlib.Path(r"{marker}").write_text("ran", encoding="utf-8")
            """,
        ),
    )

    result = CliRunner().invoke(cli, ["ask"], input="diff\n")

    assert result.exit_code == EXIT_OK, result.output
    assert recorded_asks == ["Piped input:\ndiff\n"]
    assert not marker.exists()


@pytest.mark.parametrize(
    ("workflow", "glyph"),
    [("propose", ",,"), ("do", ",,,")],
)
def test_step_without_objective_composes_in_editor(
    workflow: str,
    glyph: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VISUAL", raising=False)
    seen = tmp_path / "seen.txt"
    monkeypatch.setenv(
        "EDITOR",
        editor_command(
            tmp_path,
            f"""\
            import pathlib, sys
            buffer = pathlib.Path(sys.argv[1])
            pathlib.Path(r"{seen}").write_text(
                buffer.read_text(encoding="utf-8"), encoding="utf-8"
            )
            buffer.write_text("tighten the tests\\n", encoding="utf-8")
            """,
        ),
    )
    calls = []

    def fake_step(objective: str, **kwargs: object) -> int:
        calls.append(objective)
        return EXIT_OK

    with patch(f"sigil.cli.step.{workflow}", side_effect=fake_step):
        result = CliRunner().invoke(cli, ["step", "--workflow", workflow])

    assert result.exit_code == EXIT_OK, result.output
    assert calls == ["tighten the tests"]
    assert f"# {glyph} ({workflow})" in seen.read_text(encoding="utf-8")


def test_step_editor_aborts_when_saved_text_is_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv(
        "EDITOR",
        editor_command(
            tmp_path,
            """\
            import pathlib, sys
            pathlib.Path(sys.argv[1]).write_text("# nothing\\n", encoding="utf-8")
            """,
        ),
    )
    calls = []

    def fake_propose(objective: str, **kwargs: object) -> int:
        calls.append(objective)
        return EXIT_OK

    with patch("sigil.cli.step.propose", side_effect=fake_propose):
        result = CliRunner().invoke(cli, ["step", "--workflow", "propose"])

    assert result.exit_code == EXIT_ERROR
    assert "aborted: empty objective" in result.stderr
    assert calls == []


def test_step_continue_skips_editor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VISUAL", raising=False)
    marker = tmp_path / "editor-ran"
    monkeypatch.setenv(
        "EDITOR",
        editor_command(
            tmp_path,
            f"""\
            import pathlib
            pathlib.Path(r"{marker}").write_text("ran", encoding="utf-8")
            """,
        ),
    )
    calls = []

    def fake_propose(objective: str, **kwargs: object) -> int:
        calls.append(objective)
        return EXIT_OK

    with (
        patch("sigil.cli.step.propose", side_effect=fake_propose),
        patch("sigil.handoff.append_shell_result", return_value={}),
    ):
        result = CliRunner().invoke(
            cli, ["step", "--workflow", "propose", "--continue"]
        )

    assert result.exit_code == EXIT_OK, result.output
    assert calls == [CONTINUE_OBJECTIVE]
    assert not marker.exists()


def test_main_reports_model_runtime_error(capsys, monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", StringIO(""))
    with patch(
        "sigil.cli.step.ask",
        side_effect=RuntimeError("model request failed: connection reset"),
    ):
        code = main(["ask", "why"])

    captured = capsys.readouterr()
    assert code == EXIT_MODEL_UNAVAILABLE
    assert "sigil: model request failed: connection reset" in captured.err
    assert "sigil doctor" in captured.err


def test_patch_side_effect_raises_exception_classes() -> None:
    from sigil.cli import step as step_module

    with patch("sigil.cli.step.ask", side_effect=RuntimeError):
        with pytest.raises(RuntimeError):
            step_module.ask("boom")
