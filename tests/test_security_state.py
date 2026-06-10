from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path

import click
import pytest
from _patch import patch, patch_dict
from click.testing import CliRunner

from sigil.cli import cli, main
from sigil.display.tty import should_color
from sigil.failure import failure_context_prompt, record_failure, truncate_snippet
from sigil.session import (
    read_event_log,
    recent_turns,
    recent_turns_context,
    record_turn,
)
from sigil.state import (
    append_event,
    session_dir,
    session_id,
    state_dir,
)
from sigil.workflows.ask import (
    ASK_SYSTEM_PROMPT,
    ask,
)


class TtyStringIO(StringIO):
    def isatty(self) -> bool:
        return True


def test_question_system_prompt_points_zeta_at_events_log_for_older_history() -> None:
    assert "events.jsonl" in ASK_SYSTEM_PROMPT
    assert "available tools are read, grep, and ls only" in ASK_SYSTEM_PROMPT


def test_top_level_help_lists_commands() -> None:
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "Common workflows:" in result.output
    assert ",      ask from local context" in result.output
    assert ",,     propose one reviewed agent step" in result.output
    assert ",,,    do one auto-approved agent step" in result.output
    assert "+      run one explicit command and capture output" in result.output
    assert "?      status for the current session" in result.output
    assert "named command:" not in result.output
    assert "named shell function:" not in result.output
    assert "Setup and diagnostics:" in result.output
    assert "sigil doctor" in result.output
    assert "sigil status" in result.output
    assert "Commands:" in result.output
    for command in [
        "ask",
        "doctor",
        "events",
        "install",
        "run",
        "session",
        "status",
    ]:
        assert command in result.output
    for command in [
        "command",
        "op",
        "record-turn",
        "record-failure",
        "staged",
    ]:
        assert f"\n  {command} " not in result.output
    assert "\n  question" not in result.output


def test_top_level_without_command_shows_help() -> None:
    result = CliRunner().invoke(cli, [])
    assert result.exit_code == 0
    assert "Common workflows:" in result.output
    assert "Commands:" in result.output


HEAVY_MODULES_PROBE = (
    "heavy = [name for name in sys.modules if name.startswith('sigil.workflows') "
    "or name.startswith('sigil.zeta') or name.startswith('rich')]; "
    "assert not heavy, heavy"
)


def test_cli_import_does_not_load_workflow_modules() -> None:
    script = "import sys; import sigil.cli; " + HEAVY_MODULES_PROBE
    subprocess.run([sys.executable, "-c", script], check=True)


def test_status_dispatch_does_not_load_workflow_modules() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"}
        script = (
            "import sys; from sigil.cli import main; "
            "code = main(['status']); "
            "assert code in (0, 1), code; " + HEAVY_MODULES_PROBE
        )
        subprocess.run(
            [sys.executable, "-c", script],
            env=env,
            check=True,
            stdout=subprocess.DEVNULL,
        )


def test_shell_turn_dispatch_does_not_load_display_or_model() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"}
        script = (
            "import sys; from sigil.cli import main; "
            "code = main(['handoff', 'shell-turn', '--command', 'ls', "
            "'--status', '0', '--cwd', '/tmp']); "
            "assert code == 0, code; "
            "heavy = [name for name in sys.modules "
            "if name.startswith('sigil.display') "
            "or name.startswith('sigil.zeta.agent') "
            "or name.startswith('sigil.zeta.model') "
            "or name.startswith('rich')]; "
            "assert not heavy, heavy"
        )
        subprocess.run(
            [sys.executable, "-c", script],
            env=env,
            check=True,
            stdout=subprocess.DEVNULL,
        )


def test_tty_helpers_do_not_load_display_renderer() -> None:
    script = (
        "import sys; "
        "import sigil.display.tty; "
        "import sigil.zeta.model; "
        "heavy = [name for name in sys.modules "
        "if name == 'sigil.display.render' or name.startswith('rich')]; "
        "assert not heavy, heavy"
    )
    subprocess.run([sys.executable, "-c", script], check=True)


def test_every_lazy_command_resolves() -> None:
    context = click.Context(cli)
    names = cli.list_commands(context)
    assert "ask" in names
    assert "doctor" in names
    for name in names:
        assert cli.get_command(context, name) is not None, name


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


