from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from _patch import patch
from sigil.cli import cli
from sigil.operators import create_invocation, parse_operator_token
from sigil.policy import ExecutionPolicy, classify_output, evaluate_policy
from sigil.state import read_json, write_json

PATCH_TEXT = """diff --git a/example.txt b/example.txt
--- a/example.txt
+++ b/example.txt
@@ -1 +1 @@
-old
+new
"""


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
        ("^^^", "^", 3),
        (",,", ",", 2),
    ],
)
def test_parse_operator_token_repetition(
    token: str,
    base: str,
    depth: int,
) -> None:
    assert parse_operator_token(token) == (base, depth)


@pytest.mark.parametrize("token", ["", "?^", "?:", "abc", ":"])
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
    assert invocation.name == "inspect"
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
        "name": "inspect",
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


def test_op_cli_runs_piped_inspect_operator() -> None:
    calls = {}

    def fake_chat_text(system: str, user: str, *, max_tokens: int = 1200) -> str:
        calls["system"] = system
        calls["user"] = user
        calls["max_tokens"] = max_tokens
        return "risk summary\n"

    with (
        patch("sigil.operators.ensure_server", return_value=True),
        patch("sigil.operators.chat_text", side_effect=fake_chat_text),
        patch("sigil.operators.append_event", return_value={}),
    ):
        result = CliRunner().invoke(
            cli,
            ["op", "??", "review", "risky", "changes"],
            input="diff --git a/file b/file\n",
        )
    assert result.exit_code == 0, result.output
    assert result.output == "risk summary\n"
    assert "Depth: 2" in str(calls["system"])
    assert "Prompt: review risky changes" in str(calls["user"])
    assert "diff --git a/file b/file" in str(calls["user"])
    assert calls["max_tokens"] == 1200


def test_op_cli_runs_piped_recommend_operator() -> None:
    calls = {}

    def fake_chat_json(
        system: str, user: str, schema: dict[str, object]
    ) -> dict[str, str]:
        calls["system"] = system
        calls["user"] = user
        calls["schema"] = schema
        return {
            "command": "uv run pytest",
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
        "uv run pytest\nTests validate the current code path before cleanup.\n"
    )
    assert "Recommend one concrete next action" in str(calls["system"])
    assert "Prompt: draft an executive summary" in str(calls["user"])
    assert "command" in str(calls["schema"])
    assert "explanation" in str(calls["schema"])


def test_op_cli_runs_piped_repair_preview_with_file_context() -> None:
    calls = {}

    def fake_chat_text(system: str, user: str, *, max_tokens: int = 1200) -> str:
        calls["system"] = system
        calls["user"] = user
        calls["max_tokens"] = max_tokens
        return "--- a/example.py\n+++ b/example.py\n@@\n-old\n+new"

    with tempfile.TemporaryDirectory() as tmp_dir:
        old_cwd = os.getcwd()
        os.chdir(tmp_dir)
        try:
            Path("example.py").write_text("old\n", encoding="utf-8")
            with (
                patch("sigil.operators.ensure_server", return_value=True),
                patch("sigil.operators.chat_text", side_effect=fake_chat_text),
                patch("sigil.operators.append_event", return_value={}),
            ):
                result = CliRunner().invoke(
                    cli,
                    ["op", "^^", "rename", "old", "to", "new"],
                    input="example.py\n",
                )
        finally:
            os.chdir(old_cwd)

    assert result.exit_code == 0, result.output
    assert result.output.startswith("--- a/example.py\n+++ b/example.py\n")
    assert "repair operator" in str(calls["system"])
    assert "Prompt: rename old to new" in str(calls["user"])
    assert "stdin targets:\nexample.py" in str(calls["user"])
    assert "--- example.py\nold" in str(calls["user"])
    assert calls["max_tokens"] == 1200


def test_repair_operator_stores_unified_diff_patch_preview() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        old_state_dir = os.environ.get("SIGIL_STATE_DIR")
        old_session_id = os.environ.get("SIGIL_SESSION_ID")
        old_cwd = os.getcwd()
        state_root = Path(tmp_dir) / "state"
        os.environ["SIGIL_STATE_DIR"] = str(state_root)
        os.environ["SIGIL_SESSION_ID"] = "test"
        os.chdir(tmp_dir)
        try:
            Path("example.txt").write_text("old\n", encoding="utf-8")
            with (
                patch("sigil.operators.ensure_server", return_value=True),
                patch("sigil.operators.chat_text", return_value=PATCH_TEXT),
            ):
                result = CliRunner().invoke(
                    cli,
                    ["op", "^^", "update", "example"],
                    input="example.txt\n",
                )
            stored = read_json("last-patch.json")
            events = read_global_events(state_root)
        finally:
            os.chdir(old_cwd)
            if old_state_dir is None:
                os.environ.pop("SIGIL_STATE_DIR", None)
            else:
                os.environ["SIGIL_STATE_DIR"] = old_state_dir
            if old_session_id is None:
                os.environ.pop("SIGIL_SESSION_ID", None)
            else:
                os.environ["SIGIL_SESSION_ID"] = old_session_id

    assert result.exit_code == 0, result.output
    assert result.stdout == PATCH_TEXT
    assert isinstance(stored, dict)
    assert stored["patch"] == PATCH_TEXT.rstrip()
    assert stored["operator"]["glyph"] == "^^"
    assert stored["taint"] == ["model"]
    assert [event["type"] for event in events] == [
        "operator_completed",
        "patch_preview_stored",
    ]


