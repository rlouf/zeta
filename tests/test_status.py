from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from click.testing import CliRunner

from sigil.cli import cli
from sigil.failure import record_failure
from sigil.staged_command import LAST_STAGED_COMMAND_FILE
from sigil.session import record_turn
from sigil.status import current_status, format_status
from sigil.state import append_event, append_jsonl, write_jsonl


def test_status_clean_when_no_live_state() -> None:
    with isolated_sigil_state():
        status = current_status()

    assert status.state == "clean"
    assert format_status(status) == "clean"


def test_status_reports_active_act_before_other_state() -> None:
    with isolated_sigil_state():
        write_jsonl(
            "last-act.jsonl",
            [
                {
                    "type": "act_created",
                    "act": {
                        "act_id": "act-1",
                        "objective": "fix parser",
                        "status": "active",
                        "steps": [
                            {
                                "id": "1",
                                "title": "Run one Zeta edit step",
                                "command": "zeta --tools read,edit,write",
                                "status": "pending",
                            }
                        ],
                    },
                }
            ],
        )
        status = current_status()

    assert status.state == "attention"
    assert status.reason == "active act"
    assert status.actions == ("sigil act resume", "sigil act abort")
    assert "objective\n  fix parser" in format_status(status)


def test_status_reports_pending_staged_command() -> None:
    with isolated_sigil_state():
        append_jsonl(
            LAST_STAGED_COMMAND_FILE,
            {"event_id": "staged-1", "command": "uv run pytest"},
        )
        status = current_status()

    assert status.reason == "pending staged command"
    assert status.actions == ("sigil staged pop",)
    assert "uv run pytest" in format_status(status)


def test_status_reports_last_failure() -> None:
    with isolated_sigil_state():
        record_turn("uv run pytest", 1, os.getcwd(), stderr_snippet="failed")
        status = current_status()

    assert status.reason == "last command failed"
    assert status.actions == (", suggest a fix",)
    assert "uv run pytest" in format_status(status)


def test_status_ignores_stale_failure_after_successful_turn() -> None:
    with isolated_sigil_state():
        record_turn("uv run pytest", 1, os.getcwd(), stderr_snippet="failed")
        record_turn("git status --short", 0, os.getcwd())
        status = current_status()

    assert status.state == "clean"


def test_status_reports_failed_sigil_execution() -> None:
    with isolated_sigil_state(session_id="status-session"):
        append_event(
            {
                "type": "operator_command_executed",
                "operator": {"glyph": ",,"},
                "status": 2,
                "command": "uv run pytest",
            }
        )
        status = current_status()

    assert status.reason == "last Sigil action failed"
    assert status.actions == ("sigil events",)


def test_status_cli_human_and_json() -> None:
    with isolated_sigil_state():
        clean = CliRunner().invoke(cli, ["status"])
        record_failure("uv run pytest", 1, os.getcwd())
        attention = CliRunner().invoke(cli, ["status"])
        as_json = CliRunner().invoke(cli, ["status", "--json"])

    assert clean.exit_code == 0
    assert clean.output == "clean\n"
    assert attention.exit_code == 1
    assert "attention: last command failed" in attention.output
    assert as_json.exit_code == 1
    payload = json.loads(as_json.output)
    assert payload["state"] == "attention"
    assert payload["reason"] == "last command failed"


class isolated_sigil_state:
    def __init__(self, session_id: str = "status-test") -> None:
        self.session_id = session_id
        self.tmp: tempfile.TemporaryDirectory[str] | None = None
        self.old_state_dir: str | None = None
        self.old_session_id: str | None = None

    def __enter__(self) -> Path:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_state_dir = os.environ.get("SIGIL_STATE_DIR")
        self.old_session_id = os.environ.get("SIGIL_SESSION_ID")
        os.environ["SIGIL_STATE_DIR"] = self.tmp.name
        os.environ["SIGIL_SESSION_ID"] = self.session_id
        return Path(self.tmp.name)

    def __exit__(self, *args: object) -> None:
        if self.old_state_dir is None:
            os.environ.pop("SIGIL_STATE_DIR", None)
        else:
            os.environ["SIGIL_STATE_DIR"] = self.old_state_dir
        if self.old_session_id is None:
            os.environ.pop("SIGIL_SESSION_ID", None)
        else:
            os.environ["SIGIL_SESSION_ID"] = self.old_session_id
        if self.tmp is not None:
            self.tmp.cleanup()
