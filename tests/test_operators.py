from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
import click
from click.testing import CliRunner

from _patch import patch
from sigil.cli import cli
from sigil.cli.operators import run_operator
from sigil.routes.operators import (
    create_invocation,
    parse_operator_token,
)


def read_global_events(root: Path) -> list[dict[str, object]]:
    path = root / "events.jsonl"
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def invoke_op(args: list[str], input: str | None = None):
    @click.command("glyph-test")
    @click.argument("glyph")
    @click.argument("prompt_parts", nargs=-1)
    @click.option("--json", "json_output", is_flag=True)
    def command(
        glyph: str,
        prompt_parts: tuple[str, ...],
        json_output: bool,
    ) -> int:
        return run_operator(glyph, prompt_parts, json_output)

    return CliRunner().invoke(command, args, input=input)


@pytest.mark.parametrize(
    ("token", "base", "depth"),
    [
        (",", ",", 1),
        (",,", ",", 2),
        (",,,", ",", 3),
        ("?", "?", 1),
    ],
)
def test_parse_operator_token_repetition(
    token: str,
    base: str,
    depth: int,
) -> None:
    assert parse_operator_token(token) == (base, depth)


@pytest.mark.parametrize(
    "token",
    [
        "",
        "??",
        "?^",
        "?:",
        "abc",
        ":",
        "^",
        "^^",
        "^^^",
        "???",
        "????",
        ",,,,",
        "@",
        "@@",
        "@@@",
        "@@@@",
        "^^^^",
    ],
)
def test_parse_operator_token_rejects_invalid_tokens(token: str) -> None:
    with pytest.raises(ValueError):
        parse_operator_token(token)


def test_create_invocation_names_operator() -> None:
    invocation = create_invocation(
        ",",
        prompt="review risky changes",
        stdin="diff",
        mode="pipeline",
    )
    assert invocation.base == ","
    assert invocation.depth == 1
    assert invocation.name == "ask"
    assert invocation.prompt == "review risky changes"
    assert invocation.stdin == "diff"
    assert invocation.mode == "pipeline"


def test_op_cli_json_reports_parsed_invocation() -> None:
    result = invoke_op(
        ["--json", ",", "review", "risky", "changes"],
        input="diff --git a/file b/file\n",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {
        "glyph": ",",
        "base": ",",
        "depth": 1,
        "name": "ask",
        "prompt": "review risky changes",
        "stdin": "diff --git a/file b/file\n",
        "mode": "pipeline",
    }


def test_create_invocation_names_comma_depths() -> None:
    assert create_invocation(",").name == "ask"
    assert create_invocation(",,").name == "propose"
    assert create_invocation(",,,").name == "do"


def test_op_cli_json_reports_status_invocation() -> None:
    result = invoke_op(["--json", "?"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {
        "glyph": "?",
        "base": "?",
        "depth": 1,
        "name": "status",
        "prompt": "",
        "stdin": "",
        "mode": "pipeline",
    }


def test_op_cli_json_does_not_run_operator() -> None:
    with patch("sigil.cli.operators.ask", side_effect=AssertionError("no model")):
        result = invoke_op(
            ["--json", ",", "review"],
            input="diff\n",
        )
    assert result.exit_code == 0, result.output


def test_op_cli_runs_piped_comma_through_readonly_route() -> None:
    calls = []

    def fake_ask(*args: object, **kwargs: object) -> int:
        calls.append((args, kwargs))
        return 0

    with (
        patch(
            "sigil.cli.operators.confirm_piped_input",
            side_effect=AssertionError("no prompt"),
        ),
        patch("sigil.cli.operators.ask", side_effect=fake_ask),
    ):
        result = invoke_op(
            [",", "review", "risky", "changes"],
            input="diff --git a/file b/file\n",
        )
    assert result.exit_code == 0, result.output
    assert calls == [
        (
            ("review risky changes\n\nPiped input:\ndiff --git a/file b/file\n",),
            {
                "glyph": ",",
                "tools": "read,grep,ls",
                "append_transcript": False,
                "json_output": False,
            },
        )
    ]


def test_comma_operator_uses_readonly_route() -> None:
    calls = []

    def fake_ask(*args: object, **kwargs: object) -> int:
        calls.append((args, kwargs))
        return 0

    with patch("sigil.cli.operators.ask", side_effect=fake_ask):
        result = invoke_op([",", "first", "question"])

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            ("first question",),
            {
                "glyph": ",",
                "tools": "read,grep,ls",
                "append_transcript": False,
                "json_output": False,
            },
        ),
    ]


def test_comma_operator_continues_existing_answer_transcript() -> None:
    calls = []

    def fake_ask(*args: object, **kwargs: object) -> int:
        calls.append((args, kwargs))
        return 0

    with (
        patch(
            "sigil.cli.operators.discussion_turns",
            return_value=[
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": "first answer"},
            ],
        ),
        patch("sigil.cli.operators.ask", side_effect=fake_ask),
    ):
        result = invoke_op([",", "follow", "up"])

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            ("follow up",),
            {
                "glyph": ",",
                "tools": "read,grep,ls",
                "append_transcript": True,
                "history": [
                    {"role": "user", "content": "first question"},
                    {"role": "assistant", "content": "first answer"},
                ],
                "json_output": False,
            },
        ),
    ]