APPEND_LARGE_EVENTS_SCRIPT = """
import os
import sys
import time
from sigil.state import append_event

marker, ready_path, start_path = sys.argv[1:4]
open(ready_path, "w").close()
while not os.path.exists(start_path):
    time.sleep(0.001)
for _ in range(25):
    append_event({"type": "big", "payload": marker * 65536})
"""


def test_append_event_does_not_interleave_large_lines_across_processes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"}
        start_gate = Path(tmp) / "start"
        ready_gates = [Path(tmp) / "ready-a", Path(tmp) / "ready-b"]
        procs = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    APPEND_LARGE_EVENTS_SCRIPT,
                    marker,
                    str(ready),
                    str(start_gate),
                ],
                env=env,
            )
            for marker, ready in zip(("a", "b"), ready_gates, strict=True)
        ]
        deadline = time.monotonic() + 30
        while not all(gate.exists() for gate in ready_gates):
            assert time.monotonic() < deadline
            time.sleep(0.001)
        start_gate.touch()
        for proc in procs:
            assert proc.wait(timeout=60) == 0
        lines = (Path(tmp) / "events.jsonl").read_text(encoding="utf-8").splitlines()

    assert len(lines) == 50
    for line in lines:
        payload = json.loads(line)["payload"]
        assert set(payload) in ({"a"}, {"b"})


def test_append_event_rotates_oversized_log(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            monkeypatch.setattr("sigil.state.EVENT_LOG_MAX_BYTES", 200)
            append_event({"type": "first", "payload": "x" * 300})
            append_event({"type": "second"})

        rotated = Path(tmp) / "events.jsonl.1"
        assert rotated.exists()
        rotated_types = [
            json.loads(line)["type"]
            for line in rotated.read_text(encoding="utf-8").splitlines()
        ]
        assert rotated_types == ["first"]
        current_types = [
            json.loads(line)["type"]
            for line in (Path(tmp) / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        assert current_types == ["second"]


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
                    "type": "failure_recorded",
                    "glyph": ",,",
                    "command": "git status --short",
                    "status": 0,
                }
            )
            text = CliRunner().invoke(cli, ["events", "--limit", "1"])
            listed = CliRunner().invoke(cli, ["events", "--json"])
            raw = CliRunner().invoke(cli, ["events", "--json", "--raw"])
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
        "workflow",
        "event",
        "session",
        "detail",
    ]
    assert str(second["id"])[:8] not in text.output
    assert ",,        failure recorded" in text.output
    assert "git status --short -> 0" in text.output
    assert first["id"] not in text.output
    assert listed.exit_code == 0, listed.output
    summaries = json.loads(listed.output)
    assert [event["type"] for event in summaries] == [
        "first",
        "failure_recorded",
    ]
    assert summaries[-1]["short_id"] == str(second["id"])[:8]
    assert summaries[-1]["workflow"] == ",,"
    assert summaries[-1]["event"] == "failure recorded"
    assert summaries[-1]["detail"] == "git status --short -> 0"
    assert raw.exit_code == 0, raw.output
    assert "short_id" not in json.loads(raw.output)[0]


def test_events_failure_recorded_label_is_not_prefixed_as_glyph() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        old_state_dir = os.environ.get("SIGIL_STATE_DIR")
        old_session_id = os.environ.get("SIGIL_SESSION_ID")
        os.environ["SIGIL_STATE_DIR"] = tmp
        os.environ["SIGIL_SESSION_ID"] = "test"
        try:
            event = append_event(
                {
                    "type": "failure_recorded",
                    "glyph": "failure",
                    "command": "false",
                    "status": 1,
                }
            )
            text = CliRunner().invoke(cli, ["events", "--limit", "1"])
            listed = CliRunner().invoke(cli, ["events", "--json"])
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
    assert str(event["id"])[:8] not in text.output
    assert "failure recorded" in text.output
    assert "failure failure recorded" not in text.output
    assert "false -> 1" in text.output
    assert listed.exit_code == 0, listed.output
    summary = json.loads(listed.output)[0]
    assert summary["workflow"] == "-"
    assert summary["event"] == "failure recorded"
    assert summary["detail"] == "false -> 1"


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


