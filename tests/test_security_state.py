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
from sigil.staged_command import (
    LAST_STAGED_COMMAND_FILE,
    PENDING_STAGED_COMMANDS_FILE,
    consume_latest_staged_command,
    prepare_staged_commands,
    record_staged_commands,
)
from sigil.zeta.stream import renderer_command, should_color, stream_events
from sigil.question import (
    QUESTION_SYSTEM_PROMPT,
    ask,
    continuation_prompt,
    discussion_turns,
)
from sigil.session import recent_turns, recent_turns_context, record_turn
from sigil.state import append_event, read_jsonl, write_jsonl
from sigil.tty import (
    clear_lines_on_tty,
    confirmation_tty_paths,
    confirm_on_tty,
)


class TtyStringIO(StringIO):
    def isatty(self) -> bool:
        return True


def test_question_system_prompt_points_zeta_at_events_log_for_older_history() -> None:
    assert "events.jsonl" in QUESTION_SYSTEM_PROMPT
    assert "available tools are read and grep only" in QUESTION_SYSTEM_PROMPT


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
        "run",
        "session",
        "status",
    ]:
        assert command in result.output
    assert "\n  question" not in result.output


def test_main_rewrites_missing_executable_errors() -> None:
    stderr = StringIO()
    missing = FileNotFoundError(2, "No such file or directory", "zeta")
    with patch("sigil.cli.cli.main", side_effect=missing):
        with redirect_stderr(stderr):
            assert main(["ask", "hello"]) == 127
    assert "missing executable: zeta" in stderr.getvalue()


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
        "session",
        "summary",
    ]
    assert str(second["id"])[:8] in text.output
    assert ",, executed" in text.output
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
    assert raw.exit_code == 0, raw.output
    assert "short_id" not in json.loads(raw.output)[0]


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


def test_clear_lines_writes_erase_sequence_to_exported_tty_fd() -> None:
    master_fd, slave_fd = os.openpty()
    try:
        with patch_dict(os.environ, {"SIGIL_TTY_FD": str(slave_fd)}, clear=True):
            clear_lines_on_tty(3)
        assert os.read(master_fd, 1024) == b"\033[3A\r\033[J"
    finally:
        os.close(master_fd)
        os.close(slave_fd)


def test_clear_lines_is_a_noop_for_nonpositive_counts() -> None:
    master_fd, slave_fd = os.openpty()
    try:
        with patch_dict(os.environ, {"SIGIL_TTY_FD": str(slave_fd)}, clear=True):
            clear_lines_on_tty(0)
        os.set_blocking(master_fd, False)
        with pytest.raises(BlockingIOError):
            os.read(master_fd, 1024)
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
    with patch("sigil.zeta.stream.shutil.which", return_value="/opt/homebrew/bin/glow"):
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
    with patch("sigil.zeta.stream.shutil.which", return_value="/opt/homebrew/bin/glow"):
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
    with patch("sigil.zeta.stream.shutil.which", return_value=None):
        assert renderer_command() == ["cat"]


def test_question_routes_record_glyph_and_web_tools() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        old_state_dir = os.environ.get("SIGIL_STATE_DIR")
        old_session_id = os.environ.get("SIGIL_SESSION_ID")
        os.environ["SIGIL_STATE_DIR"] = tmp
        os.environ["SIGIL_SESSION_ID"] = "test"
        try:
            calls = []

            def fake_answer(*args: object, **kwargs: object) -> int:
                calls.append((args, kwargs))
                return 0

            with patch("sigil.question.run_question_answer", side_effect=fake_answer):
                assert ask("what is sigil?", json_output=True) == 0
            fresh_turn = read_jsonl("last-question.jsonl")[0]
            assert fresh_turn["glyph"] == "?"
            with patch("sigil.question.run_question_answer", side_effect=fake_answer):
                assert (
                    ask(
                        "what is sigil on the web?",
                        glyph="??",
                        tools="read,grep",
                        use_web=True,
                        json_output=True,
                    )
                    == 0
                )
            web_turn = read_jsonl("last-question.jsonl")[-1]
            assert web_turn["glyph"] == "??"
            assert len(calls) == 2
            assert calls[0][0][0] == QUESTION_SYSTEM_PROMPT
            assert "available tools are read and grep only" in calls[0][0][0]
            assert calls[0][1]["allowed_tools"] == ("read", "grep")
            assert "no web_search tool" in calls[1][0][1]
        finally:
            if old_state_dir is None:
                os.environ.pop("SIGIL_STATE_DIR", None)
            else:
                os.environ["SIGIL_STATE_DIR"] = old_state_dir
            if old_session_id is None:
                os.environ.pop("SIGIL_SESSION_ID", None)
            else:
                os.environ["SIGIL_SESSION_ID"] = old_session_id


