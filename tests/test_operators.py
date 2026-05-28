from __future__ import annotations

import json
import os
import tempfile
from io import StringIO
from pathlib import Path

import pytest
from click.testing import CliRunner

from _patch import patch, patch_dict
from sigil.cli import cli
from sigil.operators import (
    create_invocation,
    parse_operator_token,
    proposal_user_prompt,
)
from sigil.policy import ExecutionPolicy, classify_output, evaluate_policy
from sigil.session import record_turn
from sigil.state import append_jsonl, read_jsonl


def read_global_events(root: Path) -> list[dict[str, object]]:
    path = root / "events.jsonl"
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.mark.parametrize(
    ("token", "base", "depth"),
    [
        ("?", "?", 1),
        ("??", "?", 2),
        (",", ",", 1),
        (",,", ",", 2),
        (",,,", ",", 3),
        ("@", "@", 1),
        ("@@", "@", 2),
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
        "??",
        prompt="review risky changes",
        stdin="diff",
        mode="pipeline",
    )
    assert invocation.base == "?"
    assert invocation.depth == 2
    assert invocation.name == "answer"
    assert invocation.prompt == "review risky changes"
    assert invocation.stdin == "diff"
    assert invocation.mode == "pipeline"


def test_op_cli_json_reports_parsed_invocation() -> None:
    result = CliRunner().invoke(
        cli,
        ["op", "--json", "??", "review", "risky", "changes"],
        input="diff --git a/file b/file\n",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {
        "glyph": "??",
        "base": "?",
        "depth": 2,
        "name": "answer",
        "prompt": "review risky changes",
        "stdin": "diff --git a/file b/file\n",
        "mode": "pipeline",
    }


def test_op_cli_json_does_not_run_operator() -> None:
    with patch("sigil.operators.chat_text", side_effect=AssertionError("no model")):
        result = CliRunner().invoke(
            cli,
            ["op", "--json", "??", "review"],
            input="diff\n",
        )
    assert result.exit_code == 0, result.output


def test_op_cli_runs_piped_double_question_operator_through_web_route() -> None:
    calls = []

    def fake_ask(*args: object, **kwargs: object) -> int:
        calls.append((args, kwargs))
        return 0

    with (
        patch("sigil.cli.confirm_piped_input", side_effect=AssertionError("no prompt")),
        patch("sigil.cli.ask", side_effect=fake_ask),
    ):
        result = CliRunner().invoke(
            cli,
            ["op", "??", "review", "risky", "changes"],
            input="diff --git a/file b/file\n",
        )
    assert result.exit_code == 0, result.output
    assert calls == [
        (
            ("review risky changes\n\nPiped input:\ndiff --git a/file b/file\n",),
            {"glyph": "??", "tools": "read,web_search", "use_web": True},
        )
    ]


def test_question_operators_use_source_specific_routes() -> None:
    calls = []

    def fake_ask(*args: object, **kwargs: object) -> int:
        calls.append((args, kwargs))
        return 0

    with patch("sigil.cli.ask", side_effect=fake_ask):
        first = CliRunner().invoke(cli, ["op", "?", "first", "question"])
        second = CliRunner().invoke(cli, ["op", "??", "second", "question"])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert calls == [
        (("first question",), {"glyph": "?", "tools": "read", "use_web": False}),
        (
            ("second question",),
            {"glyph": "??", "tools": "read,web_search", "use_web": True},
        ),
    ]


def test_triple_question_is_rejected() -> None:
    with patch("sigil.cli.ask", side_effect=AssertionError("no ask")):
        result = CliRunner().invoke(cli, ["op", "???", "explain", "this"])

    assert result.exit_code == 2
    assert "? operator depth must be 1 or 2" in result.output


def test_op_cli_runs_piped_recommend_operator() -> None:
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
        patch("sigil.cli.confirm_piped_input", return_value=True),
        patch("sigil.operators.append_event", return_value={}),
    ):
        result = CliRunner().invoke(
            cli,
            ["op", ",", "draft", "an", "executive", "summary"],
            input="meeting notes\n",
        )
    assert result.exit_code == 0, result.output
    assert result.output == (
        "uv run pytest\n"
        "local · read-only\n"
        "Tests validate the current code path before cleanup.\n"
    )
    assert "Produce one typed proposal" in str(calls["system"])
    assert "Prompt: draft an executive summary" in str(calls["user"])
    schema = calls["schema"]
    assert schema["properties"]["kind"]["enum"] == ["command"]
    assert "body" in schema["properties"]
    assert "explanation" in schema["properties"]


def test_op_cli_rejects_non_command_proposals() -> None:
    with (
        patch("sigil.operators.ensure_server", return_value=True),
        patch(
            "sigil.operators.chat_json",
            return_value={"kind": "file_update", "body": "change example.py"},
        ),
        patch("sigil.operators.append_event", side_effect=AssertionError("no event")),
    ):
        result = CliRunner().invoke(cli, ["op", ",", "update", "example"])

    assert result.exit_code == 1
    assert "did not produce a proposal" in result.stderr


def test_double_comma_runs_confirmed_agent_step() -> None:
    calls = []

    def fake_run_act_stepper(*args: object, **kwargs: object) -> int:
        calls.append((args, kwargs))
        return 0

    with patch("sigil.cli.run_act_stepper", side_effect=fake_run_act_stepper):
        result = CliRunner().invoke(cli, ["op", ",,", "update", "it"])

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            (),
            {
                "objective": "update it",
                "stdin_text": "",
                "confirm_step": True,
                "glyph": ",,",
                "verbose": False,
            },
        )
    ]


