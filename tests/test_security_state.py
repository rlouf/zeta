from __future__ import annotations
import pytest
import json
import os
import subprocess
import tempfile
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

from click.testing import CliRunner

from _patch import patch, patch_dict
from sigil.cli import cli, main
from sigil.commands import previous, select
from sigil.failure import generate_fixes, previous_fix, record_failure, select_fix
from sigil.pi_stream import should_color, stream_events
from sigil.question import ask, renderer_command
from sigil.security import (
    SecurityViolation,
    inherit_security,
    make_security,
    normalize_security,
    reject_promotion,
)
from sigil.state import append_event, append_jsonl, read_jsonl, write_json, write_jsonl


class TtyStringIO(StringIO):
    def isatty(self) -> bool:
        return True


def test_legacy_record_is_low_trust() -> None:
    record = normalize_security({"type": "old"})
    assert record["integrity"] == "unknown"
    assert record["taint"] == ["legacy"]
    assert record["capability"] == "none"


def test_continuation_descends_to_lowest_integrity_and_keeps_inputs() -> None:
    inherited = inherit_security(
        glyph="??",
        input_records=[
            {
                "event_id": "question-1",
                "integrity": "web",
                "capability": "read",
                "taint": ["web"],
            },
            {"event_id": "legacy-1"},
        ],
        capability="read",
    )
    assert inherited["integrity"] == "unknown"
    assert inherited["inputs"] == ["question-1", "legacy-1"]
    assert inherited["taint"] == ["legacy", "web"]


def test_integrity_promotion_requires_fresh_human_input() -> None:
    with pytest.raises(SecurityViolation):
        make_security(
            glyph="??",
            integrity="local_model",
            capability="propose",
            taint=["model"],
            input_records=[{"integrity": "web", "taint": ["web"]}],
        )
    security = make_security(
        glyph=",",
        integrity="local_model",
        capability="propose",
        taint=["model"],
        fresh_human=True,
    )
    assert security["integrity"] == "local_model"


def test_reject_promotion_mutation_without_fresh_human() -> None:
    with pytest.raises(SecurityViolation):
        reject_promotion({"integrity": "web"}, {"integrity": "local_model"})


def test_top_level_help_leads_with_examples_and_support_path() -> None:
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert 'sigil command --select "find large files"' in result.output
    assert "sigil command --previous --select" in result.output
    assert 'sigil question --json "what changed in this repo?"' in result.output
    assert 'sigil question --follow-up "summarize that as a command"' in result.output
    assert "sigil install zsh" in result.output
    assert "sigil doctor" in result.output
    assert "sigil summary" in result.output
    assert "sigil events lineage" in result.output
    assert "sigil session show --json" in result.output
    assert "https://github.com/rlouf/sigil" in result.output


def test_main_rewrites_missing_executable_errors() -> None:
    stderr = StringIO()
    missing = FileNotFoundError(2, "No such file or directory", "pi")
    with patch("sigil.cli.cli.main", side_effect=missing):
        with redirect_stderr(stderr):
            assert main(["question", "hello"]) == 127
    assert "missing executable: pi" in stderr.getvalue()


def test_main_rewrites_permission_errors() -> None:
    stderr = StringIO()
    denied = PermissionError(1, "Operation not permitted", "/nope/events.jsonl")
    with patch("sigil.cli.cli.main", side_effect=denied):
        with redirect_stderr(stderr):
            assert main(["question", "hello"]) == 1
    assert "permission denied: /nope/events.jsonl" in stderr.getvalue()