def test_question_route_requests_tool_calls_on_stdout() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            captured_kwargs: dict[str, object] = {}

            def fake_answer(*args: object, **kwargs: object) -> int:
                del args
                captured_kwargs.update(kwargs)
                return 0

            with patch("sigil.question.run_question_answer", side_effect=fake_answer):
                assert ask("inspect pyproject") == 0

    assert captured_kwargs["question"] == "inspect pyproject"


def test_zeta_stream_can_render_tool_calls_to_stdout() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            stdin = StringIO(
                json.dumps(
                    {
                        "type": "message_update",
                        "assistantMessageEvent": {
                            "type": "toolcall_end",
                            "toolCall": {
                                "id": "call-1",
                                "name": "read",
                                "arguments": {"path": "pyproject.toml"},
                            },
                        },
                    }
                )
                + "\n"
            )
            stdout = StringIO()
            stderr = StringIO()
            with patch("sigil.zeta.stream.open_terminal_output", return_value=None):
                assert (
                    stream_events(
                        stdin=stdin,
                        stdout=stdout,
                        stderr=stderr,
                        capture_trace=True,
                        tool_output_stdout=True,
                    )
                    == 0
                )

    assert "❯ read   pyproject.toml" in stdout.getvalue()
    assert "❯ read" not in stderr.getvalue()


def test_zeta_stream_can_render_tool_calls_to_terminal_when_stdout_is_redirected() -> (
    None
):
    class NonClosingTtyStringIO(StringIO):
        def close(self) -> None:
            pass

        def isatty(self) -> bool:
            return True

    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            stdin = StringIO(
                json.dumps(
                    {
                        "type": "message_update",
                        "assistantMessageEvent": {
                            "type": "toolcall_end",
                            "toolCall": {
                                "id": "call-1",
                                "name": "read",
                                "arguments": {"path": "pyproject.toml"},
                            },
                        },
                    }
                )
                + "\n"
            )
            stdout = StringIO()
            stderr = StringIO()
            terminal = NonClosingTtyStringIO()
            with patch("sigil.zeta.stream.open_terminal_output", return_value=terminal):
                assert (
                    stream_events(
                        stdin=stdin,
                        stdout=stdout,
                        stderr=stderr,
                        capture_trace=True,
                        tool_output_stdout=True,
                    )
                    == 0
                )

    assert "❯ read   pyproject.toml" in terminal.getvalue()
    assert "❯ read" not in stdout.getvalue()
    assert "❯ read" not in stderr.getvalue()


def test_staged_command_records_and_consumes_blocked_command() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            pending = prepare_staged_commands()
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
            records = record_staged_commands(glyph="?")

            assert records[0]["command"] == "git diff --stat"
            assert records[0]["glyph"] == "?"
            assert not (
                Path(tmp) / "sessions/test" / PENDING_STAGED_COMMANDS_FILE
            ).exists()

            consumed = consume_latest_staged_command()
            assert consumed is not None
            assert consumed["command"] == "git diff --stat"
            assert read_jsonl(LAST_STAGED_COMMAND_FILE) == []


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


def test_record_turn_appends_command_with_glyph() -> None:
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