def test_policy_classifies_destructive_shell_output() -> None:
    classification = classify_output("sudo rm -rf build\ncurl https://example.com\n")

    assert "execute" in classification.classes
    assert "delete" in classification.classes
    assert "network" in classification.classes
    assert "privileged" in classification.classes
    assert classification.labels == (
        "local",
        "network",
        "read-only",
        "delete",
        "privileged",
        "high-risk",
    )


@pytest.mark.parametrize(
    ("command", "labels"),
    [
        ("uv run pytest tests/test_status.py", ("local", "read-only", "focused")),
        ("touch fixed.txt", ("local", "write")),
        ("curl https://example.com", ("network", "read-only")),
        ("git push origin main", ("network", "publish", "high-risk")),
        ("rm -rf build", ("local", "delete", "high-risk")),
        ("sudo chmod 600 secret.txt", ("local", "privileged", "high-risk")),
    ],
)
def test_policy_maps_commands_to_trust_labels(
    command: str,
    labels: tuple[str, ...],
) -> None:
    assert classify_output(command).labels == labels


def test_double_comma_policy_defers_to_act_runner() -> None:
    decision = evaluate_policy(
        glyph=",,",
        depth=2,
        output="rm -rf build",
        policy=ExecutionPolicy(),
    )

    assert decision.status == "preview"
    assert "act runner" in decision.message
    assert "delete" in decision.classification.classes


def test_triple_comma_policy_defers_to_act_runner() -> None:
    decision = evaluate_policy(
        glyph=",,,",
        depth=3,
        output="git status --short",
        policy=ExecutionPolicy(),
    )

    assert decision.status == "preview"
    assert "act runner" in decision.message


def test_dry_run_policy_previews_without_execution() -> None:
    decision = evaluate_policy(
        glyph=",,",
        depth=2,
        output="git status --short",
        policy=ExecutionPolicy(dry_run=True),
    )

    assert decision.status == "preview"
    assert "dry-run" in decision.message


def test_op_cli_routes_double_comma_to_agent_stepper() -> None:
    calls = []

    def fake_run_act_stepper(*args: object, **kwargs: object) -> int:
        calls.append((args, kwargs))
        return 0

    with patch("sigil.cli.run_act_stepper", side_effect=fake_run_act_stepper):
        result = CliRunner().invoke(cli, ["op", ",,", "say", "done"])

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

    with patch("sigil.cli.run_act_stepper", side_effect=fake_run_act_stepper):
        result = CliRunner().invoke(cli, ["op", ",,,", "publish"])

    assert result.exit_code == 0
    assert result.stdout == ""
    assert calls[0][1]["objective"] == "publish"
    assert calls[0][1]["confirm_step"] is False
    assert calls[0][1]["glyph"] == ",,,"


def test_op_cli_returns_agent_stepper_status() -> None:
    with patch("sigil.cli.run_act_stepper", return_value=7):
        result = CliRunner().invoke(cli, ["op", ",,", "fail"])

    assert result.exit_code == 7
    assert result.stdout == ""
    assert result.stderr == ""


