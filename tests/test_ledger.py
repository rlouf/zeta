"""Delegation ledger index, append-path, and reindex tests."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from click.testing import CliRunner

from sigil import ledger as sigil_ledger
from sigil.cli import cli as sigil_cli
from sigil.protocols import (
    EFFECT_KIND_COMMAND,
    EFFECT_KIND_FILE_WRITE,
    TURN_OUTCOME_EXECUTED,
    TURN_OUTCOME_FAILED,
    effect_record,
    turn_contract,
    turn_record,
)
from sigil.session import clear_current_session, read_event_log
from sigil.state import append_event, session_dir, state_dir


def sample_turn_record(turn_id: str = "turn-1", **overrides: Any) -> dict[str, Any]:
    record = turn_record(
        turn_id,
        workflow="run",
        objective="ls",
        contract=turn_contract("run", (), staged=False),
        outcome=TURN_OUTCOME_EXECUTED,
    )
    record.update(overrides)
    return record


def sample_effect_record(
    effect_id: str = "effect-1",
    turn_id: str = "turn-1",
    **overrides: Any,
) -> dict[str, Any]:
    record = effect_record(
        effect_id,
        turn_id=turn_id,
        kind=EFFECT_KIND_COMMAND,
        staged=False,
        command="ls",
        exit_status=0,
    )
    record.update(overrides)
    return record


def test_ledger_append_turn_record_writes_log_and_index() -> None:
    payload = sigil_ledger.append_turn_record(sample_turn_record())

    (event,) = read_event_log()
    assert event == payload
    assert sigil_ledger.default_ledger_index().turn("turn-1") == payload


def test_ledger_append_effect_record_writes_log_and_index() -> None:
    payload = sigil_ledger.append_effect_record(sample_effect_record())

    (event,) = read_event_log()
    assert event == payload
    index = sigil_ledger.default_ledger_index()
    assert index.effects_for_turn("turn-1") == [payload]


def test_ledger_index_upserts_converge_on_one_row_per_id() -> None:
    index = sigil_ledger.default_ledger_index()
    first = append_event(sample_turn_record())
    index.index_record(first)
    index.index_record(first)
    replaced = dict(first)
    replaced["outcome"] = TURN_OUTCOME_FAILED
    index.index_record(replaced)

    (row,) = index.turns()
    assert row["outcome"] == TURN_OUTCOME_FAILED


def test_ledger_index_ignores_non_ledger_events() -> None:
    index = sigil_ledger.default_ledger_index()

    assert index.index_record({"type": "user_message", "content": "hi"}) is False
    assert index.turns() == []


def test_ledger_turns_lists_newest_first_and_honors_limit() -> None:
    index = sigil_ledger.default_ledger_index()
    index.index_record(append_event(sample_turn_record("turn-old", time=100.0)))
    index.index_record(append_event(sample_turn_record("turn-new", time=200.0)))

    listed = index.turns()
    assert [row["turn_id"] for row in listed] == ["turn-new", "turn-old"]
    assert [row["turn_id"] for row in index.turns(limit=1)] == ["turn-new"]


def test_ledger_effects_touching_filters_by_exact_path() -> None:
    index = sigil_ledger.default_ledger_index()
    touched = append_event(
        sample_effect_record(
            "effect-1",
            kind=EFFECT_KIND_FILE_WRITE,
            path="a.txt",
        )
    )
    index.index_record(touched)
    index.index_record(
        append_event(
            sample_effect_record(
                "effect-2",
                kind=EFFECT_KIND_FILE_WRITE,
                path="b.txt",
            )
        )
    )

    assert index.effects_touching("a.txt") == [touched]
    assert index.effects_touching("missing.txt") == []


def test_ledger_default_index_is_cached_and_reopens_after_close() -> None:
    first = sigil_ledger.default_ledger_index()

    assert sigil_ledger.default_ledger_index() is first

    sigil_ledger.close_ledger_indexes()
    with pytest.raises(sqlite3.ProgrammingError):
        first.connection.execute("SELECT 1")
    assert sigil_ledger.default_ledger_index() is not first


def test_ledger_append_survives_index_failure(monkeypatch) -> None:
    def broken_index() -> sigil_ledger.LedgerIndex:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(sigil_ledger, "_WARNED_FAILURES", set())
    monkeypatch.setattr(sigil_ledger, "default_ledger_index", broken_index)

    payload = sigil_ledger.append_turn_record(sample_turn_record())

    (event,) = read_event_log()
    assert event == payload


def test_ledger_reindex_reads_both_log_generations() -> None:
    append_event(sample_turn_record("turn-old", time=100.0))
    append_event(sample_effect_record("effect-old", turn_id="turn-old"))
    log_path = state_dir() / "events.jsonl"
    log_path.replace(log_path.with_name("events.jsonl.1"))
    append_event(sample_turn_record("turn-new", time=200.0))
    append_event({"type": "user_message", "content": "not a ledger record"})

    counts = sigil_ledger.reindex()

    assert counts == (2, 1)
    index = sigil_ledger.default_ledger_index()
    assert [row["turn_id"] for row in index.turns()] == ["turn-new", "turn-old"]
    assert index.effects_for_turn("turn-old")[0]["effect_id"] == "effect-old"


def test_ledger_reindex_is_idempotent() -> None:
    append_event(sample_turn_record())
    append_event(sample_effect_record())

    first = sigil_ledger.reindex()
    second = sigil_ledger.reindex()

    assert first == second == (1, 1)
    index = sigil_ledger.default_ledger_index()
    assert len(index.turns()) == 1
    assert len(index.effects_for_turn("turn-1")) == 1


def test_ledger_cli_log_reindex_reports_counts() -> None:
    append_event(sample_turn_record())
    append_event(sample_effect_record())

    result = CliRunner().invoke(sigil_cli, ["log", "reindex"])

    assert result.exit_code == 0
    assert "1 turn record(s)" in result.output
    assert "1 effect record(s)" in result.output
    assert sigil_ledger.default_ledger_index().turn("turn-1") is not None


def test_ledger_survives_session_clear() -> None:
    sigil_ledger.append_turn_record(sample_turn_record())
    root = session_dir()
    root.mkdir(parents=True, exist_ok=True)
    (root / "recent-turns.jsonl").write_text("", encoding="utf-8")

    clear_current_session()

    assert not root.exists()
    assert (state_dir() / "ledger.sqlite3").exists()
    assert (state_dir() / "events.jsonl").exists()
    assert sigil_ledger.default_ledger_index().turn("turn-1") is not None