def test_question_workflows_record_glyph_and_local_tools() -> None:
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

            with patch("sigil.workflows.ask.run_tool_ask", side_effect=fake_answer):
                assert ask("what is sigil?", json_output=True) == 0
            request_event = read_event_log()[-1]
            assert request_event["type"] == "ask_requested"
            assert request_event["glyph"] == "ask"
            assert request_event["input"] == "what is sigil?"
            assert "question" not in request_event
            with patch("sigil.workflows.ask.run_tool_ask", side_effect=fake_answer):
                assert (
                    ask(
                        "what is sigil?",
                        glyph=",",
                        tools="read,grep,ls",
                        json_output=True,
                    )
                    == 0
                )
            comma_event = read_event_log()[-1]
            assert comma_event["glyph"] == ","
            assert len(calls) == 2
            assert calls[0][0][0] == ASK_SYSTEM_PROMPT
            assert "available tools are read, grep, and ls only" in calls[0][0][0]
            assert calls[0][1]["allowed_tools"] == ("read", "grep", "ls")
            assert calls[1][0][1] == "what is sigil?"
        finally:
            if old_state_dir is None:
                os.environ.pop("SIGIL_STATE_DIR", None)
            else:
                os.environ["SIGIL_STATE_DIR"] = old_state_dir
            if old_session_id is None:
                os.environ.pop("SIGIL_SESSION_ID", None)
            else:
                os.environ["SIGIL_SESSION_ID"] = old_session_id


def test_question_workflow_requests_tool_calls_on_stdout() -> None:
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

            with patch("sigil.workflows.ask.run_tool_ask", side_effect=fake_answer):
                assert ask("inspect pyproject") == 0

    assert captured_kwargs["input_text"] == "inspect pyproject"


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


def test_record_turn_appends_in_place_under_the_buffer_limit() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            record_turn("cmd-1", 0, "/repo")
            path = Path(tmp) / "sessions" / "test" / "recent-turns.jsonl"
            inode = path.stat().st_ino
            record_turn("cmd-2", 0, "/repo")
            assert path.stat().st_ino == inode

        rows = read_recent_turns(tmp)
        assert [row["command"] for row in rows] == ["cmd-1", "cmd-2"]


RECORD_TURNS_SCRIPT = """
import os
import sys
import time
from sigil.session import record_turn

marker, ready_path, start_path = sys.argv[1:4]
open(ready_path, "w").close()
while not os.path.exists(start_path):
    time.sleep(0.001)
for index in range(10):
    record_turn(f"cmd-{marker}-{index}", 0, "/repo")
"""


def test_record_turn_keeps_all_turns_across_concurrent_processes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"}
        start_gate = Path(tmp) / "start"
        ready_gates = [Path(tmp) / "ready-a", Path(tmp) / "ready-b"]
        procs = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    RECORD_TURNS_SCRIPT,
                    marker,
                    str(ready),
                    str(start_gate),
                ],
                env=env,
            )
            for marker, ready in zip(("a", "b"), ready_gates, strict=True)
        ]
        deadline = time.monotonic() + 30
        while not all(gate.exists() for gate in ready_gates):
            assert time.monotonic() < deadline
            time.sleep(0.001)
        start_gate.touch()
        for proc in procs:
            assert proc.wait(timeout=60) == 0

        rows = read_recent_turns(tmp)
        assert len(rows) == 20


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


def test_record_turn_skips_comma_and_sigil_commands() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            record_turn(", run tests", 0, "/repo")
            record_turn("? what is this", 0, "/repo")
            record_turn("sigil ask hello", 0, "/repo")
            record_turn("__sigil_precmd", 0, "/repo")
        rows = read_recent_turns(tmp)
        assert len(rows) == 1
        assert rows[0]["command"] == "? what is this"


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


def test_record_turn_cli_command_is_not_public_surface() -> None:
    result = CliRunner().invoke(
        cli,
        ["record-turn", "--status", "0", "--cwd", "/repo", "ls -la"],
    )

    assert result.exit_code == 2
    assert "No such command 'record-turn'" in result.output


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


def test_run_cli_shell_mode_captures_raw_command_string() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {
                "SIGIL_RUN_SHELL": "/bin/sh",
                "SIGIL_STATE_DIR": tmp,
                "SIGIL_SESSION_ID": "test",
            },
        ):
            result = CliRunner().invoke(
                cli,
                ["run", "--shell", "printf 'stdout line\\n' | cat"],
            )

        assert result.exit_code == 0
        assert result.stdout == "stdout line\n"
        rows = read_recent_turns(tmp)
        assert rows[-1]["command"] == "printf 'stdout line\\n' | cat"
        assert rows[-1]["status"] == 0
        assert rows[-1]["stdout_snippet"] == "stdout line\n"


