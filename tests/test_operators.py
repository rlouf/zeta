from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import cast

import pytest
import click
from click.testing import CliRunner

from _patch import patch, patch_dict
from sigil.cli import cli
from sigil.cli.operators import run_operator
from sigil.operators import (
    create_invocation,
    parse_operator_token,
    proposal_user_prompt,
)
from sigil.session import record_turn
from sigil.state import append_jsonl, read_jsonl


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
        "?",
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
    assert invocation.name == "read"
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
        "name": "read",
        "prompt": "review risky changes",
        "stdin": "diff --git a/file b/file\n",
        "mode": "pipeline",
    }


def test_op_cli_json_does_not_run_operator() -> None:
    with patch("sigil.operators.chat_text", side_effect=AssertionError("no model")):
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
                "json_output": False,
            },
        ),
    ]


def test_question_operator_is_rejected() -> None:
    with patch("sigil.cli.operators.ask", side_effect=AssertionError("no ask")):
        result = invoke_op(["?", "explain", "this"])

    assert result.exit_code == 2
    assert "unsupported operator: ?" in result.output


def test_at_operator_is_rejected() -> None:
    result = invoke_op(["@", "fix"])

    assert result.exit_code == 2
    assert "unsupported operator: @" in result.output


def test_command_verb_runs_piped_proposal_operator() -> None:
    calls = {}

    def fake_chat_json(
        system: str, user: str, schema: dict[str, object]
    ) -> dict[str, str]:
        calls["system"] = system
        calls["user"] = user
        calls["schema"] = schema
        return {
            "kind": "command",
            "body": "uv run pytest",
            "explanation": "Tests validate the current code path before cleanup.",
        }

    with (
        patch("sigil.operators.ensure_server", return_value=True),
        patch("sigil.operators.chat_json", side_effect=fake_chat_json),
        patch("sigil.operators.append_event", return_value={}),
    ):
        result = CliRunner().invoke(
            cli,
            ["command", "draft an executive summary"],
            input="meeting notes\n",
        )
    assert result.exit_code == 0, result.output
    assert result.output == (
        "uv run pytest\nTests validate the current code path before cleanup.\n"
    )
    assert "Produce one typed proposal" in str(calls["system"])
    assert "Prompt: draft an executive summary" in str(calls["user"])
    schema = calls["schema"]
    assert schema["properties"]["kind"]["enum"] == ["command"]
    assert "body" in schema["properties"]
    assert "explanation" in schema["properties"]


def test_command_verb_rejects_non_command_proposals() -> None:
    with patch(
        "sigil.cli.command.run_command_proposal",
        side_effect=RuntimeError("command route did not produce a proposal"),
    ):
        result = CliRunner().invoke(cli, ["command", "update example"])

    assert result.exit_code == 1
    assert "did not produce a proposal" in result.stderr


def test_double_comma_runs_confirmed_agent_step() -> None:
    calls = []

    def fake_run_act_stepper(*args: object, **kwargs: object) -> int:
        calls.append((args, kwargs))
        return 0

    with patch("sigil.cli.operators.run_act_stepper", side_effect=fake_run_act_stepper):
        result = invoke_op([",,", "update", "it"])

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            (),
            {
                "objective": "update it",
                "stdin_text": "",
                "confirm_step": True,
                "glyph": ",,",
            },
        )
    ]


def test_op_cli_routes_double_comma_to_agent_stepper() -> None:
    calls = []

    def fake_run_act_stepper(*args: object, **kwargs: object) -> int:
        calls.append((args, kwargs))
        return 0

    with patch("sigil.cli.operators.run_act_stepper", side_effect=fake_run_act_stepper):
        result = invoke_op([",,", "say", "done"])

    assert result.exit_code == 0
    assert result.stdout == ""
    assert calls[0][1]["objective"] == "say done"
    assert calls[0][1]["confirm_step"] is True
    assert calls[0][1]["glyph"] == ",,"


def test_triple_comma_routes_to_auto_approved_agent_stepper() -> None:
    calls = []

    def fake_run_act_stepper(*args: object, **kwargs: object) -> int:
        calls.append((args, kwargs))
        return 0

    with patch("sigil.cli.operators.run_act_stepper", side_effect=fake_run_act_stepper):
        result = invoke_op([",,,", "publish"])

    assert result.exit_code == 0
    assert result.stdout == ""
    assert calls[0][1]["objective"] == "publish"
    assert calls[0][1]["confirm_step"] is False
    assert calls[0][1]["glyph"] == ",,,"