def test_question_operator_routes_to_status() -> None:
    with patch("sigil.cli.operators.ask", side_effect=AssertionError("no ask")):
        result = invoke_op(["?"])

    assert result.exit_code == 0
    assert result.output == "clean\n"


def test_at_operator_is_rejected() -> None:
    result = invoke_op(["@", "fix"])

    assert result.exit_code == 2
    assert "unsupported operator: @" in result.output


def test_command_verb_is_not_registered() -> None:
    result = CliRunner().invoke(cli, ["command", "update example"])

    assert result.exit_code == 2
    assert "No such command" in result.stderr


def test_double_comma_runs_confirmed_agent_step() -> None:
    calls = []

    def fake_run_agent_step(*args: object, **kwargs: object) -> int:
        calls.append((args, kwargs))
        return 0

    with patch("sigil.cli.operators.run_agent_step", side_effect=fake_run_agent_step):
        result = invoke_op([",,", "update", "it"])

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            ("update it",),
            {
                "stdin_text": "",
                "glyph": ",,",
            },
        )
    ]


def test_op_cli_routes_double_comma_to_agent_stepper() -> None:
    calls = []

    def fake_run_agent_step(*args: object, **kwargs: object) -> int:
        calls.append((args, kwargs))
        return 0

    with patch("sigil.cli.operators.run_agent_step", side_effect=fake_run_agent_step):
        result = invoke_op([",,", "say", "done"])

    assert result.exit_code == 0
    assert result.stdout == ""
    assert calls[0][0] == ("say done",)
    assert calls[0][1]["glyph"] == ",,"


def test_triple_comma_routes_to_auto_approved_agent_stepper() -> None:
    calls = []

    def fake_run_agent_step(*args: object, **kwargs: object) -> int:
        calls.append((args, kwargs))
        return 0

    with patch("sigil.cli.operators.run_agent_step", side_effect=fake_run_agent_step):
        result = invoke_op([",,,", "publish"])

    assert result.exit_code == 0
    assert result.stdout == ""
    assert calls[0][0] == ("publish",)
    assert calls[0][1]["glyph"] == ",,,"


def test_comma_agent_glyphs_print_tool_start_while_agent_runs() -> None:
    rendered: list[tuple[str, dict[str, object]]] = []

    def fake_render_tool_start(
        name: str,
        params: dict[str, object],
        *,
        output: object,
    ) -> None:
        del output
        rendered.append((name, params))

    def fake_run_agent_turn(*args: object, **kwargs: object):
        del args
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
        assert rendered[-1] == ("read", {"path": "README.md"})
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
        from sigil.zeta.agent import AgentTurnResult

        return AgentTurnResult(final_text="done", events=[tool_call, tool_result])

    with tempfile.TemporaryDirectory() as tmp_dir:
        old_state_dir = os.environ.get("SIGIL_STATE_DIR")
        old_session_id = os.environ.get("SIGIL_SESSION_ID")
        os.environ["SIGIL_STATE_DIR"] = tmp_dir
        os.environ["SIGIL_SESSION_ID"] = "streaming-agent-glyphs"
        try:
            with (
                patch("sigil.routes.zeta_step.ensure_server", return_value=True),
                patch(
                    "sigil.routes.zeta_step.run_agent_turn",
                    side_effect=fake_run_agent_turn,
                ),
                patch(
                    "sigil.routes.zeta_step.render_tool_start",
                    side_effect=fake_render_tool_start,
                ),
            ):
                for glyph in (",,", ",,,"):
                    result = invoke_op([glyph, "inspect", glyph])

                    assert result.exit_code == 0, result.output
                    assert "done" in result.stdout
        finally:
            if old_state_dir is None:
                os.environ.pop("SIGIL_STATE_DIR", None)
            else:
                os.environ["SIGIL_STATE_DIR"] = old_state_dir
            if old_session_id is None:
                os.environ.pop("SIGIL_SESSION_ID", None)
            else:
                os.environ["SIGIL_SESSION_ID"] = old_session_id


def test_op_cli_returns_agent_stepper_status() -> None:
    with patch("sigil.cli.operators.run_agent_step", return_value=7):
        result = invoke_op([",,", "fail"])

    assert result.exit_code == 7
    assert result.stdout == ""
    assert result.stderr == ""


def test_op_cli_rejects_caret_before_model_or_confirmation() -> None:
    with patch(
        "sigil.cli.operators.confirm_piped_input",
        side_effect=AssertionError("no prompt"),
    ):
        result = invoke_op(["^", "status"], input="notes\n")

    assert result.exit_code == 2
    assert "unsupported operator: ^" in result.output


