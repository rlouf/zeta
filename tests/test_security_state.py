from __future__ import annotations
import pytest
import json
import os
import tempfile
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

from click.testing import CliRunner

from _patch import patch, patch_dict
from sigil.cli import cli, main
from sigil.failure import failure_context_prompt, record_failure, truncate_snippet
from sigil.handoff import (
    LAST_BASH_HANDOFF_FILE,
    PENDING_BASH_HANDOFF_FILE,
    consume_latest_bash_handoff,
    prepare_bash_handoff,
    record_bash_handoffs,
)
from sigil.pi_stream import should_color, stream_events
from sigil.question import (
    QUESTION_SYSTEM_PROMPT,
    ask,
    continuation_prompt,
    discussion_turns,
    renderer_command,
)
from sigil.security import (
    inherit_security,
    create_trust_metadata,
    normalize_trust_record,
)
from sigil.session import recent_turns, recent_turns_context, record_turn
from sigil.state import append_event, append_jsonl, read_jsonl, write_jsonl
from sigil.tty import confirmation_tty_paths, confirm_on_tty


class TtyStringIO(StringIO):
    def isatty(self) -> bool:
        return True


def test_legacy_record_is_alpha_propose_mode() -> None:
    record = normalize_trust_record({"type": "old"})
    assert record["mode"] == "propose"
    assert record["labels"] == []
    assert "integrity" not in record
    assert "capability" not in record
    assert "taint" not in record


def test_question_system_prompt_points_pi_at_events_log_for_older_history() -> None:
    assert "events.jsonl" in QUESTION_SYSTEM_PROMPT
    assert "at most one tool call" in QUESTION_SYSTEM_PROMPT


def test_continuation_keeps_inputs_and_alpha_labels() -> None:
    inherited = inherit_security(
        glyph="??",
        input_records=[
            {
                "event_id": "question-1",
                "mode": "read-only",
                "labels": ["network"],
            },
            {"event_id": "legacy-1"},
        ],
        mode="read-only",
    )
    assert inherited["mode"] == "read-only"
    assert inherited["inputs"] == ["question-1", "legacy-1"]
    assert inherited["labels"] == ["network"]


def test_create_trust_metadata_keeps_alpha_fields_only() -> None:
    security = create_trust_metadata(
        glyph=",",
        mode="propose",
        labels=["network", "local", "publish"],
    )
    assert security == {
        "glyph": ",",
        "mode": "propose",
        "labels": ["network", "publish"],
        "inputs": [],
    }


def test_top_level_help_lists_commands() -> None:
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "Commands:" in result.output
    for command in [
        "act",
        "ask",
        "command",
        "doctor",
        "events",
        "install",
        "session",
        "status",
    ]:
        assert command in result.output
    assert "\n  question" not in result.output


def test_main_rewrites_missing_executable_errors() -> None:
    stderr = StringIO()
    missing = FileNotFoundError(2, "No such file or directory", "pi")
    with patch("sigil.cli.cli.main", side_effect=missing):
        with redirect_stderr(stderr):
            assert main(["ask", "hello"]) == 127
    assert "missing executable: pi" in stderr.getvalue()


def test_main_rewrites_permission_errors() -> None:
    stderr = StringIO()
    denied = PermissionError(1, "Operation not permitted", "/nope/events.jsonl")
    with patch("sigil.cli.cli.main", side_effect=denied):
        with redirect_stderr(stderr):
            assert main(["ask", "hello"]) == 1
    assert "permission denied: /nope/events.jsonl" in stderr.getvalue()