def test_op_cli_returns_agent_stepper_status() -> None:
    with patch("sigil.cli.operators.run_act_stepper", return_value=7):
        result = invoke_op([",,", "fail"])

    assert result.exit_code == 7
    assert result.stdout == ""
    assert result.stderr == ""


def test_op_cli_rejects_caret_before_model_or_confirmation() -> None:
    with (
        patch(
            "sigil.cli.operators.confirm_piped_input",
            side_effect=AssertionError("no prompt"),
        ),
        patch("sigil.operators.chat_json", side_effect=AssertionError("no model")),
    ):
        result = invoke_op(["^", "status"], input="notes\n")

    assert result.exit_code == 2
    assert "unsupported operator: ^" in result.output


def test_triple_comma_creates_act_and_executes_one_auto_approved_step() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        old_state_dir = os.environ.get("SIGIL_STATE_DIR")
        old_session_id = os.environ.get("SIGIL_SESSION_ID")
        os.environ["SIGIL_STATE_DIR"] = tmp_dir
        os.environ["SIGIL_SESSION_ID"] = "act-session"
        events = []
        zeta_calls = []

        def fake_append_event(event: dict[str, object]) -> dict[str, object]:
            stored = {"id": f"event-{len(events)}", **event}
            events.append(stored)
            return stored

        def fake_run_pi(*args: object, **kwargs: object) -> int:
            zeta_calls.append((args, kwargs))
            return 0

        try:
            with (
                patch(
                    "sigil.acts.prompt_on_tty",
                    side_effect=AssertionError("no prompt"),
                ),
                patch("sigil.acts.run_zeta_agent_step", side_effect=fake_run_pi),
                patch("sigil.acts.append_event", side_effect=fake_append_event),
            ):
                result = invoke_op([",,,", "ship", "it"])
            act_events = read_jsonl("last-act.jsonl")
        finally:
            if old_state_dir is None:
                os.environ.pop("SIGIL_STATE_DIR", None)
            else:
                os.environ["SIGIL_STATE_DIR"] = old_state_dir
            if old_session_id is None:
                os.environ.pop("SIGIL_SESSION_ID", None)
            else:
                os.environ["SIGIL_SESSION_ID"] = old_session_id

    assert result.exit_code == 0, result.output
    assert "objective: ship it" not in result.output
    assert "❯ tools  read,grep,bash,edit,write" in result.output
    assert len(zeta_calls) == 1
    assert zeta_calls[0][1]["glyph"] == ",,,"
    assert [event["type"] for event in events] == [
        "act_created",
        "act_step_decision",
        "act_step_executed",
        "act_completed",
    ]
    assert [event["type"] for event in act_events] == [
        "act_created",
        "act_step_decision",
        "act_step_executed",
        "act_completed",
    ]
    latest_act = act_events[-1]["act"]
    assert latest_act["status"] == "completed"
    assert latest_act["approval"] == "auto"
    assert latest_act["steps"][0]["decision"] == "auto_accepted"
    assert latest_act["steps"][0]["status"] == "done"


def test_confirmed_act_can_edit_tools_before_execution() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp_dir, "SIGIL_SESSION_ID": "act-session"},
        ):
            zeta_calls = []

            def fake_run_pi(*args: object, **kwargs: object) -> int:
                zeta_calls.append((args, kwargs))
                return 0

            prompts = iter(["e\n", "y\n"])

            def fake_prompt(*args: object, **kwargs: object) -> str:
                del args, kwargs
                return next(prompts)

            with (
                patch("sigil.acts.prompt_on_tty", side_effect=fake_prompt),
                patch("sigil.acts.edit_tools", return_value=["read", "grep", "edit"]),
                patch("sigil.acts.run_zeta_agent_step", side_effect=fake_run_pi),
            ):
                result = invoke_op([",,", "ship", "it"])
            act_events = read_jsonl("last-act.jsonl")

    assert result.exit_code == 0, result.output
    assert len(zeta_calls) == 1
    assert zeta_calls[0][1]["tools"] == "read,grep,edit"
    step = act_events[-1]["act"]["steps"][0]
    assert step["edited_tools"] is True
    assert step["tools"] == ["read", "grep", "edit"]
    assert step["command"] == "zeta --tools read,grep,edit"


