"""Delegation ledger index, append-path, and reindex tests."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest
from click.testing import CliRunner

from sigil import ledger as sigil_ledger
from sigil.cli import cli as sigil_cli
from sigil.protocols import (
    EFFECT_KIND_COMMAND,
    EFFECT_KIND_FILE_WRITE,
    TURN_OUTCOME_ABORTED,
    TURN_OUTCOME_EXECUTED,
    TURN_OUTCOME_FAILED,
    effect_record,
    turn_contract,
    turn_record,
)
from sigil.session import clear_current_session, read_events
from sigil.state import (
    append_event,
    session_dir,
    sigil_event_store,
    state_dir,
    trace_store_path,
)
from zeta.events import DraftEvent, Event, publish_event


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


def append_indexed(record: dict[str, Any]) -> dict[str, Any]:
    if record.get("type") == "sigil.effect":
        return sigil_ledger.append_effect_record(record)
    event = append_event(record)
    sigil_ledger.ledger_index().index_event(event)
    return sigil_ledger.ledger_event_record(event)


def publish_sigil_draft(draft: DraftEvent) -> None:
    store = sigil_event_store()
    try:
        publish_event(draft, sink=store)
    finally:
        store.close()


def test_ledger_append_turn_record_writes_log_and_index() -> None:
    event = sigil_ledger.append_turn_record(
        sample_turn_record(caused_by="prompt-event")
    )
    payload = sigil_ledger.ledger_event_record(event)

    (stored_event,) = read_events()
    assert event.event_type == "sigil.turn.completed"
    assert event.caused_by == "prompt-event"
    assert payload["caused_by"] == "prompt-event"
    assert stored_event == event
    assert sigil_ledger.ledger_index().turn("turn-1") == payload


def test_ledger_append_turn_record_uses_outcome_event_names() -> None:
    failed = sigil_ledger.append_turn_record(
        sample_turn_record("turn-failed", outcome=TURN_OUTCOME_FAILED)
    )
    aborted = sigil_ledger.append_turn_record(
        sample_turn_record("turn-aborted", outcome=TURN_OUTCOME_ABORTED)
    )

    assert failed.event_type == "sigil.turn.failed"
    assert aborted.event_type == "sigil.turn.aborted"


def test_ledger_append_effect_record_writes_projection_only() -> None:
    payload = sigil_ledger.append_effect_record(sample_effect_record())

    assert read_events() == []
    index = sigil_ledger.ledger_index()
    assert index.effects_for_turn("turn-1") == [payload]


def test_ledger_index_upserts_converge_on_one_row_per_id() -> None:
    index = sigil_ledger.ledger_index()
    first = append_event(sample_turn_record())
    index.index_event(first)
    index.index_event(first)
    replaced = append_event(
        sample_turn_record(
            outcome=TURN_OUTCOME_FAILED,
            type="sigil.turn.failed",
        )
    )
    index.index_event(replaced)

    (row,) = index.turns()
    assert row["outcome"] == TURN_OUTCOME_FAILED


def test_ledger_index_ignores_non_ledger_events() -> None:
    index = sigil_ledger.ledger_index()

    assert (
        index.index_event(append_event({"type": "user_message", "content": "hi"}))
        is False
    )
    assert index.turns() == []


def test_ledger_turns_lists_newest_first_and_honors_limit() -> None:
    append_indexed(sample_turn_record("turn-old", time=100.0))
    append_indexed(sample_turn_record("turn-new", time=200.0))

    index = sigil_ledger.ledger_index()
    listed = index.turns()
    assert [row["turn_id"] for row in listed] == ["turn-new", "turn-old"]
    assert [row["turn_id"] for row in index.turns(limit=1)] == ["turn-new"]


def test_ledger_effects_touching_filters_by_exact_path() -> None:
    index = sigil_ledger.ledger_index()
    touched = append_indexed(
        sample_effect_record(
            "effect-1",
            kind=EFFECT_KIND_FILE_WRITE,
            path="a.txt",
        )
    )
    append_indexed(
        sample_effect_record(
            "effect-2",
            kind=EFFECT_KIND_FILE_WRITE,
            path="b.txt",
        )
    )

    assert index.effects_touching("a.txt") == [touched]
    assert index.effects_touching("missing.txt") == []


def test_ledger_default_index_is_cached_and_reopens_after_close() -> None:
    first = sigil_ledger.ledger_index()

    assert sigil_ledger.ledger_index() is first

    sigil_ledger.close_ledger_indexes()
    with pytest.raises(sqlite3.ProgrammingError):
        first.connection.execute("SELECT 1")
    assert sigil_ledger.ledger_index() is not first


def test_ledger_append_survives_index_failure(monkeypatch) -> None:
    def broken_index() -> sigil_ledger.LedgerIndex:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(sigil_ledger, "_WARNED_FAILURES", set())
    monkeypatch.setattr(sigil_ledger, "ledger_index", broken_index)

    event = sigil_ledger.append_turn_record(sample_turn_record())

    (stored_event,) = read_events()
    assert stored_event == event


def test_ledger_reindex_reads_event_store() -> None:
    append_event(sample_turn_record("turn-old", time=100.0))
    publish_sigil_draft(
        DraftEvent(
            event_type="zeta.tool.called",
            source="zeta",
            payload={
                "effects": [sample_effect_record("effect-old", turn_id="turn-old")]
            },
            session_id="default",
            timestamp_micros=101_000_000,
        )
    )
    append_event(sample_turn_record("turn-new", time=200.0))
    append_event({"type": "user_message", "content": "not a ledger record"})
    index = sigil_ledger.ledger_index()
    index.index_event(
        Event(
            id="stale-event",
            event_type="sigil.turn.completed",
            source="test",
            payload=sample_turn_record("stale-turn", time=300.0),
            idempotency_key=None,
            caused_by=None,
            session_id="default",
            turn_id="stale-turn",
            timestamp_micros=300_000_000,
        )
    )

    counts = sigil_ledger.reindex()

    assert counts == (2, 1)
    assert [row["turn_id"] for row in index.turns()] == ["turn-new", "turn-old"]
    assert index.turn("stale-turn") is None
    assert index.effects_for_turn("turn-old")[0]["effect_id"] == "effect-old"


def test_ledger_reindex_uses_event_metadata() -> None:
    publish_sigil_draft(
        DraftEvent(
            event_type="sigil.turn.completed",
            source="test",
            payload={
                "turn_id": "turn-meta",
                "time": 1.0,
                "session": "payload-session",
                "cwd": "/payload",
                "workflow": "run",
                "objective": "ls",
                "contract": {"staged": False},
                "outcome": TURN_OUTCOME_EXECUTED,
            },
            session_id="event-session",
            timestamp_micros=2_000_000,
        )
    )

    sigil_ledger.reindex()

    turn = sigil_ledger.ledger_index().turn("turn-meta")
    assert turn is not None
    assert turn["time"] == 2.0
    assert turn["session"] == "event-session"


def test_ledger_reindex_is_idempotent() -> None:
    append_event(sample_turn_record())
    publish_sigil_draft(
        DraftEvent(
            event_type="zeta.tool.called",
            source="zeta",
            payload={"effects": [sample_effect_record()]},
            session_id="default",
            timestamp_micros=101_000_000,
        )
    )

    first = sigil_ledger.reindex()
    second = sigil_ledger.reindex()

    assert first == second == (1, 1)
    index = sigil_ledger.ledger_index()
    assert len(index.turns()) == 1
    assert len(index.effects_for_turn("turn-1")) == 1


def test_ledger_query_turns_filters_workflow_outcome_and_since() -> None:
    append_indexed(sample_turn_record("turn-ask", workflow="ask", time=100.0))
    append_indexed(
        sample_turn_record(
            "turn-broken",
            workflow="do",
            outcome=TURN_OUTCOME_FAILED,
            time=200.0,
        )
    )
    append_indexed(sample_turn_record("turn-run", workflow="run", time=300.0))

    index = sigil_ledger.ledger_index()
    assert [row["turn_id"] for row in index.query_turns(workflow="ask")] == ["turn-ask"]
    assert [row["turn_id"] for row in index.query_turns(failed=True)] == ["turn-broken"]
    assert [row["turn_id"] for row in index.query_turns(since=250.0)] == ["turn-run"]
    assert [row["turn_id"] for row in index.query_turns(limit=2)] == [
        "turn-run",
        "turn-broken",
    ]


def test_ledger_query_turns_scopes_by_session_and_touched_path() -> None:
    append_indexed(sample_turn_record("turn-here", time=100.0, session="here"))
    append_indexed(sample_turn_record("turn-there", time=200.0, session="there"))
    append_indexed(
        sample_effect_record(
            "effect-write",
            turn_id="turn-here",
            kind=EFFECT_KIND_FILE_WRITE,
            path="notes.txt",
        )
    )

    index = sigil_ledger.ledger_index()
    assert [row["turn_id"] for row in index.query_turns(session="there")] == [
        "turn-there"
    ]
    assert [row["turn_id"] for row in index.query_turns(touched=("notes.txt",))] == [
        "turn-here"
    ]
    assert index.query_turns(touched=("missing.txt",)) == []


def test_ledger_turn_ids_with_prefix_lists_matches_sorted() -> None:
    index = sigil_ledger.ledger_index()
    index.index_event(append_event(sample_turn_record("aaaa-1111")))
    index.index_event(append_event(sample_turn_record("aaaa-2222")))
    index.index_event(append_event(sample_turn_record("bbbb-3333")))

    assert index.turn_ids_with_prefix("aaaa") == ["aaaa-1111", "aaaa-2222"]
    assert index.turn_ids_with_prefix("bbbb-3333") == ["bbbb-3333"]
    assert index.turn_ids_with_prefix("cccc") == []


def test_ledger_pending_staged_command_clears_on_resolution(monkeypatch) -> None:
    monkeypatch.setenv("SIGIL_SESSION_ID", "pending-test")
    index = sigil_ledger.ledger_index()
    append_indexed(
        sample_effect_record(
            "effect-staged",
            staged=True,
            tool_call_id="call-1",
            command="uv run pytest",
        )
    )

    pending = index.pending_staged_command("pending-test")
    assert pending is not None
    assert pending["command"] == "uv run pytest"
    assert index.pending_staged_command("other-session") is None

    append_indexed(
        sample_effect_record(
            "effect-resolved",
            kind="handoff",
            tool_call_id="call-1",
            resolved_outcome="executed",
        )
    )

    assert index.pending_staged_command("pending-test") is None


def test_ledger_cost_since_sums_session_turns(monkeypatch) -> None:
    monkeypatch.setenv("SIGIL_SESSION_ID", "cost-test")
    index = sigil_ledger.ledger_index()
    append_indexed(
        sample_turn_record(
            "turn-early",
            time=100.0,
            cost={"input_tokens": 10, "output_tokens": 5, "model_calls": 1},
        )
    )
    append_indexed(
        sample_turn_record(
            "turn-late",
            time=300.0,
            cost={"input_tokens": 100, "output_tokens": 50, "model_calls": 2},
        )
    )

    today = index.cost_since("cost-test", 200.0)
    assert today == {
        "input_tokens": 100,
        "output_tokens": 50,
        "model_calls": 2,
        "turns": 1,
    }
    everything = index.cost_since("cost-test", 0.0)
    assert everything["turns"] == 2
    assert everything["input_tokens"] == 110


def seed_log_cli_index(monkeypatch) -> None:
    monkeypatch.setenv("SIGIL_SESSION_ID", "log-cli")
    append_indexed(
        sample_turn_record(
            "turn-do-1111",
            workflow="do",
            objective="refactor the staging path",
            time=100.0,
            cost={"input_tokens": 1000, "output_tokens": 200, "model_calls": 3},
        )
    )
    append_indexed(
        sample_turn_record(
            "turn-ask-222",
            workflow="ask",
            objective="why did the test fail?",
            outcome=TURN_OUTCOME_FAILED,
            time=200.0,
        )
    )
    append_indexed(
        sample_turn_record(
            "turn-elsewhere",
            workflow="run",
            objective="ls",
            time=300.0,
            session="elsewhere",
        )
    )


def test_sigil_log_lists_every_session_newest_first(monkeypatch) -> None:
    seed_log_cli_index(monkeypatch)

    result = CliRunner().invoke(sigil_cli, ["log"])

    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert len(lines) == 3
    assert lines[0].startswith("turn-els")
    assert "elsewhere" in lines[0]
    assert lines[1].startswith("turn-ask")
    assert "log-cli" in lines[1]
    assert "why did the test fail?" in lines[1]
    assert lines[2].startswith("turn-do-")


def test_sigil_log_filters_workflow_failed_and_sessions(monkeypatch) -> None:
    seed_log_cli_index(monkeypatch)
    runner = CliRunner()

    by_workflow = runner.invoke(sigil_cli, ["log", "--workflow", "do"])
    by_failed = runner.invoke(sigil_cli, ["log", "--failed"])
    elsewhere = runner.invoke(sigil_cli, ["log", "--session", "elsewhere"])
    legacy_flag = runner.invoke(sigil_cli, ["log", "--all-sessions"])

    assert by_workflow.exit_code == 0
    assert len(by_workflow.output.splitlines()) == 1
    assert "refactor the staging path" in by_workflow.output
    assert by_failed.exit_code == 0
    assert len(by_failed.output.splitlines()) == 1
    assert "why did the test fail?" in by_failed.output
    assert len(elsewhere.output.splitlines()) == 1
    assert "ls" in elsewhere.output
    assert "elsewhere" not in by_workflow.output
    assert legacy_flag.exit_code != 0


def test_sigil_log_session_filter_omits_the_session_column(monkeypatch) -> None:
    seed_log_cli_index(monkeypatch)

    result = CliRunner().invoke(sigil_cli, ["log", "--session", "log-cli"])

    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert len(lines) == 2
    assert all("log-cli" not in line for line in lines)


def test_sigil_log_renders_cost_and_json(monkeypatch) -> None:
    seed_log_cli_index(monkeypatch)
    runner = CliRunner()

    with_cost = runner.invoke(sigil_cli, ["log", "--cost"])
    as_json = runner.invoke(sigil_cli, ["log", "--json"])

    assert with_cost.exit_code == 0
    assert "1200 tok" in with_cost.output
    assert "3 calls" in with_cost.output
    assert as_json.exit_code == 0
    payload = json.loads(as_json.output)
    assert [turn["turn_id"] for turn in payload["turns"]] == [
        "turn-elsewhere",
        "turn-ask-222",
        "turn-do-1111",
    ]


def test_sigil_log_touched_filter_finds_writing_turn(monkeypatch) -> None:
    seed_log_cli_index(monkeypatch)
    append_indexed(
        sample_effect_record(
            "effect-write",
            turn_id="turn-do-1111",
            kind=EFFECT_KIND_FILE_WRITE,
            path="/tmp/notes.txt",
        )
    )

    result = CliRunner().invoke(sigil_cli, ["log", "--touched", "/tmp/notes.txt"])

    assert result.exit_code == 0
    assert len(result.output.splitlines()) == 1
    assert result.output.startswith("turn-do-")


def test_sigil_log_empty_ledger_prints_friendly_line(monkeypatch) -> None:
    monkeypatch.setenv("SIGIL_SESSION_ID", "empty-session")

    result = CliRunner().invoke(sigil_cli, ["log"])

    assert result.exit_code == 0
    assert "no turns recorded" in result.output


def seed_show_and_blame_index(monkeypatch) -> None:
    monkeypatch.setenv("SIGIL_SESSION_ID", "show-cli")
    record = turn_record(
        "turn-do-1111",
        workflow="do",
        objective="refactor the staging path",
        contract=turn_contract("do", ("read", "edit", "bash"), staged=False),
        outcome=TURN_OUTCOME_EXECUTED,
        agent={"model": "qwen2.5-coder", "url": "http://127.0.0.1:8080/v1"},
        cost={
            "input_tokens": 1000,
            "output_tokens": 200,
            "model_calls": 3,
            "wall_ms": 4200,
        },
        prompt_object_ids=["sha256:" + "70da571d" + "0" * 56],
        effect_ids=["effect-edit"],
    )
    append_indexed({**record, "time": 100.0})
    append_indexed(
        sample_effect_record(
            "effect-edit",
            turn_id="turn-do-1111",
            kind="file_edit",
            path="/tmp/notes.txt",
            before_hash="sha256:" + "aa" * 32,
            after_hash="sha256:" + "bb" * 32,
            time=100.0,
        )
    )
    append_indexed(sample_turn_record("turn-other-22", time=200.0))


def test_sigil_log_show_renders_the_full_record(monkeypatch) -> None:
    seed_show_and_blame_index(monkeypatch)

    result = CliRunner().invoke(sigil_cli, ["log", "show", "turn-do"])

    assert result.exit_code == 0
    assert "turn     turn-do-1111" in result.output
    assert "workflow do" in result.output
    assert "outcome  executed" in result.output
    assert "refactor the staging path" in result.output
    assert "read, edit, bash" in result.output
    assert "qwen2.5-coder" in result.output
    assert "1200 tok" in result.output
    assert "3 calls" in result.output
    assert "file_edit" in result.output
    assert "/tmp/notes.txt" in result.output
    assert "70da571d" in result.output


def test_sigil_log_show_reports_ambiguous_and_unknown_ids(monkeypatch) -> None:
    seed_show_and_blame_index(monkeypatch)
    runner = CliRunner()

    ambiguous = runner.invoke(sigil_cli, ["log", "show", "turn-"])
    unknown = runner.invoke(sigil_cli, ["log", "show", "nope"])

    assert ambiguous.exit_code != 0
    assert "turn-do-1111" in ambiguous.output
    assert "turn-other-22" in ambiguous.output
    assert unknown.exit_code != 0
    assert "nope" in unknown.output


def test_sigil_log_show_json_emits_record_and_effects(monkeypatch) -> None:
    seed_show_and_blame_index(monkeypatch)

    result = CliRunner().invoke(sigil_cli, ["log", "show", "--json", "turn-do-1111"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["turn"]["turn_id"] == "turn-do-1111"
    assert payload["effects"][0]["effect_id"] == "effect-edit"


def test_sigil_blame_lists_turns_touching_a_file(monkeypatch) -> None:
    seed_show_and_blame_index(monkeypatch)

    result = CliRunner().invoke(sigil_cli, ["blame", "/tmp/notes.txt"])

    assert result.exit_code == 0
    assert "file_edit" in result.output
    assert "do" in result.output
    assert "executed" in result.output
    assert "turn-do-" in result.output
    assert "refactor the staging path" in result.output
    assert "70da571d" in result.output


def test_sigil_blame_reports_untouched_files(monkeypatch) -> None:
    seed_show_and_blame_index(monkeypatch)

    result = CliRunner().invoke(sigil_cli, ["blame", "/tmp/other.txt"])

    assert result.exit_code == 0
    assert "no recorded writes" in result.output


def test_ledger_cli_log_reindex_reports_counts() -> None:
    append_event(sample_turn_record())
    publish_sigil_draft(
        DraftEvent(
            event_type="zeta.tool.called",
            source="zeta",
            payload={"effects": [sample_effect_record()]},
            session_id="default",
            timestamp_micros=101_000_000,
        )
    )

    result = CliRunner().invoke(sigil_cli, ["log", "reindex"])

    assert result.exit_code == 0
    assert "1 turn record(s)" in result.output
    assert "1 effect record(s)" in result.output
    assert sigil_ledger.ledger_index().turn("turn-1") is not None


def seed_bundle_state(monkeypatch) -> dict[str, str]:
    """Record one turn with an effect, bridged into its session trace store."""
    from zeta import trace as zeta_trace

    monkeypatch.setenv("SIGIL_SESSION_ID", "bundle-src")
    turn = append_event(
        sample_turn_record(
            "turn-bundle-1",
            workflow="do",
            objective="write the deploy notes",
            time=100.0,
        )
    )
    append_indexed(
        sample_effect_record(
            "effect-bundle-1",
            turn_id="turn-bundle-1",
            kind=EFFECT_KIND_FILE_WRITE,
            path="/tmp/deploy-notes.md",
            session="bundle-src",
            time=101.0,
        )
    )
    sigil_ledger.ledger_index().index_event(turn)
    store = zeta_trace.SqliteStore(trace_store_path("bundle-src"))
    prompt_id = store.put_object(
        zeta_trace.Object(
            kind="prompt",
            schema="zeta.prompt.v1",
            data={"payload": {"text": "the deploy prompt"}},
        )
    )
    turn_object_id = store.put_object(
        zeta_trace.Object(
            kind="turn_record",
            schema="sigil.turn",
            data={"turn_id": "turn-bundle-1"},
            links=(prompt_id,),
        )
    )
    store.record_derivation(
        zeta_trace.Derivation(
            producer="SigilTurnRecord:v1",
            output_id=turn_object_id,
            input_ids=(prompt_id,),
        )
    )
    store.set_ref("turn/turn-bundle-1", turn_object_id)
    store.close()
    return {"prompt_id": prompt_id, "turn_object_id": turn_object_id}


def test_bundle_export_collects_turns_effects_and_trace_closure(
    monkeypatch,
) -> None:
    from sigil.bundle import export_bundle

    ids = seed_bundle_state(monkeypatch)

    bundle = export_bundle()

    assert bundle["sigil_bundle"] == 1
    record_ids = {
        record.get("effect_id") or record.get("turn_id") for record in bundle["records"]
    }
    assert record_ids == {"turn-bundle-1", "effect-bundle-1"}
    graph = bundle["sessions"]["bundle-src"]
    assert {obj["id"] for obj in graph["objects"]} == set(ids.values())
    assert graph["refs"] == {"turn/turn-bundle-1": ids["turn_object_id"]}
    derivation = graph["derivations"][0]
    assert derivation["producer"] == "SigilTurnRecord:v1"
    assert isinstance(derivation["created_at"], float)


def test_bundle_export_honors_since_and_session_filters(monkeypatch) -> None:
    from sigil.bundle import export_bundle

    seed_bundle_state(monkeypatch)
    append_indexed(sample_turn_record("turn-late", time=900.0, session="late-session"))

    recent = export_bundle(since=500.0)
    scoped = export_bundle(session="bundle-src")

    assert {r["turn_id"] for r in recent["records"]} == {"turn-late"}
    assert {r.get("effect_id") or r.get("turn_id") for r in scoped["records"]} == {
        "turn-bundle-1",
        "effect-bundle-1",
    }


def test_bundle_export_skips_sessions_without_trace_stores(monkeypatch) -> None:
    from sigil.bundle import export_bundle

    monkeypatch.setenv("SIGIL_SESSION_ID", "no-trace")
    append_indexed(sample_turn_record("turn-no-trace"))

    bundle = export_bundle()

    assert [r["turn_id"] for r in bundle["records"]] == ["turn-no-trace"]
    assert bundle["sessions"] == {}


def fresh_state_dir(monkeypatch, tmp_path) -> None:
    """Re-point sigil state at an empty directory, as on another machine."""
    sigil_ledger.close_ledger_indexes()
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path / "imported-state"))


def test_bundle_import_restores_ledger_and_trace_queries(
    monkeypatch,
    tmp_path,
) -> None:
    from sigil.bundle import export_bundle, import_bundle

    ids = seed_bundle_state(monkeypatch)
    bundle = export_bundle()
    fresh_state_dir(monkeypatch, tmp_path)

    import_bundle(bundle)

    runner = CliRunner()
    log = runner.invoke(sigil_cli, ["log"])
    show = runner.invoke(sigil_cli, ["log", "show", "turn-bundle-1"])
    blame = runner.invoke(sigil_cli, ["blame", "/tmp/deploy-notes.md"])
    trace_show = runner.invoke(
        sigil_cli,
        ["trace", "--session", "bundle-src", "show", "--json", ids["prompt_id"]],
    )
    assert log.exit_code == 0 and "write the deploy notes" in log.output
    assert show.exit_code == 0 and "turn-bundle-1" in show.output
    assert blame.exit_code == 0 and "turn-bun" in blame.output
    assert trace_show.exit_code == 0
    assert json.loads(trace_show.output)["id"] == ids["prompt_id"]


def test_bundle_import_is_idempotent(monkeypatch, tmp_path) -> None:
    from sigil.bundle import export_bundle, import_bundle

    seed_bundle_state(monkeypatch)
    bundle = export_bundle()
    fresh_state_dir(monkeypatch, tmp_path)

    first = import_bundle(bundle)
    second = import_bundle(bundle)

    assert first["records"] == 2
    assert second["records"] == 0
    log_lines = read_events()
    assert len(log_lines) == 1
    assert log_lines[0].event_type == "sigil.turn.completed"


def test_bundle_import_preserves_event_causality(monkeypatch, tmp_path) -> None:
    from sigil.bundle import export_bundle, import_bundle

    append_indexed(sample_turn_record("turn-causal", caused_by="prompt-event"))
    bundle = export_bundle()
    fresh_state_dir(monkeypatch, tmp_path)

    import_bundle(bundle)

    (event,) = read_events()
    assert event.caused_by == "prompt-event"


def test_bundle_import_survives_reindex(monkeypatch, tmp_path) -> None:
    from sigil.bundle import export_bundle, import_bundle

    seed_bundle_state(monkeypatch)
    bundle = export_bundle()
    fresh_state_dir(monkeypatch, tmp_path)
    import_bundle(bundle)
    sigil_ledger.close_ledger_indexes()
    connection = sqlite3.connect(state_dir() / "events.sqlite3")
    try:
        connection.executescript("DROP TABLE effects; DROP TABLE turns;")
    finally:
        connection.close()

    result = CliRunner().invoke(sigil_cli, ["log", "reindex"])

    assert result.exit_code == 0
    assert sigil_ledger.ledger_index().turn("turn-bundle-1") is not None


def test_sigil_log_export_and_import_round_trip_via_cli(
    monkeypatch,
    tmp_path,
) -> None:
    seed_bundle_state(monkeypatch)
    bundle_path = tmp_path / "bundle.json"
    runner = CliRunner()

    exported = runner.invoke(sigil_cli, ["log", "export", "--output", str(bundle_path)])
    assert exported.exit_code == 0
    fresh_state_dir(monkeypatch, tmp_path)
    imported = runner.invoke(sigil_cli, ["log", "import", str(bundle_path)])

    assert imported.exit_code == 0
    listed = runner.invoke(sigil_cli, ["log"])
    assert "write the deploy notes" in listed.output


def test_ledger_survives_session_clear() -> None:
    sigil_ledger.append_turn_record(sample_turn_record())
    root = session_dir()
    root.mkdir(parents=True, exist_ok=True)
    (root / "recent-turns.jsonl").write_text("", encoding="utf-8")

    clear_current_session()

    assert not root.exists()
    assert (state_dir() / "events.sqlite3").exists()
    assert sigil_ledger.ledger_index().turn("turn-1") is not None