def test_op_cli_dry_run_double_comma_does_not_execute() -> None:
    calls = []

    def fake_run_act_stepper(*args: object, **kwargs: object) -> int:
        calls.append((args, kwargs))
        return 0

    with patch("sigil.cli.run_act_stepper", side_effect=fake_run_act_stepper):
        result = CliRunner().invoke(cli, ["op", "--dry-run", ",,", "status"])

    assert result.exit_code == 0
    assert result.stdout == ""
    assert calls[0][1]["dry_run"] is True
    assert calls[0][1]["confirm_step"] is True
    assert calls[0][1]["glyph"] == ",,"


def test_op_cli_dry_run_question_does_not_call_web_route() -> None:
    with patch("sigil.cli.ask", side_effect=AssertionError("no web")):
        result = CliRunner().invoke(cli, ["op", "--dry-run", "?", "status"])

    assert result.exit_code == 0
    assert "read question route" in result.output


def test_op_cli_rejects_caret_before_model_or_confirmation() -> None:
    with (
        patch("sigil.cli.confirm_piped_input", side_effect=AssertionError("no prompt")),
        patch("sigil.operators.chat_json", side_effect=AssertionError("no model")),
    ):
        result = CliRunner().invoke(cli, ["op", "^", "status"], input="notes\n")

    assert result.exit_code == 2
    assert "unsupported operator: ^" in result.output


def test_triple_comma_creates_act_and_executes_one_auto_approved_step() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        old_state_dir = os.environ.get("SIGIL_STATE_DIR")
        old_session_id = os.environ.get("SIGIL_SESSION_ID")
        os.environ["SIGIL_STATE_DIR"] = tmp_dir
        os.environ["SIGIL_SESSION_ID"] = "act-session"
        events = []
        pi_calls = []

        def fake_append_event(event: dict[str, object]) -> dict[str, object]:
            stored = {"id": f"event-{len(events)}", **event}
            events.append(stored)
            return stored

        def fake_run_pi(*args: object, **kwargs: object) -> int:
            pi_calls.append((args, kwargs))
            return 0

        try:
            with (
                patch(
                    "sigil.acts.prompt_on_tty",
                    side_effect=AssertionError("no prompt"),
                ),
                patch("sigil.acts.run_pi_agent_step", side_effect=fake_run_pi),
                patch("sigil.acts.append_event", side_effect=fake_append_event),
            ):
                result = CliRunner().invoke(cli, ["op", ",,,", "ship", "it"])
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
    assert "sigil act (active):" in result.output
    assert "pi --tools read,grep,find,ls,bash,edit,write" in result.output
    assert len(pi_calls) == 1
    assert pi_calls[0][1]["glyph"] == ",,,"
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


def test_piped_triple_comma_denies_input_before_act_generation() -> None:
    with (
        patch("sigil.cli.confirm_piped_input", return_value=False),
        patch("sigil.acts.run_pi_agent_step", side_effect=AssertionError("no pi")),
    ):
        result = CliRunner().invoke(cli, ["op", ",,,", "ship"], input="notes\n")

    assert result.exit_code == 2
    assert "piped input declined" in result.stderr


def test_act_pi_step_uses_bash_handoff_extension() -> None:
    class FakeProc:
        def __init__(self, stdout: object | None = None) -> None:
            self.stdout = stdout

        def wait(self) -> int:
            return 0

    with tempfile.TemporaryDirectory() as tmp_dir:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp_dir, "SIGIL_SESSION_ID": "act-session"},
        ):
            popen_calls: list[tuple[list[str], dict[str, object]]] = []

            def fake_popen(cmd: list[str], *args: object, **kwargs: object) -> FakeProc:
                popen_calls.append((cmd, kwargs))
                if cmd[0] == "pi":
                    return FakeProc(stdout=StringIO(""))
                return FakeProc(stdout=StringIO(""))

            with (
                patch("sigil.acts.ensure_model_for_pi", return_value=True),
                patch("sigil.acts.subprocess.Popen", side_effect=fake_popen),
                patch("sigil.acts.renderer_command", return_value=["cat"]),
                patch("sigil.acts.record_bash_handoffs", return_value=[]),
            ):
                from sigil.acts import run_pi_agent_step

                result = run_pi_agent_step(
                    {"objective": "repair"},
                    {"id": "1"},
                    {
                        "id": "decision",
                        "integrity": "human",
                        "capability": "none",
                        "taint": [],
                    },
                )

    assert result == 0
    pi_cmd, _ = next(call for call in popen_calls if call[0][0] == "pi")
    assert pi_cmd[pi_cmd.index("--tools") + 1] == "read,grep,find,ls,bash,edit,write"
    assert "--extension" in pi_cmd
    filter_cmd, filter_kwargs = next(call for call in popen_calls if call[0][0] != "pi")
    assert "--compact" in filter_cmd
    filter_env = filter_kwargs["env"]
    assert isinstance(filter_env, dict)
    assert "SIGIL_BASH_HANDOFF_PATH" in filter_env