def test_confirmed_act_leaves_editor_errors_visible() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp_dir, "SIGIL_SESSION_ID": "act-session"},
        ):
            clear_calls = []

            def fake_clear_lines(*args: object, **kwargs: object) -> None:
                clear_calls.append((args, kwargs))

            with (
                patch("sigil.acts.prompt_on_tty", return_value="e\n"),
                patch("sigil.acts.edit_tools", return_value=None),
                patch("sigil.acts.clear_lines_on_tty", side_effect=fake_clear_lines),
                patch(
                    "sigil.acts.run_zeta_agent_step",
                    side_effect=AssertionError("no zeta"),
                ),
            ):
                result = invoke_op([",,", "ship", "it"])

    assert result.exit_code == 0, result.output
    assert clear_calls == []


def test_piped_triple_comma_denies_input_before_act_generation() -> None:
    with (
        patch("sigil.cli.operators.confirm_piped_input", return_value=False),
        patch("sigil.acts.run_zeta_agent_step", side_effect=AssertionError("no zeta")),
    ):
        result = invoke_op([",,,", "ship"], input="notes\n")

    assert result.exit_code == 2
    assert "piped input declined" in result.stderr


def test_act_zeta_step_invokes_zeta_runner() -> None:
    captured: dict[str, object] = {}

    def fake_run_agent_step(*args: object, **kwargs: object) -> int:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return 0

    with patch("sigil.acts.run_agent_step", side_effect=fake_run_agent_step):
        from sigil.acts import run_zeta_agent_step

        result = run_zeta_agent_step(
            {"objective": "repair", "stdin": "notes", "glyph": ",,"},
            tools="read,grep,edit",
        )

    assert result == 0
    assert captured["args"] == ("repair",)
    kwargs = cast("dict[str, object]", captured["kwargs"])
    assert kwargs["glyph"] == ",,"
    assert isinstance(kwargs["system"], str)
    assert "bounded shell-native edit route" in kwargs["system"]
    assert kwargs["stdin_text"] == "notes"
    assert kwargs["allowed_tools"] == ["read", "grep", "edit"]


def test_act_resume_executes_pending_step_without_regenerating() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        old_state_dir = os.environ.get("SIGIL_STATE_DIR")
        old_session_id = os.environ.get("SIGIL_SESSION_ID")
        os.environ["SIGIL_STATE_DIR"] = tmp_dir
        os.environ["SIGIL_SESSION_ID"] = "act-session"
        zeta_calls = []

        def fake_run_pi(*args: object, **kwargs: object) -> int:
            zeta_calls.append((args, kwargs))
            return 0

        try:
            append_jsonl(
                "last-act.jsonl",
                {
                    "type": "act_created",
                    "act": {
                        "act_id": "act",
                        "objective": "ship it",
                        "status": "active",
                        "steps": [
                            {
                                "id": "1",
                                "title": "Run one Zeta edit step",
                                "command": "zeta --tools read,grep,bash,edit,write",
                                "explanation": "Run the pending edit.",
                                "status": "pending",
                            },
                        ],
                    },
                },
            )
            with (
                patch("sigil.acts.create_act", side_effect=AssertionError("no create")),
                patch("sigil.acts.prompt_on_tty", return_value="y\n"),
                patch("sigil.acts.run_zeta_agent_step", side_effect=fake_run_pi),
            ):
                result = CliRunner().invoke(cli, ["act", "resume"])
            act_events = read_jsonl("last-act.jsonl")
        finally:
            if old_state_dir is None:
                os.environ.pop("SIGIL_STATE_DIR", None)
            else:
                os.environ["SIGIL_STATE_DIR"] = old_state_dir
            if old_session_id is None:
                os.environ.pop("SIGIL_SESSION_ID", None)
            else:
                os.environ["SIGIL_SESSION_ID"] = old_session_id

    assert result.exit_code == 0, result.output
    assert "objective: ship it" not in result.output
    assert "❯ tools  read,grep,bash,edit,write" in result.output
    assert len(zeta_calls) == 1
    assert act_events[-1]["act"]["status"] == "completed"