def test_piped_triple_comma_denies_input_before_agent_step() -> None:
    with (
        patch("sigil.cli.operators.confirm_piped_input", return_value=False),
        patch("sigil.cli.operators.run_agent_step", side_effect=AssertionError),
    ):
        result = invoke_op([",,,", "ship"], input="notes\n")

    assert result.exit_code == 2
    assert "piped input declined" in result.stderr


def test_op_cli_does_not_confirm_piped_comma_before_readonly_route() -> None:
    calls = []

    def fake_ask(*args: object, **kwargs: object) -> int:
        calls.append((args, kwargs))
        return 0

    with (
        patch(
            "sigil.cli.operators.confirm_piped_input",
            side_effect=AssertionError("no prompt"),
        ),
        patch("sigil.cli.operators.ask", side_effect=fake_ask),
    ):
        result = invoke_op([",", "summarize"], input="notes\n")

    assert result.exit_code == 0
    assert calls == [
        (
            ("summarize\n\nPiped input:\nnotes\n",),
            {
                "glyph": ",",
                "tools": "read,grep,ls",
                "append_transcript": False,
                "json_output": False,
            },
        )
    ]


def test_op_cli_routes_piped_question_operator_to_status_without_confirmation() -> None:
    calls = []

    def fake_ask(*args: object, **kwargs: object) -> int:
        calls.append((args, kwargs))
        return 0

    with (
        patch(
            "sigil.cli.operators.confirm_piped_input",
            side_effect=AssertionError("no prompt"),
        ),
        patch("sigil.cli.operators.ask", side_effect=fake_ask),
    ):
        result = invoke_op(["?", "review"], input="diff\n")

    assert result.exit_code == 0
    assert calls == []
    assert result.output == "clean\n"


def test_ask_follow_up_sends_piped_input_without_confirmation() -> None:
    calls = []

    def fake_ask(*args: object, **kwargs: object) -> int:
        calls.append((args, kwargs))
        return 0

    with (
        patch(
            "sigil.cli.operators.confirm_piped_input",
            side_effect=AssertionError("no prompt"),
        ),
        patch("sigil.cli.ask.ask", side_effect=fake_ask),
    ):
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
                "append_transcript": True,
                "history": [],
                "json_output": False,
            },
        )
    ]


def test_op_cli_routes_piped_comma_to_readonly_answer() -> None:
    calls = []

    def fake_ask(*args: object, **kwargs: object) -> int:
        calls.append((args, kwargs))
        return 0

    with (
        patch(
            "sigil.cli.operators.confirm_piped_input",
            side_effect=AssertionError("no prompt"),
        ),
        patch("sigil.cli.operators.ask", side_effect=fake_ask),
    ):
        result = invoke_op([",", "summarize"], input="notes\n")

    assert result.exit_code == 0
    assert calls == [
        (
            ("summarize\n\nPiped input:\nnotes\n",),
            {
                "glyph": ",",
                "tools": "read,grep,ls",
                "append_transcript": False,
                "json_output": False,
            },
        )
    ]


def test_op_cli_confirms_piped_double_comma_before_agent_step() -> None:
    calls = []

    def fake_run_agent_step(*args: object, **kwargs: object) -> int:
        calls.append((args, kwargs))
        return 0

    with (
        patch("sigil.cli.operators.confirm_piped_input", return_value=True),
        patch("sigil.cli.operators.run_agent_step", side_effect=fake_run_agent_step),
    ):
        result = invoke_op([",,", "summarize"], input="notes\n")

    assert result.exit_code == 0
    assert result.stdout == ""
    assert calls[0][0] == ("summarize",)
    assert calls[0][1]["stdin_text"] == "notes\n"
    assert calls[0][1]["glyph"] == ",,"


def test_op_cli_routes_piped_triple_comma_to_auto_agent_step() -> None:
    calls = []

    def fake_run_agent_step(*args: object, **kwargs: object) -> int:
        calls.append((args, kwargs))
        return 0

    with (
        patch("sigil.cli.operators.confirm_piped_input", return_value=True),
        patch("sigil.cli.operators.run_agent_step", side_effect=fake_run_agent_step),
    ):
        result = invoke_op([",,,", "summarize"], input="notes\n")

    assert result.exit_code == 0
    assert result.stdout == ""
    assert calls[0][0] == ("summarize",)
    assert calls[0][1]["stdin_text"] == "notes\n"
    assert calls[0][1]["glyph"] == ",,,"


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


def test_op_cli_rejects_mixed_glyphs() -> None:
    result = invoke_op(["?^"])
    assert result.exit_code == 2
    assert "operator token must repeat one glyph: ?^" in result.output


def test_op_cli_rejects_transform_until_colon_operator_exists() -> None:
    result = invoke_op([":json"])
    assert result.exit_code == 2
    assert "unsupported operator: :" in result.output