def test_run_cli_streams_output_and_records_snippets() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            result = CliRunner().invoke(
                cli,
                [
                    "run",
                    "python",
                    "-c",
                    (
                        "import sys; "
                        "print('stdout line'); "
                        "print('stderr line', file=sys.stderr); "
                        "sys.exit(7)"
                    ),
                ],
            )

        assert result.exit_code == 7
        assert result.stdout == "stdout line\n"
        assert result.stderr == "stderr line\n"
        rows = read_recent_turns(tmp)
        command = rows[-1]["command"]
        assert isinstance(command, str)
        assert command.startswith("python -c ")
        assert rows[-1]["status"] == 7
        assert rows[-1]["stdout_snippet"] == "stdout line\n"
        assert rows[-1]["stderr_snippet"] == "stderr line\n"
        failure = json.loads(
            (Path(tmp) / "sessions" / "test" / "last-failure.json").read_text(
                encoding="utf-8"
            )
        )
        assert failure["stdout_snippet"] == "stdout line\n"
        assert failure["stderr_snippet"] == "stderr line\n"


def test_run_cli_requires_a_command() -> None:
    result = CliRunner().invoke(cli, ["run"])
    assert result.exit_code == 2
    assert "missing command to run" in result.output


def test_run_cli_records_missing_executable() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            result = CliRunner().invoke(cli, ["run", "definitely-not-a-command"])

        assert result.exit_code == 127
        assert "missing executable: definitely-not-a-command" in result.stderr
        rows = read_recent_turns(tmp)
        assert rows[-1]["command"] == "definitely-not-a-command"
        assert rows[-1]["status"] == 127
        assert "missing executable" in str(rows[-1]["stderr_snippet"])


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


def test_fresh_ask_prepends_recent_turns_context_to_zeta_prompt() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            record_turn("ls -la", 0, "/repo")
            record_turn("pytest tests/test_foo.py", 1, "/repo")
            captured: dict[str, str] = {}

            def fake_answer(system: str, prompt: str, **kwargs: object) -> int:
                del system, kwargs
                captured["prompt"] = prompt
                return 0

            with patch("sigil.question.run_question_answer", side_effect=fake_answer):
                assert ask("what should I do next?", json_output=True) == 0

    prompt = captured["prompt"]
    assert "Recent shell activity:" in prompt
    assert "ls -la" in prompt
    assert "pytest tests/test_foo.py" in prompt
    assert "what should I do next?" in prompt


def test_ask_attaches_active_failure_context_for_unrelated_question() -> None:
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
            captured: dict[str, str] = {}

            def fake_answer(system: str, prompt: str, **kwargs: object) -> int:
                del system, kwargs
                captured["prompt"] = prompt
                return 0

            with patch("sigil.question.run_question_answer", side_effect=fake_answer):
                assert ask("what does this repo do", json_output=True) == 0

    prompt = captured["prompt"]
    assert "Last failed command context:" in prompt
    assert "Failed command: pytest tests/test_foo.py" in prompt
    assert "Recent stderr:" in prompt
    assert "AssertionError: no" in prompt


def test_ask_omits_failure_context_after_successful_turn() -> None:
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
            captured: dict[str, str] = {}

            def fake_answer(system: str, prompt: str, **kwargs: object) -> int:
                del system, kwargs
                captured["prompt"] = prompt
                return 0

            with patch("sigil.question.run_question_answer", side_effect=fake_answer):
                assert ask("why failed", json_output=True) == 0

    prompt = captured["prompt"]
    assert "Last failed command context:" not in prompt


def test_explicit_follow_up_ask_does_not_include_recent_turns_context() -> None:
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
            captured: dict[str, str] = {}

            def fake_answer(system: str, prompt: str, **kwargs: object) -> int:
                del system, kwargs
                captured["prompt"] = prompt
                return 0

            with patch("sigil.question.run_question_answer", side_effect=fake_answer):
                assert (
                    ask(
                        continuation_prompt("follow up", discussion_turns()),
                        glyph="??",
                        tools="read,grep",
                        use_web=True,
                        append_transcript=True,
                        json_output=True,
                    )
                    == 0
                )

    prompt = captured["prompt"]
    assert "Recent shell activity" not in prompt