def test_act_replaces_stale_same_objective_act_without_pending_step() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        old_state_dir = os.environ.get("SIGIL_STATE_DIR")
        old_session_id = os.environ.get("SIGIL_SESSION_ID")
        os.environ["SIGIL_STATE_DIR"] = tmp_dir
        os.environ["SIGIL_SESSION_ID"] = "act-session"
        zeta_calls = []

        def fake_run_pi(*args: object, **kwargs: object) -> int:
            zeta_calls.append((args, kwargs))
            return 0

        try:
            append_jsonl(
                "last-act.jsonl",
                {
                    "type": "act_created",
                    "act": {
                        "act_id": "stale-act",
                        "objective": "ship it",
                        "status": "active",
                        "steps": [
                            {
                                "id": "1",
                                "title": "Run one Zeta edit step",
                                "command": "zeta --tools read,grep,bash,edit,write",
                                "explanation": "Already handled.",
                                "status": "done",
                            },
                        ],
                    },
                },
            )
            with (
                patch(
                    "sigil.acts.prompt_on_tty", side_effect=AssertionError("no prompt")
                ),
                patch("sigil.acts.run_zeta_agent_step", side_effect=fake_run_pi),
            ):
                result = invoke_op([",,,", "ship", "it"])
            act_events = read_jsonl("last-act.jsonl")
        finally:
            if old_state_dir is None:
                os.environ.pop("SIGIL_STATE_DIR", None)
            else:
                os.environ["SIGIL_STATE_DIR"] = old_state_dir
            if old_session_id is None:
                os.environ.pop("SIGIL_SESSION_ID", None)
            else:
                os.environ["SIGIL_SESSION_ID"] = old_session_id

    assert result.exit_code == 0, result.output
    assert "objective: ship it" not in result.output
    assert "❯ tools  read,grep,bash,edit,write" in result.output
    assert len(zeta_calls) == 1
    created = [event for event in act_events if event["type"] == "act_created"]
    assert created[-1]["act"]["act_id"] != "stale-act"


def test_act_show_and_abort_use_last_act_state() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        old_state_dir = os.environ.get("SIGIL_STATE_DIR")
        old_session_id = os.environ.get("SIGIL_SESSION_ID")
        os.environ["SIGIL_STATE_DIR"] = tmp_dir
        os.environ["SIGIL_SESSION_ID"] = "act-session"
        try:
            append_jsonl(
                "last-act.jsonl",
                {
                    "type": "act_created",
                    "act": {
                        "act_id": "act",
                        "objective": "ship it",
                        "status": "active",
                        "steps": [
                            {
                                "id": "1",
                                "title": "Inspect",
                                "command": "git status --short",
                                "explanation": "",
                                "status": "pending",
                            }
                        ],
                    },
                },
            )
            shown = CliRunner().invoke(cli, ["act", "show"])
            aborted = CliRunner().invoke(cli, ["act", "abort", "--json"])
            act_events = read_jsonl("last-act.jsonl")
        finally:
            if old_state_dir is None:
                os.environ.pop("SIGIL_STATE_DIR", None)
            else:
                os.environ["SIGIL_STATE_DIR"] = old_state_dir
            if old_session_id is None:
                os.environ.pop("SIGIL_SESSION_ID", None)
            else:
                os.environ["SIGIL_SESSION_ID"] = old_session_id

    assert shown.exit_code == 0, shown.output
    assert "objective: ship it" in shown.output
    assert aborted.exit_code == 0, aborted.output
    assert json.loads(aborted.output)["aborted"]
    assert act_events[-1]["act"]["status"] == "aborted"


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
                "json_output": False,
            },
        )
    ]


def test_op_cli_rejects_piped_question_operator() -> None:
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

    assert result.exit_code == 2
    assert calls == []
    assert "unsupported operator: ?" in result.output


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
                "json_output": False,
            },
        )
    ]


def test_op_cli_confirms_piped_double_comma_before_agent_step() -> None:
    calls = []

    def fake_run_act_stepper(*args: object, **kwargs: object) -> int:
        calls.append((args, kwargs))
        return 0

    with (
        patch("sigil.cli.operators.confirm_piped_input", return_value=True),
        patch("sigil.cli.operators.run_act_stepper", side_effect=fake_run_act_stepper),
    ):
        result = invoke_op([",,", "summarize"], input="notes\n")

    assert result.exit_code == 0
    assert result.stdout == ""
    assert calls[0][1]["objective"] == "summarize"
    assert calls[0][1]["stdin_text"] == "notes\n"
    assert calls[0][1]["confirm_step"] is True
    assert calls[0][1]["glyph"] == ",,"