def test_command_json_invokes_fresh_command_route() -> None:
    with patch(
        "sigil.cli.generate",
        return_value=[{"command": "git status --short", "note": "show status"}],
    ):
        result = CliRunner().invoke(cli, ["command", "--json", "status"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["prompt"] == "status"
    assert payload["commands"][0]["command"] == "git status --short"


def test_command_previous_json_invokes_previous_route() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        old_state_dir = os.environ.get("SIGIL_STATE_DIR")
        old_session_id = os.environ.get("SIGIL_SESSION_ID")
        os.environ["SIGIL_STATE_DIR"] = tmp
        os.environ["SIGIL_SESSION_ID"] = "test"
        try:
            write_json(
                "last-command.json",
                {
                    "id": "command-event",
                    "prompt": "status",
                    "commands": [
                        {"command": "git status --short", "note": "show changes"}
                    ],
                    "glyph": ",",
                    "integrity": "local_model",
                    "capability": "propose",
                    "taint": ["model"],
                },
            )
            result = CliRunner().invoke(cli, ["command", "--previous", "--json"])
            assert result.exit_code == 0, result.output
            payload = json.loads(result.output.splitlines()[0])
            assert payload["prompt"] == "status"
            assert payload["commands"][0]["command"] == "git status --short"
            assert payload["glyph"] == ",,"
            assert payload["taint"] == ["model"]
            assert payload["integrity"] == "local_model"
        finally:
            if old_state_dir is None:
                os.environ.pop("SIGIL_STATE_DIR", None)
            else:
                os.environ["SIGIL_STATE_DIR"] = old_state_dir
            if old_session_id is None:
                os.environ.pop("SIGIL_SESSION_ID", None)
            else:
                os.environ["SIGIL_SESSION_ID"] = old_session_id


def test_events_lineage_json_follows_transitive_inputs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        old_state_dir = os.environ.get("SIGIL_STATE_DIR")
        old_session_id = os.environ.get("SIGIL_SESSION_ID")
        os.environ["SIGIL_STATE_DIR"] = tmp
        os.environ["SIGIL_SESSION_ID"] = "test"
        try:
            root = append_event(
                {
                    "type": "command_generated",
                    "glyph": ",",
                    "integrity": "local_model",
                    "capability": "propose",
                    "taint": ["model"],
                }
            )
            child = append_event(
                {
                    "type": "command_continued",
                    "glyph": ",,",
                    "inputs": [root["id"]],
                    "integrity": "local_model",
                    "capability": "propose",
                    "taint": ["model"],
                }
            )
            selected = append_event(
                {
                    "type": "command_selected",
                    "glyph": ",,",
                    "inputs": [child["id"]],
                    "integrity": "local_model",
                    "capability": "propose",
                    "taint": ["model"],
                }
            )
            result = CliRunner().invoke(
                cli, ["events", "lineage", selected["id"], "--json"]
            )
            assert result.exit_code == 0, result.output
            lineage = json.loads(result.output)
            assert lineage["event_id"] == selected["id"]
            assert [node["event"]["type"] for node in lineage["nodes"]] == [
                "command_selected",
                "command_continued",
                "command_generated",
            ]
            assert [node["depth"] for node in lineage["nodes"]] == [0, 1, 2]
        finally:
            if old_state_dir is None:
                os.environ.pop("SIGIL_STATE_DIR", None)
            else:
                os.environ["SIGIL_STATE_DIR"] = old_state_dir
            if old_session_id is None:
                os.environ.pop("SIGIL_SESSION_ID", None)
            else:
                os.environ["SIGIL_SESSION_ID"] = old_session_id


def test_summary_json_is_read_only_session_inspection() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        old_state_dir = os.environ.get("SIGIL_STATE_DIR")
        old_session_id = os.environ.get("SIGIL_SESSION_ID")
        os.environ["SIGIL_STATE_DIR"] = tmp
        os.environ["SIGIL_SESSION_ID"] = "test"
        try:
            append_event(
                {
                    "type": "question",
                    "glyph": "?",
                    "integrity": "web",
                    "capability": "read",
                    "taint": ["web"],
                }
            )
            write_json(
                "last-command.json",
                {
                    "prompt": "status",
                    "commands": [
                        {"command": "git status --short", "note": "show changes"}
                    ],
                    "glyph": ",",
                    "integrity": "local_model",
                    "capability": "propose",
                    "taint": ["model"],
                },
            )
            result = CliRunner().invoke(cli, ["summary", "--json"])
            assert result.exit_code == 0, result.output
            summary = json.loads(result.output)
            assert summary["session_id"] == "test"
            assert summary["continuity"]["has_command"]
            assert summary["recent_events"][0]["type"] == "question"
            assert summary["recent_events"][0]["taint"] == ["web"]
        finally:
            if old_state_dir is None:
                os.environ.pop("SIGIL_STATE_DIR", None)
            else:
                os.environ["SIGIL_STATE_DIR"] = old_state_dir
            if old_session_id is None:
                os.environ.pop("SIGIL_SESSION_ID", None)
            else:
                os.environ["SIGIL_SESSION_ID"] = old_session_id


def test_select_rejects_multiple_candidates_without_interactive_stdin() -> None:
    stderr = StringIO()
    with (
        patch("sys.stdin", StringIO("")),
        patch("sigil.commands.selector_has_terminal", return_value=False),
    ):
        with redirect_stderr(stderr):
            with pytest.raises(SystemExit) as raised:
                select(
                    "status",
                    [
                        {"command": "git status --short", "note": "short status"},
                        {"command": "git status", "note": "full status"},
                    ],
                )
    assert raised.value.code == 2
    assert "--select requires an interactive terminal" in stderr.getvalue()


def test_select_uses_fzf_when_controlling_tty_exists() -> None:
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args == ["fzf", "--version"]:
            return subprocess.CompletedProcess(args, 0)
        return subprocess.CompletedProcess(args, 0, stdout="2\tgit status\tshown\n")

    with (
        patch("sys.stdin", StringIO("")),
        patch("sigil.commands.selector_has_terminal", return_value=True),
        patch("sigil.commands.subprocess.run", side_effect=fake_run),
    ):
        selected = select(
            "status",
            [
                {"command": "git status --short", "note": "short status"},
                {"command": "git status", "note": "full status"},
            ],
        )

    assert selected == "git status"
    assert calls[0] == ["fzf", "--version"]


def test_select_returns_single_candidate_without_interactive_stdin() -> None:
    with patch("sys.stdin", StringIO("")):
        selected = select(
            "status", [{"command": "git status --short", "note": "short status"}]
        )
    assert selected == "git status --short"


def test_renderer_defaults_to_glow_notty_when_available() -> None:
    with patch("sigil.question.shutil.which", return_value="/opt/homebrew/bin/glow"):
        with patch_dict(os.environ, {}, clear=True):
            assert renderer_command() == [
                "glow",
                "--style",
                "notty",
                "--width",
                "88",
                "-",
            ]


def test_renderer_uses_env_overrides() -> None:
    with patch("sigil.question.shutil.which", return_value="/opt/homebrew/bin/glow"):
        with patch_dict(
            os.environ,
            {"SIGIL_GLOW_STYLE": "tokyo-night", "SIGIL_GLOW_WIDTH": "100"},
            clear=True,
        ):
            assert renderer_command() == [
                "glow",
                "--style",
                "tokyo-night",
                "--width",
                "100",
                "-",
            ]


def test_renderer_falls_back_to_cat_without_glow() -> None:
    with patch("sigil.question.shutil.which", return_value=None):
        assert renderer_command() == ["cat"]


def test_writers_normalize_metadata() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        old_state_dir = os.environ.get("SIGIL_STATE_DIR")
        old_session_id = os.environ.get("SIGIL_SESSION_ID")
        os.environ["SIGIL_STATE_DIR"] = tmp
        os.environ["SIGIL_SESSION_ID"] = "test"
        try:
            event = append_event({"type": "legacy_shape"})
            assert event["integrity"] == "unknown"
            assert event["taint"] == ["legacy"]
            turn = append_jsonl(
                "last-question.jsonl",
                {
                    "role": "assistant",
                    "content": "answer",
                    "glyph": "?",
                    "integrity": "web",
                    "capability": "read",
                    "taint": ["web"],
                    "inputs": [event["id"]],
                    "provisional": True,
                },
            )
            assert turn["inputs"] == [event["id"]]
            assert turn["provisional"]
            written = write_jsonl("last-tools.jsonl", [{"type": "tool_start"}])
            assert written[0]["taint"] == ["legacy"]
            assert read_jsonl("last-tools.jsonl")[0]["integrity"] == "unknown"
            events_path = Path(tmp) / "events.jsonl"
            stored = json.loads(events_path.read_text(encoding="utf-8").splitlines()[0])
            assert stored["taint"] == ["legacy"]
        finally:
            if old_state_dir is None:
                os.environ.pop("SIGIL_STATE_DIR", None)
            else:
                os.environ["SIGIL_STATE_DIR"] = old_state_dir
            if old_session_id is None:
                os.environ.pop("SIGIL_SESSION_ID", None)
            else:
                os.environ["SIGIL_SESSION_ID"] = old_session_id


def test_previous_command_inherits_legacy_low_trust() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        old_state_dir = os.environ.get("SIGIL_STATE_DIR")
        old_session_id = os.environ.get("SIGIL_SESSION_ID")
        os.environ["SIGIL_STATE_DIR"] = tmp
        os.environ["SIGIL_SESSION_ID"] = "test"
        try:
            write_json(
                "last-command.json",
                {
                    "id": "legacy-command",
                    "prompt": "status",
                    "commands": [
                        {"command": "git status --short", "note": "show changes"}
                    ],
                },
            )
            prompt, candidates, security = previous()
            assert prompt == "status"
            assert candidates[0]["command"] == "git status --short"
            assert security["integrity"] == "unknown"
            assert security["taint"] == ["legacy"]
            assert security["inputs"] == ["legacy-command"]
        finally:
            if old_state_dir is None:
                os.environ.pop("SIGIL_STATE_DIR", None)
            else:
                os.environ["SIGIL_STATE_DIR"] = old_state_dir
            if old_session_id is None:
                os.environ.pop("SIGIL_SESSION_ID", None)
            else:
                os.environ["SIGIL_SESSION_ID"] = old_session_id


def test_question_and_follow_up_record_web_taint() -> None:

    class FakeProc:
        def __init__(self, stdout: StringIO | None = None) -> None:
            self.stdout = stdout

        def wait(self) -> int:
            return 0

    with tempfile.TemporaryDirectory() as tmp:
        old_state_dir = os.environ.get("SIGIL_STATE_DIR")
        old_session_id = os.environ.get("SIGIL_SESSION_ID")
        os.environ["SIGIL_STATE_DIR"] = tmp
        os.environ["SIGIL_SESSION_ID"] = "test"
        try:
            popen_calls = []

            def fake_popen(cmd: list[str], *args: object, **kwargs: object) -> FakeProc:
                popen_calls.append(cmd)
                if cmd[0] == "pi":
                    return FakeProc(StringIO(""))
                return FakeProc()

            with patch("sigil.question.start_qwen_for_pi", return_value=True):
                with patch("sigil.question.subprocess.Popen", side_effect=fake_popen):
                    assert ask("what is sigil?", json_output=True) == 0
            fresh_turn = read_jsonl("last-question.jsonl")[0]
            assert fresh_turn["glyph"] == "?"
            assert fresh_turn["integrity"] == "web"
            assert fresh_turn["capability"] == "read"
            assert fresh_turn["taint"] == ["web"]
            assert fresh_turn["provisional"]
            write_jsonl(
                "last-question.jsonl",
                [
                    {
                        "role": "user",
                        "content": "what is sigil?",
                        "event_id": "question-event",
                        "glyph": "?",
                        "integrity": "web",
                        "capability": "read",
                        "taint": ["web"],
                        "provisional": True,
                    },
                    {
                        "role": "assistant",
                        "content": "answer",
                        "event_id": "answer-event",
                        "glyph": "?",
                        "integrity": "web",
                        "capability": "read",
                        "taint": ["web"],
                        "provisional": True,
                    },
                ],
            )
            with patch("sigil.question.start_qwen_for_pi", return_value=True):
                with patch("sigil.question.subprocess.Popen", side_effect=fake_popen):
                    assert ask("continue", follow_up=True, json_output=True) == 0
            follow_up_turn = read_jsonl("last-question.jsonl")[-1]
            assert follow_up_turn["glyph"] == "??"
            assert follow_up_turn["inputs"] == ["question-event", "answer-event"]
            assert follow_up_turn["integrity"] == "web"
            assert follow_up_turn["capability"] == "read"
            assert follow_up_turn["taint"] == ["web"]
            assert follow_up_turn["provisional"]
            assert popen_calls
        finally:
            if old_state_dir is None:
                os.environ.pop("SIGIL_STATE_DIR", None)
            else:
                os.environ["SIGIL_STATE_DIR"] = old_state_dir
            if old_session_id is None:
                os.environ.pop("SIGIL_SESSION_ID", None)
            else:
                os.environ["SIGIL_SESSION_ID"] = old_session_id


def test_fix_and_previous_fix_inherit_model_taint() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        old_state_dir = os.environ.get("SIGIL_STATE_DIR")
        old_session_id = os.environ.get("SIGIL_SESSION_ID")
        os.environ["SIGIL_STATE_DIR"] = tmp
        os.environ["SIGIL_SESSION_ID"] = "test"
        try:
            record_failure("bad command", 2, "/tmp")
            captured_prompts = []

            def fake_chat_json(
                system: str, prompt: str, schema: dict[str, object]
            ) -> dict[str, object]:
                del system, schema
                captured_prompts.append(prompt)
                return {
                    "commands": [
                        {
                            "command": "fixed command",
                            "note": "repair the failed command",
                        }
                    ]
                }

            with patch("sigil.failure.ensure_server", return_value=True):
                with patch("sigil.failure.chat_json", side_effect=fake_chat_json):
                    prompt, candidates, security = generate_fixes()
            assert prompt == "bad command"
            assert candidates[0]["command"] == "fixed command"
            assert security["glyph"] == "^"
            assert security["integrity"] == "local_model"
            assert security["capability"] == "propose"
            assert security["taint"] == ["model"]
            assert security["inputs"][0]
            assert "Failed command: bad command" in captured_prompts[0]
            assert "Working directory: /tmp" in captured_prompts[0]
            assert "Recent stderr: <not captured>" in captured_prompts[0]
            assert "Recent stdout: <not captured>" in captured_prompts[0]
            assert "Do not invent missing stdout or stderr." in captured_prompts[0]
            previous_prompt, previous_candidates, previous_security = previous_fix()
            assert previous_prompt == "bad command"
            assert previous_candidates[0]["command"] == "fixed command"
            assert previous_security["glyph"] == "^^"
            assert previous_security["integrity"] == "local_model"
            assert previous_security["capability"] == "propose"
            assert previous_security["taint"] == ["model"]
            assert previous_security["inputs"][0]
        finally:
            if old_state_dir is None:
                os.environ.pop("SIGIL_STATE_DIR", None)
            else:
                os.environ["SIGIL_STATE_DIR"] = old_state_dir
            if old_session_id is None:
                os.environ.pop("SIGIL_SESSION_ID", None)
            else:
                os.environ["SIGIL_SESSION_ID"] = old_session_id


def test_failure_records_snippets_and_safe_context() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        old_state_dir = os.environ.get("SIGIL_STATE_DIR")
        old_session_id = os.environ.get("SIGIL_SESSION_ID")
        os.environ["SIGIL_STATE_DIR"] = tmp
        os.environ["SIGIL_SESSION_ID"] = "test"
        try:
            with patch(
                "sigil.failure.cwd_context",
                return_value={
                    "cwd": "/repo",
                    "git_branch": "main",
                    "git_status": [" M file.py"],
                },
            ):
                record_failure(
                    "pytest tests",
                    1,
                    "/repo",
                    stdout_snippet="stdout line",
                    stderr_snippet="stderr line",
                )
            failure = json.loads(
                (Path(tmp) / "sessions" / "test" / "last-failure.json").read_text(
                    encoding="utf-8"
                )
            )
            assert failure["stdout_snippet"] == "stdout line"
            assert failure["stderr_snippet"] == "stderr line"
            assert failure["context"]["git_branch"] == "main"
            assert failure["context"]["git_status"] == [" M file.py"]
        finally:
            if old_state_dir is None:
                os.environ.pop("SIGIL_STATE_DIR", None)
            else:
                os.environ["SIGIL_STATE_DIR"] = old_state_dir
            if old_session_id is None:
                os.environ.pop("SIGIL_SESSION_ID", None)
            else:
                os.environ["SIGIL_SESSION_ID"] = old_session_id


def test_select_fix_prints_note_to_stderr_but_stdout_is_command_only() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        old_state_dir = os.environ.get("SIGIL_STATE_DIR")
        old_session_id = os.environ.get("SIGIL_SESSION_ID")
        os.environ["SIGIL_STATE_DIR"] = tmp
        os.environ["SIGIL_SESSION_ID"] = "test"
        stderr = StringIO()
        try:
            record_failure("bad command", 2, "/tmp")
            with patch("sigil.failure.ensure_server", return_value=True):
                with patch(
                    "sigil.failure.chat_json",
                    return_value={
                        "commands": [
                            {
                                "command": "fixed command",
                                "note": "because bad command missed an argument",
                            }
                        ]
                    },
                ):
                    with redirect_stderr(stderr):
                        command = select_fix()
            assert command == "fixed command"
            assert "why: because bad command missed an argument" in stderr.getvalue()
        finally:
            if old_state_dir is None:
                os.environ.pop("SIGIL_STATE_DIR", None)
            else:
                os.environ["SIGIL_STATE_DIR"] = old_state_dir
            if old_session_id is None:
                os.environ.pop("SIGIL_SESSION_ID", None)
            else:
                os.environ["SIGIL_SESSION_ID"] = old_session_id


def test_pi_stream_records_web_tainted_answer_inputs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        saved = {
            key: os.environ.get(key)
            for key in [
                "SIGIL_STATE_DIR",
                "SIGIL_SESSION_ID",
                "SIGIL_CAPTURE_ANSWER",
                "SIGIL_CAPTURE_TRACE",
                "SIGIL_SECURITY_GLYPH",
                "SIGIL_SECURITY_INTEGRITY",
                "SIGIL_SECURITY_CAPABILITY",
                "SIGIL_SECURITY_TAINT",
                "SIGIL_SECURITY_PROVISIONAL",
                "SIGIL_SECURITY_INPUTS",
                "SIGIL_QUESTION",
                "SIGIL_PROMPT",
                "SIGIL_FOLLOW_UP",
            ]
        }
        os.environ["SIGIL_STATE_DIR"] = tmp
        os.environ["SIGIL_SESSION_ID"] = "test"
        os.environ["SIGIL_CAPTURE_ANSWER"] = "1"
        os.environ["SIGIL_SECURITY_GLYPH"] = "?"
        os.environ["SIGIL_SECURITY_INTEGRITY"] = "web"
        os.environ["SIGIL_SECURITY_CAPABILITY"] = "read"
        os.environ["SIGIL_SECURITY_TAINT"] = "web"
        os.environ["SIGIL_SECURITY_PROVISIONAL"] = "1"
        os.environ["SIGIL_SECURITY_INPUTS"] = "question-event"
        try:
            stdin = StringIO(
                json.dumps(
                    {
                        "type": "message_update",
                        "assistantMessageEvent": {
                            "type": "text_delta",
                            "delta": "answer",
                        },
                    }
                )
                + "\n"
            )
            assert stream_events(stdin=stdin, stdout=StringIO(), stderr=StringIO()) == 0
            answer = read_jsonl("last-question.jsonl")[0]
            assert answer["inputs"] == ["question-event"]
            assert answer["event_id"]
            assert answer["integrity"] == "web"
            assert answer["capability"] == "read"
            assert answer["taint"] == ["web"]
            assert answer["provisional"]
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def test_pi_stream_json_output_is_machine_readable() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        saved = {
            key: os.environ.get(key)
            for key in [
                "SIGIL_STATE_DIR",
                "SIGIL_SESSION_ID",
                "SIGIL_CAPTURE_ANSWER",
                "SIGIL_CAPTURE_TRACE",
                "SIGIL_SECURITY_GLYPH",
                "SIGIL_SECURITY_INTEGRITY",
                "SIGIL_SECURITY_CAPABILITY",
                "SIGIL_SECURITY_TAINT",
                "SIGIL_SECURITY_PROVISIONAL",
                "SIGIL_SECURITY_INPUTS",
                "SIGIL_QUESTION",
                "SIGIL_PROMPT",
                "SIGIL_FOLLOW_UP",
            ]
        }
        os.environ["SIGIL_STATE_DIR"] = tmp
        os.environ["SIGIL_SESSION_ID"] = "test"
        os.environ["SIGIL_CAPTURE_ANSWER"] = "1"
        os.environ["SIGIL_CAPTURE_TRACE"] = "1"
        os.environ["SIGIL_SECURITY_GLYPH"] = "?"
        os.environ["SIGIL_SECURITY_INTEGRITY"] = "web"
        os.environ["SIGIL_SECURITY_CAPABILITY"] = "read"
        os.environ["SIGIL_SECURITY_TAINT"] = "web"
        os.environ["SIGIL_SECURITY_PROVISIONAL"] = "1"
        os.environ["SIGIL_SECURITY_INPUTS"] = "question-event"
        os.environ["SIGIL_QUESTION"] = "what is sigil?"
        os.environ["SIGIL_PROMPT"] = "what is sigil?"
        os.environ["SIGIL_FOLLOW_UP"] = "0"
        try:
            stdin = StringIO(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "tool_execution_start",
                                "toolName": "web_search",
                                "args": {"query": "sigil"},
                            }
                        ),
                        json.dumps(
                            {"type": "tool_execution_end", "toolName": "web_search"}
                        ),
                        json.dumps(
                            {
                                "type": "message_update",
                                "assistantMessageEvent": {
                                    "type": "text_delta",
                                    "delta": "answer",
                                },
                            }
                        ),
                    ]
                )
                + "\n"
            )
            stdout = StringIO()
            stderr = StringIO()
            assert (
                stream_events(
                    stdin=stdin, stdout=stdout, stderr=stderr, json_output=True
                )
                == 0
            )
            payload = json.loads(stdout.getvalue())
            assert payload["ok"]
            assert payload["type"] == "answer"
            assert payload["question"] == "what is sigil?"
            assert payload["answer"] == "answer"
            assert payload["security"]["taint"] == ["web"]
            assert payload["malformed_events"] == 0
            assert payload["tools"][0]["tool"] == "web_search"
            assert stderr.getvalue() == ""
            assert read_jsonl("last-question.jsonl")[-1]["content"] == "answer"
            assert len(read_jsonl("last-tools.jsonl")) == 2
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def test_pi_stream_json_output_counts_malformed_events() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        saved = {
            key: os.environ.get(key)
            for key in [
                "SIGIL_STATE_DIR",
                "SIGIL_SESSION_ID",
                "SIGIL_QUESTION",
                "SIGIL_PROMPT",
                "SIGIL_FOLLOW_UP",
            ]
        }
        os.environ["SIGIL_STATE_DIR"] = tmp
        os.environ["SIGIL_SESSION_ID"] = "test"
        os.environ["SIGIL_QUESTION"] = "question"
        os.environ["SIGIL_PROMPT"] = "question"
        os.environ["SIGIL_FOLLOW_UP"] = "0"
        try:
            stdin = StringIO(
                "not json\n"
                + json.dumps(
                    {
                        "type": "message_update",
                        "assistantMessageEvent": {
                            "type": "text_delta",
                            "delta": "answer",
                        },
                    }
                )
                + "\n"
            )
            stdout = StringIO()
            assert (
                stream_events(
                    stdin=stdin, stdout=stdout, stderr=StringIO(), json_output=True
                )
                == 0
            )
            payload = json.loads(stdout.getvalue())
            assert payload["malformed_events"] == 1
            assert payload["answer"] == "answer"
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def test_pi_stream_non_tty_status_has_no_control_codes_or_color() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        saved = {
            key: os.environ.get(key)
            for key in ["SIGIL_STATE_DIR", "SIGIL_SESSION_ID", "SIGIL_CAPTURE_TRACE"]
        }
        os.environ["SIGIL_STATE_DIR"] = tmp
        os.environ["SIGIL_SESSION_ID"] = "test"
        os.environ["SIGIL_CAPTURE_TRACE"] = "1"
        try:
            stdin = StringIO(
                json.dumps(
                    {
                        "type": "tool_execution_start",
                        "toolName": "web_search",
                        "args": {"query": "sigil"},
                    }
                )
                + "\n"
            )
            stderr = StringIO()
            assert stream_events(stdin=stdin, stdout=StringIO(), stderr=stderr) == 0
            status = stderr.getvalue()
            assert "web_search" in status
            assert "\x1b" not in status
            assert "\r" not in status
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def test_no_color_disables_tty_color() -> None:
    saved = os.environ.get("NO_COLOR")
    try:
        os.environ.pop("NO_COLOR", None)
        assert should_color(TtyStringIO())
        os.environ["NO_COLOR"] = "1"
        assert not should_color(TtyStringIO())
        assert not should_color(StringIO())
    finally:
        if saved is None:
            os.environ.pop("NO_COLOR", None)
        else:
            os.environ["NO_COLOR"] = saved