def test_fresh_ask_omits_recent_turns_section_when_none_recorded() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            captured: dict[str, str] = {}

            def fake_answer(system: str, prompt: str, **kwargs: object) -> int:
                del system, kwargs
                captured["prompt"] = prompt
                return 0

            with patch("sigil.question.run_question_answer", side_effect=fake_answer):
                assert ask("hello", json_output=True) == 0

    prompt = captured["prompt"]
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


def test_zeta_stream_records_answer_turn() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        saved = {
            key: os.environ.get(key) for key in ["SIGIL_STATE_DIR", "SIGIL_SESSION_ID"]
        }
        os.environ["SIGIL_STATE_DIR"] = tmp
        os.environ["SIGIL_SESSION_ID"] = "test"
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
            assert (
                stream_events(
                    stdin=stdin,
                    stdout=StringIO(),
                    stderr=StringIO(),
                    capture_answer=True,
                )
                == 0
            )
            answer = read_jsonl("last-question.jsonl")[0]
            assert answer["content"] == "answer"
            assert answer["event_id"]
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def test_zeta_stream_json_output_is_machine_readable() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        saved = {
            key: os.environ.get(key) for key in ["SIGIL_STATE_DIR", "SIGIL_SESSION_ID"]
        }
        os.environ["SIGIL_STATE_DIR"] = tmp
        os.environ["SIGIL_SESSION_ID"] = "test"
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
                    stdin=stdin,
                    stdout=stdout,
                    stderr=stderr,
                    question="what is sigil?",
                    prompt="what is sigil?",
                    capture_answer=True,
                    capture_trace=True,
                    json_output=True,
                )
                == 0
            )
            payload = json.loads(stdout.getvalue())
            assert payload["ok"]
            assert payload["type"] == "answer"
            assert payload["question"] == "what is sigil?"
            assert payload["answer"] == "answer"
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


def test_zeta_stream_json_output_counts_malformed_events() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        saved = {
            key: os.environ.get(key) for key in ["SIGIL_STATE_DIR", "SIGIL_SESSION_ID"]
        }
        os.environ["SIGIL_STATE_DIR"] = tmp
        os.environ["SIGIL_SESSION_ID"] = "test"
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
                    stdin=stdin,
                    stdout=stdout,
                    stderr=StringIO(),
                    question="question",
                    prompt="question",
                    json_output=True,
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


def test_zeta_stream_non_tty_status_has_no_control_codes_or_color() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        saved = {
            key: os.environ.get(key) for key in ["SIGIL_STATE_DIR", "SIGIL_SESSION_ID"]
        }
        os.environ["SIGIL_STATE_DIR"] = tmp
        os.environ["SIGIL_SESSION_ID"] = "test"
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
            assert (
                stream_events(
                    stdin=stdin,
                    stdout=StringIO(),
                    stderr=stderr,
                    capture_trace=True,
                )
                == 0
            )
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


def test_zeta_stream_shows_function_call_events() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        saved = {
            key: os.environ.get(key) for key in ["SIGIL_STATE_DIR", "SIGIL_SESSION_ID"]
        }
        os.environ["SIGIL_STATE_DIR"] = tmp
        os.environ["SIGIL_SESSION_ID"] = "test"
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
            assert (
                stream_events(
                    stdin=stdin,
                    stdout=StringIO(),
                    stderr=stderr,
                    capture_trace=True,
                )
                == 0
            )
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