def test_op_cli_routes_piped_triple_comma_to_auto_agent_step() -> None:
    calls = []

    def fake_run_act_stepper(*args: object, **kwargs: object) -> int:
        calls.append((args, kwargs))
        return 0

    with (
        patch("sigil.cli.operators.confirm_piped_input", return_value=True),
        patch("sigil.cli.operators.run_act_stepper", side_effect=fake_run_act_stepper),
    ):
        result = invoke_op([",,,", "summarize"], input="notes\n")

    assert result.exit_code == 0
    assert result.stdout == ""
    assert calls[0][1]["objective"] == "summarize"
    assert calls[0][1]["stdin_text"] == "notes\n"
    assert calls[0][1]["confirm_step"] is False
    assert calls[0][1]["glyph"] == ",,,"


def test_verb_commands_run_piped_stream_operators() -> None:
    ask_calls = []
    json_calls = []

    def fake_ask(*args: object, **kwargs: object) -> int:
        ask_calls.append((args, kwargs))
        return 0

    def fake_chat_json(
        system: str, user: str, schema: dict[str, object]
    ) -> dict[str, str]:
        json_calls.append((system, user, schema))
        return {
            "kind": "command",
            "body": "stream result",
            "explanation": "because stdin",
        }

    with (
        patch("sigil.operators.ensure_server", return_value=True),
        patch("sigil.operators.chat_json", side_effect=fake_chat_json),
        patch("sigil.cli.ask.ask", side_effect=fake_ask),
        patch("sigil.operators.append_event", return_value={}),
    ):
        ask_result = CliRunner().invoke(
            cli,
            ["ask", "review"],
            input="diff\n",
        )
        command_result = CliRunner().invoke(
            cli,
            ["command", "summarize"],
            input="notes\n",
        )

    assert ask_result.exit_code == 0, ask_result.output
    assert command_result.exit_code == 0, command_result.output
    assert ask_result.output == ""
    assert command_result.output == "stream result\nbecause stdin\n"
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
    assert "Operator: command (command)" in json_calls[0][1]


def test_command_verb_generates_proposal_without_stdin() -> None:
    with (
        patch("sigil.operators.ensure_server", return_value=True),
        patch(
            "sigil.operators.chat_json",
            return_value={
                "kind": "command",
                "body": "find . -size +10M",
                "explanation": "Lists large files.",
            },
        ),
        patch("sigil.operators.append_event", return_value={}),
    ):
        result = CliRunner().invoke(cli, ["command", "find big files"])

    assert result.exit_code == 0, result.output
    assert result.output == "find . -size +10M\nLists large files.\n"