def test_patch_apply_requires_yes_and_records_application() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        work = tmp / "work"
        work.mkdir()
        old_state_dir = os.environ.get("SIGIL_STATE_DIR")
        old_session_id = os.environ.get("SIGIL_SESSION_ID")
        state_root = tmp / "state"
        os.environ["SIGIL_STATE_DIR"] = str(state_root)
        os.environ["SIGIL_SESSION_ID"] = "test"
        try:
            target = work / "example.txt"
            target.write_text("old\n", encoding="utf-8")
            write_json(
                "last-patch.json",
                {
                    "patch": PATCH_TEXT,
                    "cwd": str(work),
                    "event_id": "patch-event",
                    "glyph": "^^",
                    "integrity": "local_model",
                    "capability": "propose",
                    "taint": ["model"],
                },
            )

            blocked = CliRunner().invoke(cli, ["patch", "apply"])
            checked = CliRunner().invoke(cli, ["patch", "check"])
            applied = CliRunner().invoke(cli, ["patch", "apply", "--yes"])
            applied_text = target.read_text(encoding="utf-8")
            events = read_global_events(state_root)
        finally:
            if old_state_dir is None:
                os.environ.pop("SIGIL_STATE_DIR", None)
            else:
                os.environ["SIGIL_STATE_DIR"] = old_state_dir
            if old_session_id is None:
                os.environ.pop("SIGIL_SESSION_ID", None)
            else:
                os.environ["SIGIL_SESSION_ID"] = old_session_id

    assert blocked.exit_code == 2
    assert "pass --yes" in blocked.stderr
    assert checked.exit_code == 0, checked.output
    assert checked.stdout == "patch applies cleanly\n"
    assert applied.exit_code == 0, applied.output
    assert applied.stdout == "patch applied\n"
    assert applied_text == "new\n"
    assert [event["type"] for event in events] == ["patch_checked", "patch_applied"]
    assert "patch" not in events[0]
    assert events[-1]["capability"] == "write_boxed"


def test_policy_classifies_destructive_shell_output() -> None:
    classification = classify_output("sudo rm -rf build\ncurl https://example.com\n")

    assert "execute" in classification.classes
    assert "delete" in classification.classes
    assert "network" in classification.classes
    assert "privileged" in classification.classes


def test_policy_classifies_unified_diff_as_file_write() -> None:
    classification = classify_output("--- a/app.py\n+++ b/app.py\n@@\n-old\n+new\n")

    assert "file_write" in classification.classes
    assert "execute" not in classification.classes


def test_double_comma_policy_allows_execution_classification() -> None:
    decision = evaluate_policy(
        glyph=",,",
        depth=2,
        output="rm -rf build",
        policy=ExecutionPolicy(),
    )

    assert decision.status == "allowed"
    assert "executes" in decision.message
    assert "delete" in decision.classification.classes


def test_deeper_comma_policy_matches_runtime_execution() -> None:
    decision = evaluate_policy(
        glyph=",,,,",
        depth=4,
        output="git status --short",
        policy=ExecutionPolicy(),
    )

    assert decision.status == "allowed"
    assert "executes" in decision.message


def test_dry_run_policy_previews_without_execution() -> None:
    decision = evaluate_policy(
        glyph=",,",
        depth=2,
        output="git status --short",
        policy=ExecutionPolicy(dry_run=True),
    )

    assert decision.status == "preview"
    assert "dry-run" in decision.message


def test_op_cli_executes_double_comma_command() -> None:
    calls = {}

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls["args"] = args
        calls["kwargs"] = kwargs
        return subprocess.CompletedProcess(args, 0, stdout="done\n", stderr="")

    events = []

    def fake_append_event(event: dict[str, object]) -> dict[str, object]:
        events.append(event)
        return {"id": str(len(events)), **event}

    with (
        patch("sigil.operators.ensure_server", return_value=True),
        patch(
            "sigil.operators.chat_json", return_value={"command": "printf 'done\\n'"}
        ),
        patch("sigil.operators.subprocess.run", side_effect=fake_run),
        patch("sigil.operators.append_event", side_effect=fake_append_event),
    ):
        result = CliRunner().invoke(cli, ["op", ",,", "say", "done"])

    assert result.exit_code == 0
    assert result.stdout == "done\n"
    assert result.stderr == ""
    assert calls["args"][-2:] == ["-lc", "printf 'done\\n'"]
    assert [event["type"] for event in events] == [
        "operator_completed",
        "operator_command_executed",
    ]
    assert events[-1]["capability"] == "exec_boxed"