def test_zeta_stream_shows_nested_tool_call_updates() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        saved = {
            key: os.environ.get(key) for key in ["SIGIL_STATE_DIR", "SIGIL_SESSION_ID"]
        }
        os.environ["SIGIL_STATE_DIR"] = tmp
        os.environ["SIGIL_SESSION_ID"] = "test"
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
            assert (
                stream_events(
                    stdin=stdin,
                    stdout=StringIO(),
                    stderr=stderr,
                    capture_trace=True,
                )
                == 0
            )

            assert "read" in stderr.getvalue()
            assert "src/sigil/question.py" in stderr.getvalue()
            assert read_jsonl("last-tools.jsonl")[0]["tool"] == "read"
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def test_zeta_stream_shows_zeta_toolcall_end_updates() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        saved = {
            key: os.environ.get(key) for key in ["SIGIL_STATE_DIR", "SIGIL_SESSION_ID"]
        }
        os.environ["SIGIL_STATE_DIR"] = tmp
        os.environ["SIGIL_SESSION_ID"] = "test"
        try:
            stdin = StringIO(
                json.dumps(
                    {
                        "type": "message_update",
                        "assistantMessageEvent": {
                            "type": "toolcall_end",
                            "toolCall": {
                                "id": "call-1",
                                "name": "bash",
                                "arguments": {"command": "uv run pytest"},
                            },
                        },
                    }
                )
                + "\n"
            )
            stderr = StringIO()
            assert (
                stream_events(
                    stdin=stdin,
                    stdout=StringIO(),
                    stderr=stderr,
                    capture_trace=True,
                )
                == 0
            )

            assert "bash" in stderr.getvalue()
            assert "uv run pytest" in stderr.getvalue()
            tools = read_jsonl("last-tools.jsonl")
            assert tools[0]["tool"] == "bash"
            assert tools[0]["tool_call_id"] == "call-1"
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def test_zeta_stream_ignores_partial_toolcall_delta_until_complete() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        saved = {
            key: os.environ.get(key) for key in ["SIGIL_STATE_DIR", "SIGIL_SESSION_ID"]
        }
        os.environ["SIGIL_STATE_DIR"] = tmp
        os.environ["SIGIL_SESSION_ID"] = "test"
        try:
            stdin = StringIO(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "message_update",
                                "assistantMessageEvent": {
                                    "type": "toolcall_delta",
                                    "contentIndex": 0,
                                    "partial": {
                                        "role": "assistant",
                                        "content": [
                                            {
                                                "type": "toolCall",
                                                "id": "call-1",
                                                "name": "read",
                                                "arguments": {"path": "/Users"},
                                            }
                                        ],
                                    },
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "message_update",
                                "assistantMessageEvent": {
                                    "type": "toolcall_end",
                                    "contentIndex": 0,
                                    "toolCall": {
                                        "type": "toolCall",
                                        "id": "call-1",
                                        "name": "read",
                                        "arguments": {
                                            "path": "/Users/remilouf/projects/sigil/pyproject.toml"
                                        },
                                    },
                                },
                            }
                        ),
                    ]
                )
                + "\n"
            )
            stderr = StringIO()
            assert (
                stream_events(
                    stdin=stdin,
                    stdout=StringIO(),
                    stderr=stderr,
                    capture_trace=True,
                )
                == 0
            )

            assert "read" in stderr.getvalue()
            assert "pyproject.toml" in stderr.getvalue()
            assert "❯ read  /Users\n" not in stderr.getvalue()
            tools = read_jsonl("last-tools.jsonl")
            assert tools[0]["tool"] == "read"
            assert tools[0]["tool_call_id"] == "call-1"
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def test_zeta_stream_ignores_toolcall_start_until_complete() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        saved = {
            key: os.environ.get(key) for key in ["SIGIL_STATE_DIR", "SIGIL_SESSION_ID"]
        }
        os.environ["SIGIL_STATE_DIR"] = tmp
        os.environ["SIGIL_SESSION_ID"] = "test"
        try:
            stdin = StringIO(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "message_update",
                                "assistantMessageEvent": {
                                    "type": "toolcall_start",
                                    "contentIndex": 0,
                                    "partial": {
                                        "role": "assistant",
                                        "content": [
                                            {
                                                "type": "toolCall",
                                                "id": "call-1",
                                                "name": "read",
                                                "arguments": {},
                                            }
                                        ],
                                    },
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "message_update",
                                "assistantMessageEvent": {
                                    "type": "toolcall_delta",
                                    "contentIndex": 0,
                                    "partial": {
                                        "role": "assistant",
                                        "content": [
                                            {
                                                "type": "toolCall",
                                                "id": "call-1",
                                                "name": "read",
                                                "arguments": {"path": "pyproject.toml"},
                                            }
                                        ],
                                    },
                                },
                            }
                        ),
                    ]
                )
                + "\n"
            )
            stderr = StringIO()
            assert (
                stream_events(
                    stdin=stdin,
                    stdout=StringIO(),
                    stderr=stderr,
                    capture_trace=True,
                )
                == 0
            )

            assert stderr.getvalue().count("read") == 0
            assert "pyproject.toml" not in stderr.getvalue()
            assert read_jsonl("last-tools.jsonl") == []
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def test_zeta_stream_uses_execution_start_when_toolcall_end_is_missing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        saved = {
            key: os.environ.get(key) for key in ["SIGIL_STATE_DIR", "SIGIL_SESSION_ID"]
        }
        os.environ["SIGIL_STATE_DIR"] = tmp
        os.environ["SIGIL_SESSION_ID"] = "test"
        try:
            stdin = StringIO(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "message_update",
                                "assistantMessageEvent": {
                                    "type": "toolcall_delta",
                                    "contentIndex": 0,
                                    "partial": {
                                        "role": "assistant",
                                        "content": [
                                            {
                                                "type": "toolCall",
                                                "id": "call-1",
                                                "name": "read",
                                                "arguments": {"path": "/Users"},
                                            }
                                        ],
                                    },
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "tool_execution_start",
                                "toolCallId": "call-1",
                                "toolName": "read",
                                "args": {"path": "pyproject.toml"},
                            }
                        ),
                    ]
                )
                + "\n"
            )
            stderr = StringIO()
            assert (
                stream_events(
                    stdin=stdin,
                    stdout=StringIO(),
                    stderr=stderr,
                    capture_trace=True,
                )
                == 0
            )

            assert stderr.getvalue().count("read") == 1
            assert "❯ read   pyproject.toml" in stderr.getvalue()
            assert "pyproject.toml" in stderr.getvalue()
            tools = read_jsonl("last-tools.jsonl")
            assert [tool["detail"] for tool in tools] == ["pyproject.toml"]
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def test_zeta_stream_deduplicates_toolcall_end_and_execution_start() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        saved = {
            key: os.environ.get(key) for key in ["SIGIL_STATE_DIR", "SIGIL_SESSION_ID"]
        }
        os.environ["SIGIL_STATE_DIR"] = tmp
        os.environ["SIGIL_SESSION_ID"] = "test"
        try:
            stdin = StringIO(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "message_update",
                                "assistantMessageEvent": {
                                    "type": "toolcall_end",
                                    "toolCall": {
                                        "id": "call-1",
                                        "name": "read",
                                        "arguments": {
                                            "path": "src/sigil/zeta/stream.py"
                                        },
                                    },
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "tool_execution_start",
                                "toolCallId": "call-1",
                                "toolName": "read",
                                "args": {"path": "src/sigil/zeta/stream.py"},
                            }
                        ),
                    ]
                )
                + "\n"
            )
            stderr = StringIO()
            assert (
                stream_events(
                    stdin=stdin,
                    stdout=StringIO(),
                    stderr=stderr,
                    capture_trace=True,
                )
                == 0
            )

            assert stderr.getvalue().count("read") == 1
            tools = read_jsonl("last-tools.jsonl")
            assert [tool["type"] for tool in tools] == ["tool_start"]
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def test_zeta_stream_shows_tool_start_without_detail() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        saved = {
            key: os.environ.get(key) for key in ["SIGIL_STATE_DIR", "SIGIL_SESSION_ID"]
        }
        os.environ["SIGIL_STATE_DIR"] = tmp
        os.environ["SIGIL_SESSION_ID"] = "test"
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
            assert (
                stream_events(
                    stdin=stdin,
                    stdout=StringIO(),
                    stderr=stderr,
                    capture_trace=True,
                )
                == 0
            )

            assert "❯ read" in stderr.getvalue()
            assert read_jsonl("last-tools.jsonl")[0]["tool"] == "read"
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def test_zeta_stream_compact_mode_suppresses_prose_and_summarizes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        saved = {
            key: os.environ.get(key) for key in ["SIGIL_STATE_DIR", "SIGIL_SESSION_ID"]
        }
        os.environ["SIGIL_STATE_DIR"] = tmp
        os.environ["SIGIL_SESSION_ID"] = "test"
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
                    capture_answer=True,
                    capture_trace=True,
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