def test_command_verb_json_emits_proposal_envelope() -> None:
    with (
        patch("sigil.operators.ensure_server", return_value=True),
        patch(
            "sigil.operators.chat_json",
            return_value={
                "kind": "command",
                "body": "git push origin main",
                "explanation": "Publishes the branch.",
            },
        ),
        patch("sigil.operators.append_event", return_value={}),
    ):
        result = CliRunner().invoke(cli, ["command", "--json", "ship it"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "prompt": "ship it",
        "command": "git push origin main",
        "explanation": "Publishes the branch.",
    }


def test_op_cli_rejects_mixed_glyphs() -> None:
    result = invoke_op(["?^"])
    assert result.exit_code == 2
    assert "unsupported operator: ?" in result.output


def test_op_cli_rejects_transform_until_colon_operator_exists() -> None:
    result = invoke_op([":json"])
    assert result.exit_code == 2
    assert "unsupported operator: :" in result.output


def test_proposal_user_prompt_includes_recent_turns_in_interactive_mode() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            record_turn("ls -la", 0, "/repo")
            record_turn("pytest tests/test_foo.py", 1, "/repo")
            invocation = create_invocation(
                ",",
                prompt="commit message for what just happened",
                stdin="",
                mode="interactive",
            )
            prompt = proposal_user_prompt(invocation)

    assert "Recent shell activity:" in prompt
    assert "ls -la" in prompt
    assert "pytest tests/test_foo.py" in prompt
    assert "exit 0" in prompt
    assert "exit 1" in prompt


def test_proposal_user_prompt_keeps_prompt_and_attaches_active_failure() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            record_turn(
                "pytest tests/test_foo.py",
                1,
                "/repo",
                stderr_snippet="AssertionError: no",
            )
            invocation = create_invocation(
                ",",
                prompt="fix",
                stdin="",
                mode="interactive",
            )
            prompt = proposal_user_prompt(invocation)

    assert "Prompt: fix" in prompt
    assert "Suggest the smallest safe next shell command" not in prompt
    assert "Last failed command context:" in prompt
    assert "Failed command: pytest tests/test_foo.py" in prompt
    assert "AssertionError: no" in prompt


def test_proposal_user_prompt_attaches_active_failure_for_unrelated_prompt() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            record_turn(
                "pytest tests/test_foo.py",
                1,
                "/repo",
                stderr_snippet="AssertionError: no",
            )
            invocation = create_invocation(
                ",",
                prompt="summarize the repository layout",
                stdin="",
                mode="interactive",
            )
            prompt = proposal_user_prompt(invocation)

    assert "Prompt: summarize the repository layout" in prompt
    assert "Last failed command context:" in prompt
    assert "Failed command: pytest tests/test_foo.py" in prompt


def test_proposal_user_prompt_omits_failure_context_after_successful_turn() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            record_turn(
                "pytest tests/test_foo.py",
                1,
                "/repo",
                stderr_snippet="AssertionError: no",
            )
            record_turn("git status --short", 0, "/repo")
            invocation = create_invocation(
                ",",
                prompt="fix",
                stdin="",
                mode="interactive",
            )
            prompt = proposal_user_prompt(invocation)

    assert "Last failed command context:" not in prompt


def test_proposal_user_prompt_omits_recent_turns_when_none_recorded() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            invocation = create_invocation(
                ",",
                prompt="anything",
                stdin="",
                mode="interactive",
            )
            prompt = proposal_user_prompt(invocation)

    assert "Recent shell activity" not in prompt


def test_proposal_user_prompt_omits_failure_context_when_none_recorded(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            invocation = create_invocation(
                ",",
                prompt="anything",
                stdin="",
                mode="interactive",
            )
            prompt = proposal_user_prompt(invocation)

    captured = capsys.readouterr()
    assert captured.err == ""
    assert "No failed command is recorded" not in prompt
    assert "Last failed command context:" not in prompt


def test_proposal_user_prompt_omits_recent_turns_in_pipeline_mode() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            record_turn("ls -la", 0, "/repo")
            invocation = create_invocation(
                ",",
                prompt="summarize",
                stdin="some piped input\n",
                mode="pipeline",
            )
            prompt = proposal_user_prompt(invocation)

    assert "Recent shell activity" not in prompt


def test_proposal_user_prompt_includes_recent_question_transcript() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            append_jsonl(
                "last-answer.jsonl",
                {"role": "user", "content": "find the biggest files"},
            )
            append_jsonl(
                "last-answer.jsonl",
                {"role": "assistant", "content": "du -ah . | sort -rh | head -n 10"},
            )
            invocation = create_invocation(
                ",,",
                prompt="do that",
                stdin="",
                mode="interactive",
            )
            prompt = proposal_user_prompt(invocation)

    assert "Recent answer transcript:" in prompt
    assert "find the biggest files" in prompt
    assert "du -ah . | sort -rh | head -n 10" in prompt


def test_proposal_user_prompt_omits_question_transcript_when_empty() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            invocation = create_invocation(
                ",",
                prompt="anything",
                stdin="",
                mode="interactive",
            )
            prompt = proposal_user_prompt(invocation)

    assert "Recent answer transcript" not in prompt


def test_proposal_user_prompt_omits_question_transcript_in_pipeline_mode() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            append_jsonl(
                "last-answer.jsonl",
                {"role": "user", "content": "find the biggest files"},
            )
            append_jsonl(
                "last-answer.jsonl",
                {"role": "assistant", "content": "du -ah . | sort -rh | head -n 10"},
            )
            invocation = create_invocation(
                ",",
                prompt="summarize",
                stdin="some piped input\n",
                mode="pipeline",
            )
            prompt = proposal_user_prompt(invocation)

    assert "Recent answer transcript" not in prompt