def test_op_cli_returns_executed_command_status_and_stderr() -> None:
    with (
        patch("sigil.operators.ensure_server", return_value=True),
        patch("sigil.operators.chat_json", return_value={"command": "false"}),
        patch(
            "sigil.operators.subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["zsh", "-lc", "false"], 7, stdout="", stderr="nope\n"
            ),
        ),
        patch("sigil.operators.append_event", return_value={"id": "operator-event"}),
    ):
        result = CliRunner().invoke(cli, ["op", ",,", "fail"])

    assert result.exit_code == 7
    assert result.stdout == ""
    assert result.stderr == "nope\n"


def test_op_cli_dry_run_double_comma_does_not_execute() -> None:
    with (
        patch("sigil.operators.ensure_server", return_value=True),
        patch(
            "sigil.operators.chat_json",
            return_value={"command": "git status --short"},
        ),
        patch("sigil.operators.subprocess.run", side_effect=AssertionError("no exec")),
        patch("sigil.operators.append_event", return_value={}),
    ):
        result = CliRunner().invoke(cli, ["op", "--dry-run", ",,", "status"])

    assert result.exit_code == 0
    assert result.stdout == "git status --short\n"
    assert "dry-run" in result.stderr


def test_op_cli_denies_piped_comma_before_model_call() -> None:
    with (
        patch("sigil.cli.confirm_piped_input", return_value=False),
        patch("sigil.operators.chat_json", side_effect=AssertionError("no model")),
    ):
        result = CliRunner().invoke(cli, ["op", ",", "summarize"], input="notes\n")

    assert result.exit_code == 2
    assert "piped input declined" in result.stderr


def test_op_cli_confirms_piped_comma_before_model_call() -> None:
    with (
        patch("sigil.cli.confirm_piped_input", return_value=True),
        patch(
            "sigil.operators.chat_json",
            return_value={"command": "cat notes", "explanation": "uses stdin"},
        ),
        patch("sigil.operators.append_event", return_value={}),
    ):
        result = CliRunner().invoke(cli, ["op", ",", "summarize"], input="notes\n")

    assert result.exit_code == 0
    assert result.stdout == "cat notes\nuses stdin\n"


def test_op_cli_confirms_piped_double_comma_command_before_execution() -> None:
    with (
        patch("sigil.cli.confirm_piped_input", return_value=True),
        patch("sigil.operators.confirm_execution", return_value=False),
        patch("sigil.operators.chat_json", return_value={"command": "cat notes"}),
        patch("sigil.operators.subprocess.run", side_effect=AssertionError("no exec")),
        patch("sigil.operators.append_event", return_value={"id": "operator-event"}),
    ):
        result = CliRunner().invoke(cli, ["op", ",,", "summarize"], input="notes\n")

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "command execution declined" in result.stderr


def test_op_cli_accepts_piped_double_comma_execution() -> None:
    with (
        patch("sigil.operators.ensure_server", return_value=True),
        patch("sigil.cli.confirm_piped_input", return_value=True),
        patch("sigil.operators.confirm_execution", return_value=True),
        patch("sigil.operators.chat_json", return_value={"command": "cat notes"}),
        patch(
            "sigil.operators.subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["zsh", "-lc", "cat notes"], 0, stdout="done\n", stderr=""
            ),
        ),
        patch("sigil.operators.append_event", return_value={"id": "operator-event"}),
    ):
        result = CliRunner().invoke(cli, ["op", ",,", "summarize"], input="notes\n")

    assert result.exit_code == 0
    assert result.stdout == "done\n"


def test_verb_commands_run_piped_stream_operators() -> None:
    calls = []
    json_calls = []

    def fake_chat_text(system: str, user: str, *, max_tokens: int = 1200) -> str:
        calls.append((system, user, max_tokens))
        return "stream result"

    def fake_chat_json(
        system: str, user: str, schema: dict[str, object]
    ) -> dict[str, str]:
        json_calls.append((system, user, schema))
        return {"command": "stream result", "explanation": "because stdin"}

    with (
        patch("sigil.operators.ensure_server", return_value=True),
        patch("sigil.operators.chat_text", side_effect=fake_chat_text),
        patch("sigil.operators.chat_json", side_effect=fake_chat_json),
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
        fix_result = CliRunner().invoke(
            cli,
            ["fix", "rename", "old", "to", "new"],
            input="example.py\n",
        )

    assert ask_result.exit_code == 0, ask_result.output
    assert command_result.exit_code == 0, command_result.output
    assert fix_result.exit_code == 0, fix_result.output
    assert ask_result.output == "stream result\n"
    assert command_result.output == "stream result\nbecause stdin\n"
    assert fix_result.output == "stream result\n"
    assert "Operator: ? (inspect)" in calls[0][1]
    assert "Operator: , (recommend)" in json_calls[0][1]
    assert "Operator: ^ (repair)" in calls[1][1]


def test_op_cli_rejects_mixed_glyphs() -> None:
    result = CliRunner().invoke(cli, ["op", "?^"])
    assert result.exit_code == 2
    assert "operator token must repeat one glyph" in result.output


def test_op_cli_rejects_transform_until_colon_operator_exists() -> None:
    result = CliRunner().invoke(cli, ["op", ":json"])
    assert result.exit_code == 2
    assert "unsupported operator: :" in result.output