def test_run_cli_maps_signal_death_to_shell_exit_code() -> None:
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
                    "import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
                ],
            )

        assert result.exit_code == 143
        rows = read_recent_turns(tmp)
        assert rows[-1]["status"] == 143


class InterruptedProcess:
    """Fake Popen whose first wait raises KeyboardInterrupt, like Ctrl-C."""

    def __init__(self) -> None:
        self.stdout = BytesIO(b"partial output\n")
        self.stderr = BytesIO(b"")
        self.waits = 0

    def wait(self) -> int:
        self.waits += 1
        if self.waits == 1:
            raise KeyboardInterrupt
        return -2


def test_run_cli_records_turn_and_exits_130_on_ctrl_c() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            with patch(
                "sigil.cli.run.start_process",
                return_value=InterruptedProcess(),
            ):
                result = CliRunner().invoke(cli, ["run", "sleep", "100"])

        assert result.exit_code == 130
        rows = read_recent_turns(tmp)
        assert rows[-1]["command"] == "sleep 100"
        assert rows[-1]["status"] == 130
        assert rows[-1]["stdout_snippet"] == "partial output\n"


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

            with patch("sigil.workflows.ask.run_tool_ask", side_effect=fake_answer):
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

            with patch("sigil.workflows.ask.run_tool_ask", side_effect=fake_answer):
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

            with patch("sigil.workflows.ask.run_tool_ask", side_effect=fake_answer):
                assert ask("why failed", json_output=True) == 0

    prompt = captured["prompt"]
    assert "Last failed command context:" not in prompt


def test_fresh_ask_only_includes_shell_activity_since_last_response() -> None:
    from sigil.zeta import timeline as zeta_timeline

    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            record_turn("ls -la", 0, "/repo")
            zeta_timeline.record_event(
                {"type": "assistant_message", "content": "95 files."}
            )
            record_turn("git status --short", 0, "/repo")
            captured: dict[str, str] = {}

            def fake_answer(system: str, prompt: str, **kwargs: object) -> int:
                del system, kwargs
                captured["prompt"] = prompt
                return 0

            with patch("sigil.workflows.ask.run_tool_ask", side_effect=fake_answer):
                assert ask("and now?", json_output=True) == 0

    prompt = captured["prompt"]
    assert "git status --short" in prompt
    assert "ls -la" not in prompt


def test_fresh_ask_omits_failure_context_already_seen_by_the_model() -> None:
    from sigil.zeta import timeline as zeta_timeline

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
            zeta_timeline.record_event(
                {"type": "assistant_message", "content": "The fixture is wrong."}
            )
            captured: dict[str, str] = {}

            def fake_answer(system: str, prompt: str, **kwargs: object) -> int:
                del system, kwargs
                captured["prompt"] = prompt
                return 0

            with patch("sigil.workflows.ask.run_tool_ask", side_effect=fake_answer):
                assert ask("how do I fix it?", json_output=True) == 0

    prompt = captured["prompt"]
    assert "Last failed command context:" not in prompt
    assert "Recent shell activity" not in prompt
    assert prompt == "how do I fix it?"


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

            with patch("sigil.workflows.ask.run_tool_ask", side_effect=fake_answer):
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


def iter_cli_commands(
    group: click.Group,
    context: click.Context,
) -> list[tuple[str, click.Command]]:
    commands = []
    for name in group.list_commands(context):
        command = group.get_command(context, name)
        assert command is not None, name
        commands.append((name, command))
        if isinstance(command, click.Group):
            commands.extend(
                (f"{name} {subname}", subcommand)
                for subname, subcommand in iter_cli_commands(command, context)
            )
    return commands


def test_every_cli_command_and_option_documents_itself() -> None:
    context = click.Context(cli)
    for path, command in iter_cli_commands(cli, context):
        assert command.help or command.short_help, f"{path} has no help text"
        for param in command.params:
            if not isinstance(param, click.Option):
                continue
            assert param.help, f"{path} {param.opts[0]} has no help text"