def test_act_pi_step_verbose_uses_raw_stream_renderer() -> None:
    class FakeProc:
        def __init__(self, stdout: object | None = None) -> None:
            self.stdout = stdout

        def wait(self) -> int:
            return 0

    with tempfile.TemporaryDirectory() as tmp_dir:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp_dir, "SIGIL_SESSION_ID": "act-session"},
        ):
            popen_calls: list[tuple[list[str], dict[str, object]]] = []

            def fake_popen(cmd: list[str], *args: object, **kwargs: object) -> FakeProc:
                del args
                popen_calls.append((cmd, kwargs))
                return FakeProc(stdout=StringIO(""))

            with (
                patch("sigil.acts.ensure_model_for_pi", return_value=True),
                patch("sigil.acts.subprocess.Popen", side_effect=fake_popen),
                patch("sigil.acts.renderer_command", return_value=["cat"]),
                patch("sigil.acts.record_bash_handoffs", return_value=[]),
            ):
                from sigil.acts import run_pi_agent_step

                result = run_pi_agent_step(
                    {"objective": "repair"},
                    {"id": "1"},
                    {
                        "id": "decision",
                        "integrity": "human",
                        "capability": "none",
                        "taint": [],
                    },
                    verbose=True,
                )

    assert result == 0
    filter_cmd, _ = next(call for call in popen_calls if call[0][0] != "pi")
    assert "--compact" not in filter_cmd


def test_act_resume_executes_pending_step_without_regenerating() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        old_state_dir = os.environ.get("SIGIL_STATE_DIR")
        old_session_id = os.environ.get("SIGIL_SESSION_ID")
        os.environ["SIGIL_STATE_DIR"] = tmp_dir
        os.environ["SIGIL_SESSION_ID"] = "act-session"
        pi_calls = []

        def fake_run_pi(*args: object, **kwargs: object) -> int:
            pi_calls.append((args, kwargs))
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
                                "title": "Run one Pi edit step",
                                "command": "pi --tools read,grep,find,ls,bash,edit,write",
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
                patch("sigil.acts.run_pi_agent_step", side_effect=fake_run_pi),
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
    assert "Run the pending edit." in result.output
    assert len(pi_calls) == 1
    assert act_events[-1]["act"]["status"] == "completed"


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
    assert "[pending] Inspect" in shown.output
    assert aborted.exit_code == 0, aborted.output
    assert json.loads(aborted.output)["aborted"]
    assert act_events[-1]["act"]["status"] == "aborted"


def test_op_cli_denies_piped_comma_before_model_call() -> None:
    with (
        patch("sigil.cli.confirm_piped_input", return_value=False),
        patch("sigil.operators.chat_json", side_effect=AssertionError("no model")),
    ):
        result = CliRunner().invoke(cli, ["op", ",", "summarize"], input="notes\n")

    assert result.exit_code == 2
    assert "piped input declined" in result.stderr


def test_op_cli_sends_piped_question_without_confirmation() -> None:
    calls = []

    def fake_ask(*args: object, **kwargs: object) -> int:
        calls.append((args, kwargs))
        return 0

    with (
        patch("sigil.cli.confirm_piped_input", side_effect=AssertionError("no prompt")),
        patch("sigil.cli.ask", side_effect=fake_ask),
    ):
        result = CliRunner().invoke(cli, ["op", "?", "review"], input="diff\n")

    assert result.exit_code == 0
    assert calls == [
        (
            ("review\n\nPiped input:\ndiff\n",),
            {"glyph": "?", "tools": "read", "use_web": False},
        ),
    ]


def test_ask_follow_up_sends_piped_input_without_confirmation() -> None:
    calls = []

    def fake_ask(*args: object, **kwargs: object) -> int:
        calls.append((args, kwargs))
        return 0

    with (
        patch("sigil.cli.confirm_piped_input", side_effect=AssertionError("no prompt")),
        patch("sigil.cli.ask", side_effect=fake_ask),
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
                "glyph": "??",
                "tools": "read,web_search",
                "use_web": True,
                "append_transcript": True,
                "json_output": False,
            },
        )
    ]