def test_events_default_lists_recent_events() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        old_state_dir = os.environ.get("SIGIL_STATE_DIR")
        old_session_id = os.environ.get("SIGIL_SESSION_ID")
        os.environ["SIGIL_STATE_DIR"] = tmp
        os.environ["SIGIL_SESSION_ID"] = "test"
        try:
            first = append_event({"type": "first"})
            second = append_event(
                {
                    "type": "operator_command_executed",
                    "glyph": ",,",
                    "command": "git status --short",
                    "status": 0,
                    "mode": "execute-write",
                }
            )
            text = CliRunner().invoke(cli, ["events", "--limit", "1"])
            listed = CliRunner().invoke(cli, ["events", "list", "--json"])
            raw = CliRunner().invoke(cli, ["events", "list", "--json", "--raw"])
        finally:
            if old_state_dir is None:
                os.environ.pop("SIGIL_STATE_DIR", None)
            else:
                os.environ["SIGIL_STATE_DIR"] = old_state_dir
            if old_session_id is None:
                os.environ.pop("SIGIL_SESSION_ID", None)
            else:
                os.environ["SIGIL_SESSION_ID"] = old_session_id

    assert text.exit_code == 0, text.output
    assert text.output.splitlines()[0].split() == [
        "time",
        "id",
        "action",
        "trust",
        "session",
        "summary",
    ]
    assert str(second["id"])[:8] in text.output
    assert ",, executed" in text.output
    assert "execute-write" in text.output
    assert "git status --short -> 0" in text.output
    assert first["id"] not in text.output
    assert listed.exit_code == 0, listed.output
    summaries = json.loads(listed.output)
    assert [event["type"] for event in summaries] == [
        "first",
        "operator_command_executed",
    ]
    assert summaries[-1]["short_id"] == str(second["id"])[:8]
    assert summaries[-1]["action"] == ",, executed"
    assert summaries[-1]["summary"] == "git status --short -> 0"
    assert summaries[-1]["lineage"] == f"sigil events lineage {second['id']}"
    assert raw.exit_code == 0, raw.output
    assert "short_id" not in json.loads(raw.output)[0]


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
                    "mode": "propose",
                }
            )
            child = append_event(
                {
                    "type": "command_continued",
                    "glyph": ",,",
                    "inputs": [root["id"]],
                    "mode": "propose",
                }
            )
            selected = append_event(
                {
                    "type": "command_selected",
                    "glyph": ",,",
                    "inputs": [child["id"]],
                    "mode": "propose",
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


def test_confirmation_uses_exported_tty_before_dev_tty() -> None:
    with patch_dict(
        os.environ,
        {"SIGIL_TTY": "/tmp/sigil-tty", "TTY": "/tmp/fallback-tty"},
        clear=True,
    ):
        assert confirmation_tty_paths() == [
            "/tmp/sigil-tty",
            "/tmp/fallback-tty",
            "/dev/tty",
        ]


def test_confirmation_uses_exported_tty_fd_before_paths() -> None:
    master_fd, slave_fd = os.openpty()
    try:
        with (
            patch_dict(
                os.environ,
                {"SIGIL_TTY_FD": str(slave_fd), "SIGIL_TTY": "/tmp/sigil-tty"},
                clear=True,
            ),
            patch("os.open", side_effect=AssertionError("path fallback used")),
        ):
            os.write(master_fd, b"yes\n")
            assert confirm_on_tty("Use it? ")
    finally:
        os.close(master_fd)
        os.close(slave_fd)


def test_confirmation_failure_is_visible() -> None:
    stderr = StringIO()
    with (
        patch_dict(os.environ, {}, clear=True),
        patch("os.open", side_effect=OSError()),
        redirect_stderr(stderr),
    ):
        assert not confirm_on_tty("Use it? ")

    assert stderr.getvalue().count("could not open a terminal") == 1
    assert "tried /dev/tty" in stderr.getvalue()


def test_session_show_and_clear_include_act_state() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        old_state_dir = os.environ.get("SIGIL_STATE_DIR")
        old_session_id = os.environ.get("SIGIL_SESSION_ID")
        os.environ["SIGIL_STATE_DIR"] = tmp
        os.environ["SIGIL_SESSION_ID"] = "test"
        session_root = Path(tmp) / "sessions" / "test"
        session_root.mkdir(parents=True)
        act_path = session_root / "last-act.jsonl"
        act_path.write_text(
            json.dumps({"act": {"act_id": "act", "status": "active"}}) + "\n",
            encoding="utf-8",
        )
        try:
            shown = CliRunner().invoke(cli, ["session", "show", "--json"])
            cleared = CliRunner().invoke(cli, ["session", "clear", "--json"])
            removed = json.loads(cleared.output)
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
    snapshot = json.loads(shown.output)
    assert snapshot["files"]["last-act.jsonl"][0]["act"]["act_id"] == "act"
    assert cleared.exit_code == 0, cleared.output
    assert str(act_path) in removed["removed"]
    assert not act_path.exists()


def test_session_list_includes_last_event_context() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        old_state_dir = os.environ.get("SIGIL_STATE_DIR")
        old_session_id = os.environ.get("SIGIL_SESSION_ID")
        os.environ["SIGIL_STATE_DIR"] = tmp
        os.environ["SIGIL_SESSION_ID"] = "alpha"
        alpha_root = Path(tmp) / "sessions" / "alpha"
        beta_root = Path(tmp) / "sessions" / "beta"
        alpha_root.mkdir(parents=True)
        beta_root.mkdir(parents=True)
        (alpha_root / "last-failure.json").write_text("{}", encoding="utf-8")
        try:
            append_event({"type": "old_alpha", "time": 1.0, "cwd": "/old"})
            append_event({"type": "new_alpha", "time": 2.0, "cwd": "/repo"})
            os.environ["SIGIL_SESSION_ID"] = "beta"
            append_event({"type": "beta_event", "time": 3.0, "cwd": "/other"})

            listed = CliRunner().invoke(cli, ["session", "list", "--json"])
            text = CliRunner().invoke(cli, ["session", "list"])
        finally:
            if old_state_dir is None:
                os.environ.pop("SIGIL_STATE_DIR", None)
            else:
                os.environ["SIGIL_STATE_DIR"] = old_state_dir
            if old_session_id is None:
                os.environ.pop("SIGIL_SESSION_ID", None)
            else:
                os.environ["SIGIL_SESSION_ID"] = old_session_id

    assert listed.exit_code == 0, listed.output
    sessions = {session["session_id"]: session for session in json.loads(listed.output)}
    assert sessions["alpha"]["last_cwd"] == "/repo"
    assert sessions["alpha"]["last_event_type"] == "new_alpha"
    assert sessions["alpha"]["last_event_time"] == 2.0
    assert sessions["alpha"]["files"] == ["last-failure.json"]
    assert sessions["beta"]["last_cwd"] == "/other"
    assert text.exit_code == 0, text.output
    assert "alpha\t/repo\tnew_alpha\t" in text.output
    assert "beta\t/other\tbeta_event\t" in text.output


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
            assert event["mode"] == "propose"
            assert event["labels"] == []
            turn = append_jsonl(
                "last-question.jsonl",
                {
                    "role": "assistant",
                    "content": "answer",
                    "glyph": "?",
                    "mode": "read-only",
                    "labels": ["network", "local"],
                    "inputs": [event["id"]],
                },
            )
            assert turn["inputs"] == [event["id"]]
            assert turn["mode"] == "read-only"
            assert turn["labels"] == ["network"]
            written = write_jsonl("last-tools.jsonl", [{"type": "tool_start"}])
            assert written[0]["mode"] == "propose"
            assert read_jsonl("last-tools.jsonl")[0]["labels"] == []
            events_path = Path(tmp) / "events.jsonl"
            stored = json.loads(events_path.read_text(encoding="utf-8").splitlines()[0])
            assert stored["mode"] == "propose"
        finally:
            if old_state_dir is None:
                os.environ.pop("SIGIL_STATE_DIR", None)
            else:
                os.environ["SIGIL_STATE_DIR"] = old_state_dir
            if old_session_id is None:
                os.environ.pop("SIGIL_SESSION_ID", None)
            else:
                os.environ["SIGIL_SESSION_ID"] = old_session_id


def test_question_routes_record_alpha_trust_labels() -> None:

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

            with patch("sigil.question.ensure_model_for_pi", return_value=True):
                with patch("sigil.question.subprocess.Popen", side_effect=fake_popen):
                    assert ask("what is sigil?", json_output=True) == 0
            fresh_turn = read_jsonl("last-question.jsonl")[0]
            assert fresh_turn["glyph"] == "?"
            assert fresh_turn["mode"] == "read-only"
            assert fresh_turn["labels"] == []
            with patch("sigil.question.ensure_model_for_pi", return_value=True):
                with patch("sigil.question.subprocess.Popen", side_effect=fake_popen):
                    assert (
                        ask(
                            "what is sigil on the web?",
                            glyph="??",
                            tools="read,web_search",
                            use_web=True,
                            json_output=True,
                        )
                        == 0
                    )
            web_turn = read_jsonl("last-question.jsonl")[-1]
            assert web_turn["glyph"] == "??"
            assert web_turn["mode"] == "read-only"
            assert web_turn["labels"] == ["network"]
            pi_calls = [cmd for cmd in popen_calls if cmd[0] == "pi"]
            assert len(pi_calls) == 2
            assert pi_calls[0][pi_calls[0].index("--tools") + 1] == "read"
            assert pi_calls[1][pi_calls[1].index("--tools") + 1] == "read,web_search"
            for cmd in pi_calls:
                assert "--extension" not in cmd
                system_prompt = cmd[cmd.index("--append-system-prompt") + 1]
                assert system_prompt == QUESTION_SYSTEM_PROMPT
                assert "at most one tool call" in system_prompt
        finally:
            if old_state_dir is None:
                os.environ.pop("SIGIL_STATE_DIR", None)
            else:
                os.environ["SIGIL_STATE_DIR"] = old_state_dir
            if old_session_id is None:
                os.environ.pop("SIGIL_SESSION_ID", None)
            else:
                os.environ["SIGIL_SESSION_ID"] = old_session_id


def test_bash_handoff_records_and_consumes_blocked_command() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            pending = prepare_bash_handoff()
            pending.write_text(
                json.dumps(
                    {
                        "toolCallId": "call-1",
                        "toolName": "bash",
                        "command": "git diff --stat",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            records = record_bash_handoffs(
                source_event={
                    "id": "question-event",
                    "mode": "read-only",
                    "labels": ["network"],
                },
                source_security={
                    "glyph": "?",
                    "mode": "read-only",
                    "labels": ["network"],
                },
            )

            assert records[0]["command"] == "git diff --stat"
            assert records[0]["mode"] == "propose"
            assert records[0]["labels"] == ["network"]
            assert records[0]["inputs"] == ["question-event"]
            assert not (
                Path(tmp) / "sessions/test" / PENDING_BASH_HANDOFF_FILE
            ).exists()

            consumed = consume_latest_bash_handoff()
            assert consumed is not None
            assert consumed["command"] == "git diff --stat"
            assert read_jsonl(LAST_BASH_HANDOFF_FILE) == []


def test_failure_context_prompt_uses_recorded_failure_without_inventing_output() -> (
    None
):
    with tempfile.TemporaryDirectory() as tmp:
        old_state_dir = os.environ.get("SIGIL_STATE_DIR")
        old_session_id = os.environ.get("SIGIL_SESSION_ID")
        os.environ["SIGIL_STATE_DIR"] = tmp
        os.environ["SIGIL_SESSION_ID"] = "test"
        try:
            record_failure("bad command", 2, "/tmp")
            failure = json.loads(
                (Path(tmp) / "sessions" / "test" / "last-failure.json").read_text(
                    encoding="utf-8"
                )
            )
            prompt = failure_context_prompt(failure)
            assert failure["glyph"] == "failure"
            assert failure["mode"] == "propose"
            assert "Failed command: bad command" in prompt
            assert "Working directory: /tmp" in prompt
            assert "Recent stderr: <not captured>" in prompt
            assert "Recent stdout: <not captured>" in prompt
            assert "Do not invent missing stdout or stderr." in prompt
        finally:
            if old_state_dir is None:
                os.environ.pop("SIGIL_STATE_DIR", None)
            else:
                os.environ["SIGIL_STATE_DIR"] = old_state_dir
            if old_session_id is None:
                os.environ.pop("SIGIL_SESSION_ID", None)
            else:
                os.environ["SIGIL_SESSION_ID"] = old_session_id


@pytest.mark.parametrize(
    ("command", "status", "stdout_snippet", "stderr_snippet", "expected"),
    [
        (
            "uv run pytest",
            1,
            "tests/test_parser.py::test_parse FAILED",
            "AssertionError: expected command",
            "AssertionError: expected command",
        ),
        (
            "missing-tool --version",
            127,
            "",
            "zsh: command not found: missing-tool",
            "command not found: missing-tool",
        ),
        (
            "git push origin main",
            128,
            "",
            "fatal: Could not read from remote repository.",
            "Could not read from remote repository",
        ),
        (
            "curl https://example.invalid",
            6,
            "",
            "curl: (6) Could not resolve host: example.invalid",
            "Could not resolve host",
        ),
        (
            "touch /root/nope",
            1,
            "",
            "touch: /root/nope: Permission denied",
            "Permission denied",
        ),
    ],
)
def test_failure_context_prompt_covers_common_failure_fixtures(
    command: str,
    status: int,
    stdout_snippet: str,
    stderr_snippet: str,
    expected: str,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            with patch("sigil.failure.cwd_context", return_value={"cwd": "/repo"}):
                record_failure(
                    command,
                    status,
                    "/repo",
                    stdout_snippet=stdout_snippet,
                    stderr_snippet=stderr_snippet,
                )
            failure = json.loads(
                (Path(tmp) / "sessions" / "test" / "last-failure.json").read_text(
                    encoding="utf-8"
                )
            )
            prompt = failure_context_prompt(failure)

    assert f"Failed command: {command}" in prompt
    assert f"Exit status: {status}" in prompt
    assert expected in prompt
    assert "Do not invent missing stdout or stderr." in prompt


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


def test_failure_snippets_are_redacted_before_storage() -> None:
    assert (
        truncate_snippet("Authorization: Bearer secret-token")
        == "Authorization: Bearer [REDACTED]"
    )
    assert truncate_snippet("API_KEY=abc123") == "API_KEY=[REDACTED]"
    assert truncate_snippet("aws AKIA1234567890ABCDEF") == "aws [REDACTED_AWS_KEY]"


def read_recent_turns(tmp: str) -> list[dict[str, object]]:
    path = Path(tmp) / "sessions" / "test" / "recent-turns.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line:
            rows.append(json.loads(line))
    return rows


def test_record_turn_appends_command_with_read_only_mode() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            record_turn("ls -la", 0, "/repo")

        rows = read_recent_turns(tmp)
        assert len(rows) == 1
        row = rows[0]
        assert row["command"] == "ls -la"
        assert row["status"] == 0
        assert row["turn_cwd"] == "/repo"
        assert row["mode"] == "read-only"
        assert row["labels"] == []
        assert row["glyph"] == "turn"


def test_record_turn_trims_buffer_to_last_fifty_entries() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            for index in range(60):
                record_turn(f"cmd-{index}", 0, "/repo")

        rows = read_recent_turns(tmp)
        assert len(rows) == 50
        assert rows[0]["command"] == "cmd-10"
        assert rows[-1]["command"] == "cmd-59"


def test_record_turn_skips_empty_command() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            record_turn("", 0, "/repo")
            record_turn("   ", 0, "/repo")
        assert read_recent_turns(tmp) == []


def test_record_turn_skips_leading_whitespace_command() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            record_turn(" curl -H 'Authorization: Bearer secret' x", 0, "/repo")
            record_turn("\tprintenv SECRET", 0, "/repo")
        assert read_recent_turns(tmp) == []


def test_record_turn_skips_comma_question_and_sigil_commands() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            record_turn(", run tests", 0, "/repo")
            record_turn("? what is this", 0, "/repo")
            record_turn("sigil ask hello", 0, "/repo")
            record_turn("__sigil_precmd", 0, "/repo")
        assert read_recent_turns(tmp) == []


def test_record_turn_records_unsupported_caret_text() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            record_turn("^^", 0, "/repo")
        rows = read_recent_turns(tmp)
        assert len(rows) == 1
        assert rows[0]["command"] == "^^"


def test_record_turn_fans_out_to_record_failure_on_nonzero_status() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            with patch("sigil.failure.cwd_context", return_value={"cwd": "/repo"}):
                record_turn(
                    "pytest tests",
                    1,
                    "/repo",
                    stdout_snippet="captured stdout",
                    stderr_snippet="captured stderr",
                )

        rows = read_recent_turns(tmp)
        assert len(rows) == 1
        assert rows[0]["command"] == "pytest tests"
        assert rows[0]["status"] == 1

        failure_path = Path(tmp) / "sessions" / "test" / "last-failure.json"
        failure = json.loads(failure_path.read_text(encoding="utf-8"))
        assert failure["command"] == "pytest tests"
        assert failure["status"] == 1
        assert failure["stdout_snippet"] == "captured stdout"
        assert failure["stderr_snippet"] == "captured stderr"


def test_record_turn_persists_redacted_snippets_in_recent_turns() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            record_turn(
                "pytest tests",
                1,
                "/repo",
                stdout_snippet="API_KEY=abc123",
                stderr_snippet="Authorization: Bearer secret-token",
            )

        rows = read_recent_turns(tmp)
        assert rows[0]["stdout_snippet"] == "API_KEY=[REDACTED]"
        assert rows[0]["stderr_snippet"] == "Authorization: Bearer [REDACTED]"


def test_recent_turns_context_includes_compact_snippets() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            record_turn(
                "pytest tests",
                1,
                "/repo",
                stdout_snippet="collected 1 item",
                stderr_snippet="AssertionError: expected true",
            )
            context = recent_turns_context()

    assert "pytest tests (exit 1)" in context
    assert "stderr: AssertionError: expected true" in context
    assert "stdout: collected 1 item" in context


def test_record_turn_does_not_record_failure_on_zero_status() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            record_turn("ls", 0, "/repo")

        rows = read_recent_turns(tmp)
        assert len(rows) == 1
        failure_path = Path(tmp) / "sessions" / "test" / "last-failure.json"
        assert not failure_path.exists()


def test_record_turn_cli_command_persists_entry() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            result = CliRunner().invoke(
                cli,
                ["record-turn", "--status", "0", "--cwd", "/repo", "ls -la"],
            )

        assert result.exit_code == 0, result.output
        rows = read_recent_turns(tmp)
        assert len(rows) == 1
        assert rows[0]["command"] == "ls -la"
        assert rows[0]["status"] == 0


def test_recent_turns_returns_empty_when_file_missing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            assert recent_turns() == []


def test_recent_turns_returns_last_n_entries_in_order() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            for index in range(15):
                record_turn(f"cmd-{index}", 0, "/repo")
            turns = recent_turns(limit=5)
        assert [turn["command"] for turn in turns] == [
            "cmd-10",
            "cmd-11",
            "cmd-12",
            "cmd-13",
            "cmd-14",
        ]


def test_fresh_ask_prepends_recent_turns_context_to_pi_prompt() -> None:

    class FakeProc:
        def __init__(self, stdout: StringIO | None = None) -> None:
            self.stdout = stdout

        def wait(self) -> int:
            return 0

    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            record_turn("ls -la", 0, "/repo")
            record_turn("pytest tests/test_foo.py", 1, "/repo")
            popen_calls: list[list[str]] = []

            def fake_popen(cmd: list[str], *args: object, **kwargs: object) -> FakeProc:
                popen_calls.append(cmd)
                if cmd[0] == "pi":
                    return FakeProc(StringIO(""))
                return FakeProc()

            with (
                patch("sigil.question.ensure_model_for_pi", return_value=True),
                patch("sigil.question.subprocess.Popen", side_effect=fake_popen),
            ):
                assert ask("what should I do next?", json_output=True) == 0

    pi_cmd = next(cmd for cmd in popen_calls if cmd[0] == "pi")
    prompt = pi_cmd[-1]
    assert "Recent shell activity:" in prompt
    assert "ls -la" in prompt
    assert "pytest tests/test_foo.py" in prompt
    assert "what should I do next?" in prompt


def test_fresh_ask_why_failed_includes_last_failure_context() -> None:
    class FakeProc:
        def __init__(self, stdout: StringIO | None = None) -> None:
            self.stdout = stdout

        def wait(self) -> int:
            return 0

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
            popen_calls: list[list[str]] = []

            def fake_popen(cmd: list[str], *args: object, **kwargs: object) -> FakeProc:
                popen_calls.append(cmd)
                if cmd[0] == "pi":
                    return FakeProc(StringIO(""))
                return FakeProc()

            with (
                patch("sigil.question.ensure_model_for_pi", return_value=True),
                patch("sigil.question.subprocess.Popen", side_effect=fake_popen),
            ):
                assert ask("why failed", json_output=True) == 0

    pi_cmd = next(cmd for cmd in popen_calls if cmd[0] == "pi")
    prompt = pi_cmd[-1]
    assert "Last failed command context:" in prompt
    assert "Failed command: pytest tests/test_foo.py" in prompt
    assert "Recent stderr:" in prompt
    assert "AssertionError: no" in prompt


def test_explicit_follow_up_ask_does_not_include_recent_turns_context() -> None:

    class FakeProc:
        def __init__(self, stdout: StringIO | None = None) -> None:
            self.stdout = stdout

        def wait(self) -> int:
            return 0

    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            record_turn("ls -la", 0, "/repo")
            write_jsonl(
                "last-question.jsonl",
                [
                    {"role": "user", "content": "first", "event_id": "q1"},
                    {"role": "assistant", "content": "ans", "event_id": "a1"},
                ],
            )
            popen_calls: list[list[str]] = []

            def fake_popen(cmd: list[str], *args: object, **kwargs: object) -> FakeProc:
                popen_calls.append(cmd)
                if cmd[0] == "pi":
                    return FakeProc(StringIO(""))
                return FakeProc()

            with (
                patch("sigil.question.ensure_model_for_pi", return_value=True),
                patch("sigil.question.subprocess.Popen", side_effect=fake_popen),
            ):
                assert (
                    ask(
                        continuation_prompt("follow up", discussion_turns()),
                        glyph="??",
                        tools="read,web_search",
                        use_web=True,
                        append_transcript=True,
                        json_output=True,
                    )
                    == 0
                )

    pi_cmd = next(cmd for cmd in popen_calls if cmd[0] == "pi")
    prompt = pi_cmd[-1]
    assert "Recent shell activity" not in prompt


def test_fresh_ask_omits_recent_turns_section_when_none_recorded() -> None:

    class FakeProc:
        def __init__(self, stdout: StringIO | None = None) -> None:
            self.stdout = stdout

        def wait(self) -> int:
            return 0

    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            popen_calls: list[list[str]] = []

            def fake_popen(cmd: list[str], *args: object, **kwargs: object) -> FakeProc:
                popen_calls.append(cmd)
                if cmd[0] == "pi":
                    return FakeProc(StringIO(""))
                return FakeProc()

            with (
                patch("sigil.question.ensure_model_for_pi", return_value=True),
                patch("sigil.question.subprocess.Popen", side_effect=fake_popen),
            ):
                assert ask("hello", json_output=True) == 0

    pi_cmd = next(cmd for cmd in popen_calls if cmd[0] == "pi")
    prompt = pi_cmd[-1]
    assert "Recent shell activity" not in prompt
    assert prompt == "hello"


def test_recent_turns_skips_malformed_lines() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            record_turn("ls", 0, "/repo")
            path = Path(tmp) / "sessions" / "test" / "recent-turns.jsonl"
            path.write_text(
                path.read_text(encoding="utf-8") + "not json\n",
                encoding="utf-8",
            )
            turns = recent_turns()
        assert [turn["command"] for turn in turns] == ["ls"]


def test_pi_stream_records_answer_inputs_and_alpha_labels() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        saved = {
            key: os.environ.get(key)
            for key in [
                "SIGIL_STATE_DIR",
                "SIGIL_SESSION_ID",
                "SIGIL_CAPTURE_ANSWER",
                "SIGIL_CAPTURE_TRACE",
                "SIGIL_TRUST_GLYPH",
                "SIGIL_TRUST_MODE",
                "SIGIL_TRUST_LABELS",
                "SIGIL_TRUST_INPUTS",
                "SIGIL_QUESTION",
                "SIGIL_PROMPT",
                "SIGIL_FOLLOW_UP",
            ]
        }
        os.environ["SIGIL_STATE_DIR"] = tmp
        os.environ["SIGIL_SESSION_ID"] = "test"
        os.environ["SIGIL_CAPTURE_ANSWER"] = "1"
        os.environ["SIGIL_TRUST_GLYPH"] = "?"
        os.environ["SIGIL_TRUST_MODE"] = "read-only"
        os.environ["SIGIL_TRUST_LABELS"] = "network"
        os.environ["SIGIL_TRUST_INPUTS"] = "question-event"
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
            assert answer["mode"] == "read-only"
            assert answer["labels"] == ["network"]
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
                "SIGIL_TRUST_GLYPH",
                "SIGIL_TRUST_MODE",
                "SIGIL_TRUST_LABELS",
                "SIGIL_TRUST_INPUTS",
                "SIGIL_QUESTION",
                "SIGIL_PROMPT",
                "SIGIL_FOLLOW_UP",
            ]
        }
        os.environ["SIGIL_STATE_DIR"] = tmp
        os.environ["SIGIL_SESSION_ID"] = "test"
        os.environ["SIGIL_CAPTURE_ANSWER"] = "1"
        os.environ["SIGIL_CAPTURE_TRACE"] = "1"
        os.environ["SIGIL_TRUST_GLYPH"] = "?"
        os.environ["SIGIL_TRUST_MODE"] = "read-only"
        os.environ["SIGIL_TRUST_LABELS"] = "network"
        os.environ["SIGIL_TRUST_INPUTS"] = "question-event"
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
            assert payload["security"]["labels"] == ["network"]
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


def test_pi_stream_shows_function_call_events() -> None:
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
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "function_call",
                                "name": "web_search",
                                "arguments": json.dumps({"query": "sigil ???"}),
                            }
                        ),
                        json.dumps(
                            {
                                "type": "function_call_result",
                                "name": "web_search",
                            }
                        ),
                    ]
                )
                + "\n"
            )
            stderr = StringIO()
            assert stream_events(stdin=stdin, stdout=StringIO(), stderr=stderr) == 0
            status = stderr.getvalue()
            tools = read_jsonl("last-tools.jsonl")

            assert "web_search" in status
            assert "sigil ???" in status
            assert [event["type"] for event in tools] == ["tool_start", "tool_end"]
            assert tools[0]["tool"] == "web_search"
            assert tools[0]["args"] == {"query": "sigil ???"}
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def test_pi_stream_shows_nested_tool_call_updates() -> None:
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
                        "type": "message_update",
                        "assistantMessageEvent": {
                            "type": "tool_call",
                            "toolName": "read",
                            "input": {"path": "src/sigil/question.py"},
                        },
                    }
                )
                + "\n"
            )
            stderr = StringIO()
            assert stream_events(stdin=stdin, stdout=StringIO(), stderr=stderr) == 0

            assert "read" in stderr.getvalue()
            assert "src/sigil/question.py" in stderr.getvalue()
            assert read_jsonl("last-tools.jsonl")[0]["tool"] == "read"
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def test_pi_stream_shows_tool_start_without_detail() -> None:
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
                        "toolName": "read",
                        "args": {},
                    }
                )
                + "\n"
            )
            stderr = StringIO()
            assert stream_events(stdin=stdin, stdout=StringIO(), stderr=stderr) == 0

            assert "❯ read" in stderr.getvalue()
            assert read_jsonl("last-tools.jsonl")[0]["tool"] == "read"
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def test_pi_stream_compact_mode_suppresses_prose_and_summarizes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        saved = {
            key: os.environ.get(key)
            for key in [
                "SIGIL_STATE_DIR",
                "SIGIL_SESSION_ID",
                "SIGIL_CAPTURE_ANSWER",
                "SIGIL_CAPTURE_TRACE",
            ]
        }
        os.environ["SIGIL_STATE_DIR"] = tmp
        os.environ["SIGIL_SESSION_ID"] = "test"
        os.environ["SIGIL_CAPTURE_ANSWER"] = "1"
        os.environ["SIGIL_CAPTURE_TRACE"] = "1"
        try:
            stdin = StringIO(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "tool_execution_start",
                                "toolName": "read",
                                "args": {"path": str(Path(tmp) / "src/parser.py")},
                            }
                        ),
                        json.dumps({"type": "tool_execution_end", "toolName": "read"}),
                        json.dumps(
                            {
                                "type": "message_update",
                                "assistantMessageEvent": {
                                    "type": "text_delta",
                                    "delta": "I will explain too much.\n\n",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "message_update",
                                "assistantMessageEvent": {
                                    "type": "text_delta",
                                    "delta": "All tests pass.\n\nUpdated parser and tests.",
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
                    stdin=stdin,
                    stdout=stdout,
                    stderr=stderr,
                    compact=True,
                )
                == 0
            )
            assert "read" in stderr.getvalue()
            assert "src/parser.py" in stderr.getvalue()
            assert stdout.getvalue().startswith("done: ")
            assert "All tests pass" in stdout.getvalue()
            assert "I will explain too much" not in stdout.getvalue()
            assert (
                "I will explain too much."
                in read_jsonl("last-question.jsonl")[-1]["content"]
            )
            assert len(read_jsonl("last-tools.jsonl")) == 2
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