def test_events_raw_requires_json() -> None:
    result = CliRunner().invoke(cli, ["events", "--raw"])

    assert result.exit_code == 2
    assert "--raw requires --json" in result.output


def test_ask_json_uses_the_shared_indented_shape() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            from sigil.workflows import ask as ask_runner
            from sigil.zeta.agent import AgentTurnResult

            def fake_run_agent_turn(*args: object, **kwargs: object) -> AgentTurnResult:
                del args, kwargs
                return AgentTurnResult(final_text="indented answer")

            with patch("sigil.agent_io.ensure_server", return_value=True):
                with patch(
                    "sigil.workflows.ask.run_agent_turn",
                    side_effect=fake_run_agent_turn,
                ):
                    stdout = StringIO()
                    with redirect_stdout(stdout):
                        code = ask_runner.run_tool_ask(
                            "system", "question", json_output=True
                        )

        assert code == 0
        payload = json.loads(stdout.getvalue())
        assert payload["answer"] == "indented answer"
        assert stdout.getvalue().startswith("{\n")


def test_session_transcript_renders_conversation() -> None:
    from sigil.zeta import timeline as zeta_timeline

    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            zeta_timeline.record_event(
                {"type": "user_message", "content": "what is sigil?"}
            )
            zeta_timeline.record_event(
                {"type": "assistant_message", "content": "A shell assistant."}
            )

            result = CliRunner().invoke(cli, ["session", "transcript"])

    assert result.exit_code == 0
    assert "what is sigil?" in result.output
    assert "A shell assistant." in result.output


def test_session_transcript_limits_and_dumps_json() -> None:
    from sigil.zeta import timeline as zeta_timeline

    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            zeta_timeline.record_event({"type": "user_message", "content": "first"})
            zeta_timeline.record_event(
                {"type": "assistant_message", "content": "second"}
            )

            result = CliRunner().invoke(
                cli, ["session", "transcript", "--limit", "1", "--json"]
            )

    assert result.exit_code == 0
    events = json.loads(result.output)
    assert [event["content"] for event in events] == ["second"]


def test_session_transcript_reports_empty_session() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            result = CliRunner().invoke(cli, ["session", "transcript"])

    assert result.exit_code == 0
    assert "no agent turns recorded" in result.output


def test_session_is_a_group_with_show_as_default() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            help_result = CliRunner().invoke(cli, ["session", "--help"])
            bare = CliRunner().invoke(cli, ["session"])
            explicit = CliRunner().invoke(cli, ["session", "show"])

    assert help_result.exit_code == 0
    assert "Commands:" in help_result.output
    for subcommand in ("show", "path", "list", "clear"):
        assert f"\n  {subcommand} " in help_result.output
    assert bare.exit_code == 0
    assert bare.output.startswith("session test")
    assert explicit.exit_code == 0
    assert explicit.output == bare.output


def test_run_cli_passes_trailing_flags_to_the_command() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch_dict(
            os.environ,
            {"SIGIL_STATE_DIR": tmp, "SIGIL_SESSION_ID": "test"},
        ):
            result = CliRunner().invoke(cli, ["run", "echo", "hello", "--shell"])

        assert result.exit_code == 0
        assert result.stdout == "hello --shell\n"
        rows = read_recent_turns(tmp)
        assert rows[-1]["command"] == "echo hello --shell"


def test_session_dir_with_traversal_id_stays_inside_state_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIGIL_SESSION_ID", "../../escape")
    sessions_root = (state_dir() / "sessions").resolve()
    assert session_dir().resolve().is_relative_to(sessions_root)


def test_safe_session_id_is_used_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIGIL_SESSION_ID", "ttys003-1234")
    assert session_id() == "ttys003-1234"


def test_unsafe_session_id_maps_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIGIL_SESSION_ID", "../../escape")
    first = session_id()
    second = session_id()
    monkeypatch.setenv("SIGIL_SESSION_ID", "../../other")
    other = session_id()
    assert first == second
    assert first != other
    assert "/" not in first
    assert first not in {".", ".."}


def test_session_list_reports_when_no_sessions_exist() -> None:
    text = CliRunner().invoke(cli, ["session", "list"])
    listed = CliRunner().invoke(cli, ["session", "list", "--json"])

    assert text.exit_code == 0
    assert "no sessions recorded" in text.output
    assert listed.exit_code == 0
    assert json.loads(listed.stdout) == []