def test_ask_follow_up_sends_confirmed_piped_input_to_web_route() -> None:
    calls = []

    def fake_ask(*args: object, **kwargs: object) -> int:
        calls.append((args, kwargs))
        return 0

    with (
        patch("sigil.cli.confirm_piped_input", side_effect=AssertionError("no prompt")),
        patch("sigil.cli.ask", side_effect=fake_ask),
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
                "glyph": "??",
                "tools": "read,web_search",
                "use_web": True,
                "append_transcript": True,
                "json_output": False,
            },
        )
    ]


def test_op_cli_confirms_piped_comma_before_model_call() -> None:
    with (
        patch("sigil.cli.confirm_piped_input", return_value=True),
        patch("sigil.operators.ensure_server", return_value=True),
        patch(
            "sigil.operators.chat_json",
            return_value={
                "kind": "command",
                "body": "cat notes",
                "explanation": "uses stdin",
            },
        ),
        patch("sigil.operators.append_event", return_value={}),
    ):
        result = CliRunner().invoke(cli, ["op", ",", "summarize"], input="notes\n")

    assert result.exit_code == 0
    assert result.stdout == "cat notes\nlocal · read-only\nuses stdin\n"


def test_op_cli_confirms_piped_double_comma_before_agent_step() -> None:
    calls = []

    def fake_run_act_stepper(*args: object, **kwargs: object) -> int:
        calls.append((args, kwargs))
        return 0

    with (
        patch("sigil.cli.confirm_piped_input", return_value=True),
        patch("sigil.cli.run_act_stepper", side_effect=fake_run_act_stepper),
    ):
        result = CliRunner().invoke(cli, ["op", ",,", "summarize"], input="notes\n")

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
        patch("sigil.cli.confirm_piped_input", return_value=True),
        patch("sigil.cli.run_act_stepper", side_effect=fake_run_act_stepper),
    ):
        result = CliRunner().invoke(cli, ["op", ",,,", "summarize"], input="notes\n")

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
        patch("sigil.cli.ask", side_effect=fake_ask),
        patch("sigil.cli.confirm_piped_input", return_value=True),
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
    assert command_result.output == "stream result\nlocal · read-only\nbecause stdin\n"
    assert ask_calls == [
        (
            ("review\n\nPiped input:\ndiff\n",),
            {"glyph": "?", "tools": "read", "use_web": False},
        )
    ]
    assert "Operator: , (propose)" in json_calls[0][1]


def test_op_cli_rejects_mixed_glyphs() -> None:
    result = CliRunner().invoke(cli, ["op", "?^"])
    assert result.exit_code == 2
    assert "operator token must repeat one glyph" in result.output


def test_op_cli_rejects_transform_until_colon_operator_exists() -> None:
    result = CliRunner().invoke(cli, ["op", ":json"])
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


def test_proposal_user_prompt_fix_targets_last_failure() -> None:
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

    assert "Prompt: Suggest the smallest safe next shell command" in prompt
    assert "Last failed command context:" in prompt
    assert "Failed command: pytest tests/test_foo.py" in prompt
    assert "AssertionError: no" in prompt


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


def test_proposal_user_prompt_reads_missing_failure_context_quietly(
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
    assert "No failed command is recorded for interactive proposal." in prompt


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
                "last-question.jsonl",
                {"role": "user", "content": "find the biggest files"},
            )
            append_jsonl(
                "last-question.jsonl",
                {"role": "assistant", "content": "du -ah . | sort -rh | head -n 10"},
            )
            invocation = create_invocation(
                ",,",
                prompt="do that",
                stdin="",
                mode="interactive",
            )
            prompt = proposal_user_prompt(invocation)

    assert "Recent question transcript:" in prompt
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

    assert "Recent question transcript" not in prompt


def test_proposal_user_prompt_omits_question_transcript_in_pipeline_mode() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            append_jsonl(
                "last-question.jsonl",
                {"role": "user", "content": "find the biggest files"},
            )
            append_jsonl(
                "last-question.jsonl",
                {"role": "assistant", "content": "du -ah . | sort -rh | head -n 10"},
            )
            invocation = create_invocation(
                ",",
                prompt="summarize",
                stdin="some piped input\n",
                mode="pipeline",
            )
            prompt = proposal_user_prompt(invocation)

    assert "Recent question transcript" not in prompt
