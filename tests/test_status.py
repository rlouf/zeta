from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from _zeta_helpers import write_models_config
from click.testing import CliRunner

from sigil.cli import cli
from sigil.ledger import ledger_index
from sigil.protocols import effect_record, turn_contract, turn_record
from sigil.session import record_turn
from sigil.state import append_event
from sigil.status import current_status, format_status
from zeta.models import set_active_model_profile


def test_status_clean_when_no_live_state() -> None:
    status = current_status()

    assert status.state == "clean"
    assert format_status(status).splitlines()[0] == "clean"


def test_status_clean_shows_model_line(monkeypatch: pytest.MonkeyPatch) -> None:

    status = current_status()

    assert status.model == {
        "profile": "default",
        "model": "local-model",
        "url": "http://127.0.0.1:8080/v1/chat/completions",
        "source": "builtin",
    }
    assert format_status(status) == (
        "clean\n"
        "model: default -> local-model @ "
        "http://127.0.0.1:8080/v1/chat/completions (builtin)"
    )


def test_status_model_line_reports_session_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    write_models_config(
        home,
        """
[[models]]
name = "fast"
model = "fast-model"
url = "http://127.0.0.1:8081/v1/chat/completions"
""",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SIGIL_SESSION_ID", "status-model")
    set_active_model_profile("fast")

    status = current_status()

    assert status.model["profile"] == "fast"
    assert status.model["source"] == "session"
    assert "stale_profile" not in status.model
    assert (
        "model: fast -> fast-model @ http://127.0.0.1:8081/v1/chat/completions "
        "(session)" in format_status(status)
    )


def test_status_model_line_reports_stale_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    write_models_config(home, "")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SIGIL_SESSION_ID", "status-stale-model")
    set_active_model_profile("gone")

    status = current_status()

    assert status.model["source"] == "builtin"
    assert status.model["stale_profile"] == "gone"
    assert "(builtin; profile 'gone' missing from models.toml)" in format_status(status)


def test_status_reports_last_failure() -> None:
    record_turn("uv run pytest", 1, os.getcwd(), stderr_snippet="failed")
    status = current_status()

    assert status.reason == "last command failed"
    assert status.actions == (", suggest a fix",)
    assert "uv run pytest" in format_status(status)


def test_status_attention_keeps_model_line() -> None:
    record_turn("uv run pytest", 1, os.getcwd(), stderr_snippet="failed")
    status = current_status()
    rendered = format_status(status)

    assert rendered.startswith("attention:")
    assert "\nmodel: " in rendered


def test_status_ignores_stale_failure_after_successful_turn() -> None:
    record_turn("uv run pytest", 1, os.getcwd(), stderr_snippet="failed")
    record_turn("git status --short", 0, os.getcwd())
    status = current_status()

    assert status.state == "clean"


def test_status_cli_is_public_surface() -> None:
    result = CliRunner().invoke(cli, ["status"])

    assert result.exit_code == 0
    assert result.output.splitlines()[0] == "clean"
    assert "model: " in result.output


def test_status_reports_last_delegation_and_today_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIGIL_SESSION_ID", "status-ledger")
    index = ledger_index()
    index.index_event(
        append_event(
            turn_record(
                "turn-do-1111",
                workflow="do",
                objective="refactor the staging path",
                contract=turn_contract("do", ("edit",), staged=False),
                outcome="executed",
                cost={"input_tokens": 1000, "output_tokens": 200, "model_calls": 3},
            )
        )
    )
    index.index_event(
        append_event(
            turn_record(
                "turn-run-2222",
                workflow="run",
                objective="ls",
                contract=turn_contract("run", (), staged=False),
                outcome="executed",
            )
        )
    )

    status = current_status()
    rendered = format_status(status)

    assert status.last_turn is not None
    assert status.last_turn["workflow"] == "do"
    assert status.today["turns"] == 2
    assert "last: do · executed · refactor the staging path" in rendered
    assert "today: 1200 tok · 3 calls · 2 turns" in rendered


def test_status_reports_pending_staged_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIGIL_SESSION_ID", "status-pending")
    from sigil.ledger import append_effect_record

    append_effect_record(
        effect_record(
            "effect-staged",
            turn_id="turn-1",
            kind="command",
            staged=True,
            command="uv run pytest",
            tool_call_id="call-1",
        )
    )

    status = current_status()

    assert status.pending is not None
    assert status.pending["command"] == "uv run pytest"
    assert "staged: uv run pytest (pending)" in format_status(status)

    result = CliRunner().invoke(cli, ["status"])
    assert result.exit_code == 0


def test_status_omits_ledger_lines_for_quiet_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIGIL_SESSION_ID", "status-quiet")

    rendered = format_status(current_status())

    assert "last:" not in rendered
    assert "staged:" not in rendered
    assert "today:" not in rendered


def test_status_json_carries_ledger_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIGIL_SESSION_ID", "status-json")
    index = ledger_index()
    index.index_event(
        append_event(
            turn_record(
                "turn-ask-1",
                workflow="ask",
                objective="why?",
                contract=turn_contract("ask", (), staged=False),
                outcome="answered",
                cost={"input_tokens": 10, "output_tokens": 5, "model_calls": 1},
            )
        )
    )

    result = CliRunner().invoke(cli, ["status", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["last_turn"]["turn_id"] == "turn-ask-1"
    assert payload["pending"] is None
    assert payload["today"]["turns"] == 1


def test_status_cli_json_includes_model(monkeypatch: pytest.MonkeyPatch) -> None:

    result = CliRunner().invoke(cli, ["status", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["model"] == {
        "profile": "default",
        "model": "local-model",
        "url": "http://127.0.0.1:8080/v1/chat/completions",
        "source": "builtin",
    }
