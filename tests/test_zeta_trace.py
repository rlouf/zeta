"""Trace store and run-timeline tests."""

import json
import sqlite3
from pathlib import Path

import pytest
from _zeta_helpers import (
    BatchSpyStore,
    event_by_type,
    read_tool_call_response,
    read_tool_payload,
)
from click.testing import CliRunner

from sigil.cli import cli as sigil_cli
from sigil.display.summarize import assistant_trace_summary
from sigil.trace.replay import latest_model_answer
from zeta import context as zeta_context
from zeta import events as zeta_timeline
from zeta import loop as zeta_agent
from zeta import substrate as zeta_trace
from zeta.context.components import chat_messages
from zeta.events import Filter, SqliteEventStore, event_store_path
from zeta.models import profiles as zeta_models
from zeta.session import Session, default_session


def zeta_event_store() -> SqliteEventStore:
    return SqliteEventStore(event_store_path())


def zeta_runtime_context(
    trace_store: zeta_trace.Store | None = None,
) -> Session:
    context = default_session()
    if trace_store is None:
        return context
    return Session(
        session_id=context.session_id,
        event_sink=context.event_sink,
        trace_store=trace_store,
        tool_registry=context.tool_registry,
        state_dir=context.state_dir,
        session_dir=context.session_dir,
    )


def record_zeta_event(
    event: dict[str, object],
    *,
    runtime_context: Session | None = None,
) -> dict[str, object]:
    return zeta_timeline.record_event(
        event,
        runtime_context=runtime_context or zeta_runtime_context(),
    )


def current_zeta_timeline() -> list[dict[str, object]]:
    return zeta_timeline.current_timeline(runtime_context=zeta_runtime_context())


def assert_no_trace_timeline_chain(
    store: zeta_trace.Store,
    *,
    session_id: str = "zeta-test",
) -> None:
    refs = {ref.name: ref.object_id for ref in store.refs()}
    assert store.objects(kind="run_event") == []
    assert f"run/{session_id}/head" not in refs
    assert f"run/{session_id}/event_head" not in refs


@pytest.mark.parametrize(
    "store",
    [
        pytest.param(zeta_trace.InMemoryStore(), id="memory"),
        pytest.param(None, id="sqlite"),
    ],
)
def test_zeta_trace_move_ref_compares_expected_value(
    tmp_path: Path,
    store: zeta_trace.Store | None,
) -> None:
    trace_store = store or zeta_trace.SqliteStore(tmp_path / "trace.sqlite3")
    created = trace_store.move_ref("run/test/head", None, "sha256:first")

    assert created == zeta_trace.RefUpdate(
        name="run/test/head",
        old_object_id=None,
        new_object_id="sha256:first",
        updated=True,
    )
    updated = trace_store.move_ref("run/test/head", "sha256:first", "sha256:second")

    assert updated == zeta_trace.RefUpdate(
        name="run/test/head",
        old_object_id="sha256:first",
        new_object_id="sha256:second",
        updated=True,
    )
    assert trace_store.get_ref("run/test/head") == zeta_trace.Ref(
        name="run/test/head", object_id="sha256:second"
    )
    stale = trace_store.move_ref("run/test/head", "sha256:first", "sha256:third")

    assert stale == zeta_trace.RefUpdate(
        name="run/test/head",
        old_object_id="sha256:second",
        new_object_id="sha256:third",
        updated=False,
    )
    assert trace_store.get_ref("run/test/head") == zeta_trace.Ref(
        name="run/test/head", object_id="sha256:second"
    )


@pytest.mark.parametrize(
    "store",
    [
        pytest.param(zeta_trace.InMemoryStore(), id="memory"),
        pytest.param(None, id="sqlite"),
    ],
)
def test_zeta_trace_move_ref_expected_none_creates_only_when_absent(
    tmp_path: Path,
    store: zeta_trace.Store | None,
) -> None:
    trace_store = store or zeta_trace.SqliteStore(tmp_path / "trace.sqlite3")

    trace_store.move_ref("run/test/head", None, "sha256:first")

    assert trace_store.get_ref("run/test/head") == zeta_trace.Ref(
        name="run/test/head", object_id="sha256:first"
    )
    stale = trace_store.move_ref("run/test/head", None, "sha256:second")

    assert stale == zeta_trace.RefUpdate(
        name="run/test/head",
        old_object_id="sha256:first",
        new_object_id="sha256:second",
        updated=False,
    )
    assert trace_store.get_ref("run/test/head") == zeta_trace.Ref(
        name="run/test/head", object_id="sha256:first"
    )


def test_zeta_trace_object_ids_ignore_dict_key_order() -> None:
    first = zeta_trace.Object(
        kind="example",
        schema="v1",
        data={"b": 1, "a": {"z": 2, "y": [{"b": 3, "a": 4}]}},
    )
    second = zeta_trace.Object(
        kind="example",
        schema="v1",
        data={"a": {"y": [{"a": 4, "b": 3}], "z": 2}, "b": 1},
    )

    assert first.content_address() == second.content_address()


def test_zeta_trace_object_ids_change_for_schema_data_and_links() -> None:
    base = zeta_trace.Object(
        kind="example", schema="v1", data={"value": 1}
    ).content_address()

    assert (
        base
        != zeta_trace.Object(
            kind="example", schema="v2", data={"value": 1}
        ).content_address()
    )
    assert (
        base
        != zeta_trace.Object(
            kind="example", schema="v1", data={"value": 2}
        ).content_address()
    )
    assert (
        zeta_trace.Object(
            kind="example",
            schema="v1",
            data={"value": 1},
            links=("left", "right"),
        ).content_address()
        != zeta_trace.Object(
            kind="example",
            schema="v1",
            data={"value": 1},
            links=("right", "left"),
        ).content_address()
    )


def test_zeta_trace_sqlite_persists_objects_refs_derivations_and_closure(
    tmp_path: Path,
) -> None:
    path = tmp_path / "trace.sqlite3"
    store = zeta_trace.SqliteStore(path)
    parent_id = store.put_object(
        zeta_trace.Object(kind="context", schema="v1", data={"text": "parent"})
    )
    child_id = store.put_object(
        zeta_trace.Object(
            kind="prompt",
            schema="v1",
            data={"text": "child"},
            links=(parent_id,),
        )
    )
    store.move_ref("prompt/current", None, child_id)
    store.record_derivation(
        zeta_trace.Derivation(
            producer="test:v1",
            output_id=child_id,
            input_ids=(parent_id,),
            params={"mode": "unit"},
        )
    )
    store.close()

    reopened = zeta_trace.SqliteStore(path)
    assert reopened.get_object(parent_id) == zeta_trace.Object(
        kind="context", schema="v1", data={"text": "parent"}
    )
    assert reopened.get_ref("prompt/current") == zeta_trace.Ref(
        name="prompt/current", object_id=child_id
    )
    assert reopened.derivations_for_output(child_id)[0].producer == "test:v1"
    assert set(reopened.graph_closure([child_id])) == {parent_id, child_id}
    assert reopened.stats().object_count == 2
    reopened.close()


def test_zeta_trace_sqlite_reports_incompatible_substrate_schema(
    tmp_path: Path,
) -> None:
    path = tmp_path / "zeta.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE derivations (
          id TEXT NOT NULL,
          producer TEXT NOT NULL,
          output_id TEXT NOT NULL,
          input_ids_json TEXT NOT NULL,
          params_json TEXT NOT NULL,
          created_at REAL NOT NULL
        );
        CREATE TABLE events (
          seq INTEGER PRIMARY KEY AUTOINCREMENT,
          id TEXT UNIQUE NOT NULL,
          type TEXT NOT NULL,
          source TEXT NOT NULL,
          payload TEXT NOT NULL,
          idempotency_key TEXT,
          caused_by TEXT,
          session_id TEXT,
          turn_id TEXT,
          timestamp INTEGER NOT NULL
        ) STRICT;
        INSERT INTO events (id, type, source, payload, timestamp)
        VALUES ('evt_existing', 'zeta.test', 'test', '{}', 1);
        """
    )
    connection.close()

    with pytest.raises(sqlite3.OperationalError, match="reinit-store --yes"):
        zeta_trace.SqliteStore(path, session_id="current")


def test_sigil_trace_reinit_store_recreates_unified_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZETA_STATE_DIR", str(tmp_path))
    path = tmp_path / "zeta.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE derivations (
          id TEXT NOT NULL,
          producer TEXT NOT NULL,
          output_id TEXT NOT NULL,
          input_ids_json TEXT NOT NULL,
          params_json TEXT NOT NULL,
          created_at REAL NOT NULL
        );
        """
    )
    connection.close()

    result = CliRunner().invoke(sigil_cli, ["trace", "reinit-store", "--yes"])

    assert result.exit_code == 0
    assert f"reinitialized {path}" in result.output
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(derivations)")
        }
    finally:
        connection.close()

    assert "session_id" in columns


def seed_session_store(session_id: str, text: str) -> str:
    """Write one prompt object into a named session's trace store."""
    return seed_trace_store(zeta_trace.zeta_sqlite_path(), text, session_id=session_id)


def seed_sigil_session_store(session_id: str, text: str) -> str:
    """Write one prompt object into a named Sigil session's trace store."""
    return seed_trace_store(zeta_trace.zeta_sqlite_path(), text, session_id=session_id)


def seed_trace_store(path: Path, text: str, *, session_id: str | None = None) -> str:
    """Write one prompt object into a trace store path."""
    store = zeta_trace.SqliteStore(path, session_id=session_id)
    prompt_id = store.put_object(
        zeta_trace.Object(
            kind="prompt",
            schema="zeta.prompt.v1",
            data={"payload": {"text": text}},
        )
    )
    store.record_derivation(
        zeta_trace.Derivation(producer="unit:test", output_id=prompt_id)
    )
    store.close()
    return prompt_id


def test_zeta_sqlite_store_read_only_rejects_writes(tmp_path: Path) -> None:
    path = tmp_path / "trace.sqlite3"
    writer = zeta_trace.SqliteStore(path)
    stored_id = writer.put_object(
        zeta_trace.Object(kind="prompt", schema="zeta.prompt.v1", data={})
    )
    writer.close()

    reader = zeta_trace.SqliteStore(path, read_only=True)

    assert reader.get_object(stored_id) is not None
    with pytest.raises(sqlite3.OperationalError):
        reader.put_object(
            zeta_trace.Object(kind="prompt", schema="zeta.prompt.v1", data={"x": 1})
        )
    reader.close()


def test_zeta_sqlite_store_opens_other_sessions_read_only(monkeypatch) -> None:
    monkeypatch.setenv("SIGIL_SESSION_ID", "current")
    prompt_id = seed_session_store("other", "from the other session")

    store = zeta_trace.SqliteStore(
        zeta_trace.zeta_sqlite_path(),
        session_id="other",
        read_only=True,
    )

    assert store.read_only
    assert store.get_object(prompt_id) is not None
    with pytest.raises(sqlite3.OperationalError):
        store.put_object(zeta_trace.Object(kind="prompt", schema="v1", data={}))
    second = zeta_trace.SqliteStore(
        zeta_trace.zeta_sqlite_path(),
        session_id="other",
        read_only=True,
    )
    assert second is not store
    second.close()
    store.close()


def test_zeta_available_session_ids_lists_stores_sorted(monkeypatch) -> None:
    monkeypatch.setenv("SIGIL_SESSION_ID", "current")
    seed_session_store("beta", "b")
    seed_session_store("alpha", "a")

    assert zeta_trace.available_session_ids() == ["alpha", "beta"]


def test_sigil_zeta_trace_cli_session_scope_reads_other_store(monkeypatch) -> None:
    monkeypatch.setenv("SIGIL_SESSION_ID", "current")
    prompt_id = seed_sigil_session_store("other", "scoped read")

    result = CliRunner().invoke(
        sigil_cli, ["trace", "--session", "other", "show", "--json", prompt_id]
    )

    assert result.exit_code == 0
    assert json.loads(result.output)["id"] == prompt_id


def test_sigil_zeta_trace_cli_unknown_session_lists_available(monkeypatch) -> None:
    monkeypatch.setenv("SIGIL_SESSION_ID", "current")
    seed_sigil_session_store("known", "seed")

    result = CliRunner().invoke(sigil_cli, ["trace", "--session", "missing", "log"])

    assert result.exit_code != 0
    assert "missing" in result.output
    assert "known" in result.output


def test_sigil_zeta_trace_cli_log_all_sessions_prefixes_session_ids(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_SESSION_ID", "current")
    seed_sigil_session_store("alpha", "alpha prompt")
    seed_sigil_session_store("beta", "beta prompt")

    result = CliRunner().invoke(sigil_cli, ["trace", "log", "--all-sessions"])

    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert any(line.startswith("alpha") for line in lines)
    assert any(line.startswith("beta") for line in lines)


def test_zeta_trace_sqlite_search_matches_data_and_filters_kind(
    tmp_path: Path,
) -> None:
    store = zeta_trace.SqliteStore(tmp_path / "trace.sqlite3")
    matching = store.put_object(
        zeta_trace.Object(
            kind="prompt", schema="v1", data={"text": "the Kubernetes incident"}
        )
    )
    other_kind = store.put_object(
        zeta_trace.Object(
            kind="tool_result", schema="v1", data={"text": "kubernetes logs"}
        )
    )
    store.put_object(
        zeta_trace.Object(kind="prompt", schema="v1", data={"text": "unrelated"})
    )

    hits = store.search_objects("kubernetes")
    prompt_hits = store.search_objects("kubernetes", kind="prompt")

    assert {hit_id for hit_id, _ in hits} == {matching, other_kind}
    assert [hit_id for hit_id, _ in prompt_hits] == [matching]
    store.close()


def test_zeta_trace_sqlite_search_treats_like_wildcards_literally(
    tmp_path: Path,
) -> None:
    store = zeta_trace.SqliteStore(tmp_path / "trace.sqlite3")
    percent = store.put_object(
        zeta_trace.Object(kind="prompt", schema="v1", data={"text": "100% done"})
    )
    store.put_object(
        zeta_trace.Object(kind="prompt", schema="v1", data={"text": "100 done"})
    )

    hits = store.search_objects("100%")

    assert [hit_id for hit_id, _ in hits] == [percent]
    store.close()


def test_zeta_trace_in_memory_search_matches_case_insensitively() -> None:
    store = zeta_trace.InMemoryStore()
    matching = store.put_object(
        zeta_trace.Object(kind="prompt", schema="v1", data={"text": "Deploy Friday"})
    )
    store.put_object(
        zeta_trace.Object(kind="prompt", schema="v1", data={"text": "other"})
    )

    hits = store.search_objects("deploy friday")

    assert [hit_id for hit_id, _ in hits] == [matching]


def test_sigil_zeta_trace_cli_grep_lists_matches(monkeypatch) -> None:
    monkeypatch.setenv("SIGIL_SESSION_ID", "current")
    prompt_id = seed_sigil_session_store("current", "the missing deploy key")
    seed_sigil_session_store("current", "unrelated")

    result = CliRunner().invoke(sigil_cli, ["trace", "grep", "deploy key"])

    assert result.exit_code == 0
    short_id = prompt_id.split(":", 1)[1][:8]
    assert short_id in result.output
    assert len(result.output.strip().splitlines()) == 1


def test_sigil_zeta_trace_cli_grep_all_sessions_names_the_session(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_SESSION_ID", "current")
    seed_sigil_session_store("alpha", "asked about the rollback")
    seed_sigil_session_store("beta", "something else")

    result = CliRunner().invoke(
        sigil_cli, ["trace", "grep", "rollback", "--all-sessions"]
    )

    assert result.exit_code == 0
    lines = [line for line in result.output.splitlines() if line]
    assert lines and all(line.startswith("alpha") for line in lines)


def test_sigil_zeta_trace_cli_grep_reports_no_matches(monkeypatch) -> None:
    monkeypatch.setenv("SIGIL_SESSION_ID", "current")
    seed_sigil_session_store("current", "recorded")

    result = CliRunner().invoke(sigil_cli, ["trace", "grep", "absent-token"])

    assert result.exit_code == 0
    assert "no trace objects match" in result.output


def test_sigil_zeta_trace_cli_smoke_with_in_memory_store(monkeypatch) -> None:
    store = zeta_trace.InMemoryStore()
    parent_id = store.put_object(
        zeta_trace.Object(kind="context", schema="v1", data={"text": "parent"})
    )
    prompt_id = store.put_object(
        zeta_trace.Object(
            kind="prompt",
            schema="zeta.prompt.v1",
            data={"payload": {"messages": []}},
            links=(parent_id,),
        )
    )
    store.record_derivation(
        zeta_trace.Derivation(
            producer="unit:test",
            output_id=prompt_id,
            input_ids=(parent_id,),
        )
    )
    store.move_ref("prompt/current", None, prompt_id)
    monkeypatch.setattr("sigil.cli.trace.current_store", lambda: store)

    runner = CliRunner()
    show = runner.invoke(sigil_cli, ["trace", "show", "--json", prompt_id])
    closure = runner.invoke(sigil_cli, ["trace", "closure", prompt_id])
    refs = runner.invoke(sigil_cli, ["trace", "refs"])
    prompts = runner.invoke(sigil_cli, ["trace", "prompts"])

    assert show.exit_code == 0
    assert json.loads(show.output)["derivations"][0]["producer"] == "unit:test"
    assert closure.exit_code == 0
    assert json.loads(closure.output)["objects"][0]["id"] == parent_id
    assert refs.exit_code == 0
    assert json.loads(refs.output)["refs"]["prompt/current"] == prompt_id
    assert prompts.exit_code == 0
    assert json.loads(prompts.output)["prompts"][0]["id"] == prompt_id


def test_sigil_zeta_trace_cli_smoke_with_sqlite_store(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = zeta_trace.SqliteStore(tmp_path / "trace.sqlite3")
    prompt_id = store.put_object(
        zeta_trace.Object(kind="prompt", schema="zeta.prompt.v1", data={})
    )
    store.record_derivation(
        zeta_trace.Derivation(producer="unit:test", output_id=prompt_id)
    )
    monkeypatch.setattr("sigil.cli.trace.current_store", lambda: store)

    result = CliRunner().invoke(sigil_cli, ["trace", "prompts"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["stats"]["object_count"] == 1
    assert data["prompts"][0]["id"] == prompt_id
    store.close()


def test_zeta_chat_messages_keeps_full_history_and_current_events() -> None:
    transcript = [{"role": "user", "content": f"prior-{index}"} for index in range(25)]
    current_events = [
        {"type": "model", "content": f"current-{index}"} for index in range(25)
    ]

    messages = zeta_context.component_messages(
        zeta_context.prompt_components(
            "inspect",
            transcript,
            allowed_capabilities=(),
            current_events=current_events,
            include_non_message_components=False,
        )
    )
    contents = [str(message.get("content") or "") for message in messages]

    assert "prior-0" in contents
    assert "prior-24" in contents
    assert "inspect\n\ncwd:" in contents[26]
    assert "current-0" in contents
    assert "current-24" in contents


def test_zeta_prompt_components_keep_only_the_timeline_tail() -> None:
    over_limit = zeta_context.TIMELINE_TAIL_LIMIT + 10
    transcript = [
        {"role": "user", "content": f"prior-{index}"} for index in range(over_limit)
    ]

    messages = zeta_context.component_messages(
        zeta_context.prompt_components(
            "inspect",
            transcript,
            allowed_capabilities=(),
            include_non_message_components=False,
        )
    )
    contents = [str(message.get("content") or "") for message in messages]

    assert "prior-9" not in contents
    assert "prior-10" in contents
    assert f"prior-{over_limit - 1}" in contents


def test_zeta_timeline_record_and_project(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ZETA_SESSION_ID", "zeta-test")
    runtime_context = zeta_runtime_context()

    record_zeta_event(
        {"type": "tool_call", "name": "read"}, runtime_context=runtime_context
    )

    events = zeta_timeline.current_timeline(runtime_context=runtime_context)
    assert events[0]["type"] == "tool_call"
    assert events[0]["name"] == "read"
    assert_no_trace_timeline_chain(runtime_context.trace_store)
    tool_events = zeta_event_store().list_events(Filter(event_type="zeta.tool.called"))
    assert len(tool_events) == 1
    assert tool_events[0].payload["name"] == "read"


def test_zeta_timeline_tool_result_is_durable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ZETA_SESSION_ID", "zeta-test")
    runtime_context = zeta_runtime_context()

    record_zeta_event(
        {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "name": "read",
            "result": {"ok": True},
        },
        runtime_context=runtime_context,
    )

    timeline = zeta_timeline.current_timeline(runtime_context=runtime_context)
    assert timeline[0]["type"] == "tool_result"
    assert timeline[0]["result"] == {"ok": True}
    tool_events = zeta_event_store().list_events(Filter(event_type="zeta.tool.called"))
    assert len(tool_events) == 1
    assert tool_events[0].payload["result"] == {"ok": True}


def test_zeta_timeline_tool_call_is_caused_by_assistant_event(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ZETA_SESSION_ID", "zeta-test")
    runtime_context = zeta_runtime_context()

    record_zeta_event(
        {
            "type": "model",
            "id": "assistant-event-1",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "read", "arguments": "{}"},
                }
            ],
        },
        runtime_context=runtime_context,
    )
    record_zeta_event(
        {
            "type": "tool_call",
            "id": "call-1",
            "tool_call_id": "call-1",
            "name": "read",
            "input": {},
            "caused_by": "assistant-event-1",
        },
        runtime_context=runtime_context,
    )

    assistant = zeta_event_store().get("assistant-event-1")
    tool_calls = zeta_event_store().list_events(Filter(event_type="zeta.tool.called"))
    assert assistant is not None
    assert assistant.event_type == "zeta.model.called"
    assert len(tool_calls) == 1
    assert tool_calls[0].caused_by == "assistant-event-1"
    assert tool_calls[0].payload["name"] == "read"


def test_zeta_model_called_links_used_and_returned_objects(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ZETA_SESSION_ID", "zeta-test")
    runtime_context = zeta_runtime_context()
    store = runtime_context.trace_store
    prompt_id = store.put_object(
        zeta_trace.Object(
            kind="prompt",
            schema="zeta.prompt.v1",
            data={"payload_sha256": "sha256:payload"},
        )
    )
    assistant_id = store.put_object(
        zeta_trace.Object(
            kind="assistant_message",
            schema="zeta.assistant_output.v1",
            data={"message": {"role": "assistant", "content": "the answer"}},
            links=(prompt_id,),
        )
    )
    call_id = store.put_object(
        zeta_trace.Object(
            kind="tool_call",
            schema="zeta.tool_call.v1",
            data={"tool_call_id": "call-1", "name": "read", "input": {}},
            links=(assistant_id,),
        )
    )

    record_zeta_event(
        {
            "type": "model",
            "id": "model-event-1",
            "content": "the answer",
            "prompt_trace": {
                "prompt_object_id": prompt_id,
                "assistant_message_object_id": assistant_id,
            },
            "tool_call_object_ids": [call_id],
        },
        runtime_context=runtime_context,
    )

    event = zeta_event_store().get("model-event-1")
    assert event is not None
    assert event.event_type == "zeta.model.called"
    assert event.payload["used_objects"] == [
        {"kind": "prompt", "id": prompt_id},
    ]
    assert event.payload["returned_objects"] == [
        {"kind": "assistant_message", "id": assistant_id},
        {"kind": "tool_call", "id": call_id},
    ]


def test_zeta_tool_called_links_used_and_returned_objects(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ZETA_SESSION_ID", "zeta-test")

    record_zeta_event(
        {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "name": "read",
            "result": {"ok": True},
            "tool_call_object_id": "sha256:call",
            "tool_result_object_id": "sha256:result",
            "caused_by": "model-event-1",
        }
    )

    events = zeta_event_store().list_events(Filter(event_type="zeta.tool.called"))
    assert len(events) == 1
    assert events[0].caused_by == "model-event-1"
    assert events[0].payload["used_objects"] == [
        {"kind": "tool_call", "id": "sha256:call"},
    ]
    assert events[0].payload["returned_objects"] == [
        {"kind": "tool_result", "id": "sha256:result"},
    ]


def test_zeta_agent_durable_events_link_trace_objects(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ZETA_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ZETA_SESSION_ID", "zeta-test")
    runtime_context = zeta_runtime_context()
    target = tmp_path / "README.md"
    target.write_text("README\n", encoding="utf-8")
    responses = iter([read_tool_call_response(target), {"content": "done"}])

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        lambda *args, **kwargs: next(responses),
    )
    monkeypatch.setattr(
        zeta_agent,
        "invoke_capability",
        lambda name, params, **kwargs: read_tool_payload(target),
    )

    def record_runtime_event(event: dict[str, object]) -> None:
        record_zeta_event(event, runtime_context=runtime_context)

    result = zeta_agent.run_agent_turn(
        "inspect",
        [],
        zeta_agent.AgentConfig(allowed_capabilities=("read",), max_turns=2),
        event_sink=record_runtime_event,
        prompt_builder=zeta_context.PromptBuilder(store=runtime_context.trace_store),
    )

    tool_call = event_by_type(result.events, "tool_call")
    tool_result = event_by_type(result.events, "tool_result")
    model_events = zeta_event_store().list_events(
        Filter(event_type="zeta.model.called")
    )
    tool_events = zeta_event_store().list_events(Filter(event_type="zeta.tool.called"))

    assert len(model_events) == 2
    assert model_events[0].payload["used_objects"] == [
        {"kind": "prompt", "id": result.prompt_traces[0].prompt_object_id},
    ]
    assert model_events[0].payload["returned_objects"] == [
        {
            "kind": "assistant_message",
            "id": result.prompt_traces[0].assistant_message_object_id,
        },
        {"kind": "tool_call", "id": tool_call["tool_call_object_id"]},
    ]
    assert model_events[1].payload["used_objects"] == [
        {"kind": "prompt", "id": result.prompt_traces[1].prompt_object_id},
    ]
    assert model_events[1].payload["returned_objects"] == [
        {
            "kind": "assistant_message",
            "id": result.prompt_traces[1].assistant_message_object_id,
        },
    ]
    assert [event.payload["_timeline_type"] for event in tool_events] == [
        "tool_call",
        "tool_result",
    ]
    assert tool_events[1].payload["used_objects"] == [
        {"kind": "tool_call", "id": tool_call["tool_call_object_id"]},
    ]
    assert tool_events[1].payload["returned_objects"] == [
        {"kind": "tool_result", "id": tool_result["tool_result_object_id"]},
    ]


def test_zeta_user_message_usage_and_abort_are_durable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ZETA_SESSION_ID", "zeta-test")

    record_zeta_event({"type": "user_message", "content": "hello"})
    record_zeta_event({"type": "model_usage", "usage": {"tokens": 1}})
    record_zeta_event({"type": "turn_aborted", "content": "stopped"})

    events = zeta_event_store().list_events(Filter())
    assert [event.event_type for event in events] == [
        "zeta.user_message",
        "zeta.model_usage",
        "zeta.turn_aborted",
    ]
    assert [event.payload.get("content") for event in events] == [
        "hello",
        None,
        "stopped",
    ]


def test_zeta_current_timeline_uses_durable_log_without_trace_head(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ZETA_SESSION_ID", "zeta-test")
    runtime_context = zeta_runtime_context()

    record_zeta_event(
        {"type": "user_message", "content": "durable"},
        runtime_context=runtime_context,
    )

    events = zeta_timeline.current_timeline(runtime_context=runtime_context)
    assert [event["content"] for event in events] == ["durable"]
    assert_no_trace_timeline_chain(runtime_context.trace_store)


def test_zeta_timeline_projects_fresh_session_from_durable_log(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ZETA_SESSION_ID", "zeta-test")
    runtime_context = zeta_runtime_context()

    record_zeta_event(
        {"type": "user_message", "content": "first", "id": "user-1"},
        runtime_context=runtime_context,
    )
    record_zeta_event(
        {"type": "model", "content": "second", "id": "model-1"},
        runtime_context=runtime_context,
    )
    record_zeta_event(
        {
            "type": "tool_call",
            "id": "call-1",
            "tool_call_id": "call-1",
            "name": "read",
            "input": {"path": "README.md"},
            "caused_by": "model-1",
        },
        runtime_context=runtime_context,
    )
    record_zeta_event(
        {
            "type": "tool_result",
            "id": "result-1",
            "tool_call_id": "call-1",
            "name": "read",
            "result": {"ok": True},
            "caused_by": "call-1",
        },
        runtime_context=runtime_context,
    )
    record_zeta_event(
        {"type": "model_usage", "usage": {"tokens": 4}},
        runtime_context=runtime_context,
    )
    record_zeta_event(
        {"type": "turn_aborted", "content": "stopped"},
        runtime_context=runtime_context,
    )

    events = zeta_timeline.current_timeline(runtime_context=runtime_context)

    assert [event["type"] for event in events] == [
        "user_message",
        "model",
        "tool_call",
        "tool_result",
        "model_usage",
        "turn_aborted",
    ]
    assert [event["id"] for event in events[:4]] == [
        "user-1",
        "model-1",
        "call-1",
        "result-1",
    ]
    assert events[2]["input"] == {"path": "README.md"}
    assert events[3]["result"] == {"ok": True}
    assert events[4]["usage"] == {"tokens": 4}
    assert events[5]["content"] == "stopped"
    assert_no_trace_timeline_chain(runtime_context.trace_store)


def test_zeta_record_event_stores_prompt_link_not_components(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ZETA_SESSION_ID", "zeta-test")
    runtime_context = zeta_runtime_context()
    store = runtime_context.trace_store
    component_id = store.put_object(
        zeta_trace.Object(
            kind="user_message",
            schema="zeta.prompt_component.v1",
            data={"message": {"role": "user", "content": "objective"}},
        )
    )
    prompt_id = store.put_object(
        zeta_trace.Object(
            kind="prompt",
            schema="zeta.prompt.v1",
            data={"payload_sha256": "sha256:payload"},
            links=(component_id,),
        )
    )
    assistant_id = store.put_object(
        zeta_trace.Object(
            kind="assistant_message",
            schema="zeta.assistant_output.v1",
            data={"message": {"role": "assistant", "content": "the answer"}},
            links=(prompt_id,),
        )
    )

    record_zeta_event(
        {
            "type": "model",
            "content": "the answer",
            "prompt_trace": {
                "prompt_object_id": prompt_id,
                "assistant_message_object_id": assistant_id,
                "component_object_ids": [component_id],
            },
        },
        runtime_context=runtime_context,
    )

    assert_no_trace_timeline_chain(store)
    (event,) = zeta_event_store().list_events(Filter(event_type="zeta.model.called"))
    assert event.payload["used_objects"] == [{"kind": "prompt", "id": prompt_id}]
    assert event.payload["returned_objects"] == [
        {"kind": "assistant_message", "id": assistant_id}
    ]
    assert "prompt_trace" not in event.payload


def test_zeta_timeline_rehydrates_assistant_content_from_the_graph(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ZETA_SESSION_ID", "zeta-test")
    runtime_context = zeta_runtime_context()
    store = runtime_context.trace_store
    prompt_id = store.put_object(
        zeta_trace.Object(
            kind="prompt",
            schema="zeta.prompt.v1",
            data={"payload_sha256": "sha256:payload"},
        )
    )
    tool_calls = [
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "read", "arguments": "{}"},
        }
    ]
    assistant_id = store.put_object(
        zeta_trace.Object(
            kind="assistant_message",
            schema="zeta.assistant_output.v1",
            data={
                "message": {
                    "role": "assistant",
                    "content": "the answer",
                    "tool_calls": tool_calls,
                }
            },
            links=(prompt_id,),
        )
    )

    record_zeta_event(
        {
            "type": "model",
            "content": "the answer",
            "tool_calls": tool_calls,
            "prompt_trace": {
                "prompt_object_id": prompt_id,
                "assistant_message_object_id": assistant_id,
            },
        },
        runtime_context=runtime_context,
    )

    events = zeta_timeline.current_timeline(runtime_context=runtime_context)
    assert events[-1]["type"] == "model"
    assert events[-1]["content"] == "the answer"
    assert events[-1]["tool_calls"] == tool_calls
    assert_no_trace_timeline_chain(store)


def test_zeta_timeline_rehydrates_assistant_reasoning_from_the_graph(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ZETA_SESSION_ID", "zeta-test")
    runtime_context = zeta_runtime_context()
    store = runtime_context.trace_store
    prompt_id = store.put_object(
        zeta_trace.Object(
            kind="prompt",
            schema="zeta.prompt.v1",
            data={"payload_sha256": "sha256:payload"},
        )
    )
    assistant_id = store.put_object(
        zeta_trace.Object(
            kind="assistant_message",
            schema="zeta.assistant_output.v1",
            data={
                "message": {
                    "role": "assistant",
                    "content": "the answer",
                    "reasoning_content": "weighing the options",
                }
            },
            links=(prompt_id,),
        )
    )

    record_zeta_event(
        {
            "type": "model",
            "content": "the answer",
            "reasoning": "weighing the options",
            "prompt_trace": {
                "prompt_object_id": prompt_id,
                "assistant_message_object_id": assistant_id,
            },
        },
        runtime_context=runtime_context,
    )

    events = zeta_timeline.current_timeline(runtime_context=runtime_context)
    assert events[-1]["content"] == "the answer"
    assert events[-1]["reasoning"] == "weighing the options"

    assert_no_trace_timeline_chain(store)

    messages = chat_messages(events)
    assert "reasoning" not in messages[-1]
    assert "reasoning_content" not in messages[-1]


def test_zeta_timeline_keeps_untraced_assistant_content_inline(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ZETA_SESSION_ID", "zeta-test")
    runtime_context = zeta_runtime_context()

    record_zeta_event(
        {"type": "model", "content": "fallback"},
        runtime_context=runtime_context,
    )

    store = runtime_context.trace_store
    assert_no_trace_timeline_chain(store)
    assert (
        zeta_timeline.current_timeline(runtime_context=runtime_context)[-1]["content"]
        == "fallback"
    )


def test_zeta_timeline_last_event_time_tracks_the_newest_event(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ZETA_SESSION_ID", "zeta-test")
    runtime_context = zeta_runtime_context()

    assert (
        zeta_timeline.last_event_time(
            store=runtime_context.trace_store,
            run_id=runtime_context.session_id,
        )
        is None
    )

    first = record_zeta_event(
        {"type": "user_message", "content": "hi"},
        runtime_context=runtime_context,
    )
    assert (
        zeta_timeline.last_event_time(
            store=runtime_context.trace_store,
            run_id=runtime_context.session_id,
        )
        == first["time"]
    )

    second = record_zeta_event(
        {"type": "model", "content": "yo"},
        runtime_context=runtime_context,
    )
    assert (
        zeta_timeline.last_event_time(
            store=runtime_context.trace_store,
            run_id=runtime_context.session_id,
        )
        == second["time"]
    )


def test_zeta_inmemory_store_dedupes_repeated_derivations() -> None:
    store = zeta_trace.InMemoryStore()
    derivation = zeta_trace.Derivation(
        producer="Producer:v1",
        output_id="sha256:out",
        input_ids=("sha256:in",),
    )

    first = store.record_derivation(derivation)
    second = store.record_derivation(derivation)

    assert first == second
    assert len(store.derivations_for_output("sha256:out")) == 1


def test_zeta_orphan_tool_result_rendering_strips_trace_fields() -> None:
    event = {
        "type": "tool_result",
        "tool_call_id": "call-orphan",
        "name": "read",
        "result": {"ok": True, "content": [{"type": "text", "text": "data"}]},
        "tool_result_object_id": "sha256:result",
        "tool_call_object_id": "sha256:call",
        "model_telemetry": {"usage": {"prompt_tokens": 10}},
        "prompt_trace": {"prompt_object_id": "sha256:prompt"},
    }

    messages = chat_messages([event])

    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    content = str(messages[0]["content"])
    assert "data" in content
    assert "sha256:" not in content
    assert "model_telemetry" not in content
    assert "prompt_trace" not in content


def test_zeta_chat_messages_repairs_truncated_tool_call_arguments() -> None:
    event = {
        "type": "model",
        "content": "",
        "tool_calls": [
            {
                "id": "call-0",
                "type": "function",
                "function": {
                    "name": "write",
                    "arguments": '{"path": "doc.md", "content": "cut mid stri',
                },
            }
        ],
    }

    messages = chat_messages([event])

    assert len(messages) == 1
    call = messages[0]["tool_calls"][0]
    arguments = json.loads(call["function"]["arguments"])
    assert arguments["truncated_arguments"].startswith('{"path": "doc.md"')
    assert call["id"] == "call-0"
    assert call["function"]["name"] == "write"


def test_zeta_chat_messages_keeps_valid_tool_call_arguments() -> None:
    event = {
        "type": "model",
        "content": "",
        "tool_calls": [
            {
                "id": "call-0",
                "type": "function",
                "function": {"name": "read", "arguments": '{"path": "doc.md"}'},
            }
        ],
    }

    messages = chat_messages([event])

    assert messages[0]["tool_calls"][0]["function"]["arguments"] == (
        '{"path": "doc.md"}'
    )


def test_zeta_sqlite_store_batch_defers_commit(tmp_path: Path) -> None:
    store = zeta_trace.SqliteStore(tmp_path / "trace.sqlite3")
    reader = zeta_trace.SqliteStore(tmp_path / "trace.sqlite3")

    with store.batch():
        object_id = store.put_object(
            zeta_trace.Object(kind="value", schema="v1", data={"n": 1})
        )
        assert reader.get_object(object_id) is None

    assert reader.get_object(object_id) is not None
    store.close()
    reader.close()


def test_zeta_record_event_does_not_write_trace_timeline_batch() -> None:
    store = BatchSpyStore()

    record_zeta_event(
        {"type": "user_message", "content": "hello"},
        runtime_context=zeta_runtime_context(trace_store=store),
    )

    assert store.batches == 0
    assert_no_trace_timeline_chain(store, session_id="zeta")


def test_zeta_trace_sqlite_answers_forward_derivation_queries(
    tmp_path: Path,
) -> None:
    store = zeta_trace.SqliteStore(tmp_path / "trace.sqlite3")
    first_input = store.put_object(
        zeta_trace.Object(kind="component", schema="v1", data={"text": "a"})
    )
    second_input = store.put_object(
        zeta_trace.Object(kind="component", schema="v1", data={"text": "b"})
    )
    output = store.put_object(
        zeta_trace.Object(kind="prompt", schema="v1", data={"text": "out"})
    )
    store.record_derivation(
        zeta_trace.Derivation(
            producer="test:v1",
            output_id=output,
            input_ids=(first_input, second_input),
        )
    )

    (derivation,) = store.derivations_for_input(first_input)

    assert derivation.output_id == output
    assert derivation.input_ids == (first_input, second_input)
    assert store.derivations_for_input(output) == []
    store.close()


def test_zeta_inmemory_store_answers_forward_derivation_queries() -> None:
    store = zeta_trace.InMemoryStore()
    store.record_derivation(
        zeta_trace.Derivation(
            producer="test:v1",
            output_id="sha256:out",
            input_ids=("sha256:in",),
        )
    )

    (derivation,) = store.derivations_for_input("sha256:in")

    assert derivation.output_id == "sha256:out"
    assert store.derivations_for_input("sha256:out") == []


def test_zeta_trace_dedupes_forward_rows_for_repeated_derivations(
    tmp_path: Path,
) -> None:
    store = zeta_trace.SqliteStore(tmp_path / "trace.sqlite3")
    derivation = zeta_trace.Derivation(
        producer="test:v1",
        output_id="sha256:out",
        input_ids=("sha256:in", "sha256:in"),
    )

    store.record_derivation(derivation)
    store.record_derivation(derivation)

    assert len(store.derivations_for_input("sha256:in")) == 1
    store.close()


def test_zeta_trace_derivation_ids_are_content_scoped_across_sessions(
    tmp_path: Path,
) -> None:
    path = tmp_path / "trace.sqlite3"
    first = zeta_trace.SqliteStore(path, session_id="first")
    second = zeta_trace.SqliteStore(path, session_id="second")
    derivation = zeta_trace.Derivation(
        producer="test:v1",
        output_id="sha256:out",
        input_ids=("sha256:in",),
    )

    first_id = first.record_derivation(derivation)
    second_id = second.record_derivation(derivation)

    assert first_id == second_id == derivation.content_address()
    assert first.derivations_for_input("sha256:in") == [derivation]
    assert second.derivations_for_input("sha256:in") == [derivation]

    first.clear_session()

    assert first.derivations_for_input("sha256:in") == []
    assert second.derivations_for_input("sha256:in") == [derivation]
    first.close()
    second.close()


def test_zeta_trace_resolves_refs_full_ids_and_prefixes(tmp_path: Path) -> None:
    store = zeta_trace.SqliteStore(tmp_path / "trace.sqlite3")
    object_id = store.put_object(
        zeta_trace.Object(kind="prompt", schema="v1", data={"text": "target"})
    )
    store.move_ref("prompt/current", None, object_id)
    digest = object_id.removeprefix("sha256:")

    assert zeta_trace.resolve_object_id(store, "prompt/current") == object_id
    assert zeta_trace.resolve_object_id(store, object_id) == object_id
    assert zeta_trace.resolve_object_id(store, object_id[:15]) == object_id
    assert zeta_trace.resolve_object_id(store, digest[:8]) == object_id
    store.close()


def test_zeta_trace_resolver_prefers_refs_over_prefixes() -> None:
    store = zeta_trace.InMemoryStore()
    obj = zeta_trace.Object(kind="prompt", schema="v1", data={"text": "x"})
    store._objects["sha256:aaaa1111"] = obj
    store._objects["sha256:bbbb2222"] = obj
    store.move_ref("aaaa", None, "sha256:bbbb2222")

    assert zeta_trace.resolve_object_id(store, "aaaa") == "sha256:bbbb2222"


def test_zeta_trace_resolver_rejects_ambiguous_prefixes() -> None:
    store = zeta_trace.InMemoryStore()
    obj = zeta_trace.Object(kind="prompt", schema="v1", data={"text": "x"})
    store._objects["sha256:aaaa1111"] = obj
    store._objects["sha256:aaaa2222"] = obj

    with pytest.raises(zeta_trace.AmbiguousIdError) as excinfo:
        zeta_trace.resolve_object_id(store, "aaaa")

    assert set(excinfo.value.candidates) == {"sha256:aaaa1111", "sha256:aaaa2222"}


def test_zeta_trace_resolver_raises_for_unknown_tokens() -> None:
    store = zeta_trace.InMemoryStore()

    with pytest.raises(zeta_trace.UnknownIdError):
        zeta_trace.resolve_object_id(store, "missing")
    with pytest.raises(zeta_trace.UnknownIdError):
        zeta_trace.resolve_object_id(store, "")


def test_zeta_trace_prefix_matching_treats_like_wildcards_literally(
    tmp_path: Path,
) -> None:
    store = zeta_trace.SqliteStore(tmp_path / "trace.sqlite3")
    store.put_object(zeta_trace.Object(kind="prompt", schema="v1", data={"n": 1}))

    assert store.object_ids_with_prefix("sha256:%") == []
    assert store.object_ids_with_prefix("sha256:_") == []
    store.close()


def test_zeta_trace_lists_objects_by_derivation_recency(tmp_path: Path) -> None:
    store = zeta_trace.SqliteStore(tmp_path / "trace.sqlite3")
    old = store.put_object(
        zeta_trace.Object(kind="prompt", schema="v1", data={"text": "old"})
    )
    new = store.put_object(
        zeta_trace.Object(kind="prompt", schema="v1", data={"text": "new"})
    )
    underived = store.put_object(
        zeta_trace.Object(kind="component", schema="v1", data={"text": "loose"})
    )
    for output_id, created_at in ((old, 1.0), (new, 2.0)):
        derivation = zeta_trace.Derivation(producer="test:v1", output_id=output_id)
        store.connection.execute(
            """
            INSERT INTO derivations
              (id, producer, output_id, input_ids_json, params_json, created_at)
            VALUES (?, ?, ?, '[]', '{}', ?)
            """,
            (derivation.content_address(), "test:v1", output_id, created_at),
        )
    store.connection.commit()

    listed = [object_id for object_id, _ in store.objects()]

    assert listed == [new, old, underived]
    assert [object_id for object_id, _ in store.objects(kind="prompt")] == [new, old]
    assert [object_id for object_id, _ in store.objects(limit=1)] == [new]
    assert store.prompt_object_ids() == [new, old]
    store.close()


def test_zeta_inmemory_store_lists_objects_newest_first() -> None:
    store = zeta_trace.InMemoryStore()
    first = store.put_object(
        zeta_trace.Object(kind="prompt", schema="v1", data={"n": 1})
    )
    second = store.put_object(
        zeta_trace.Object(kind="component", schema="v1", data={"n": 2})
    )
    third = store.put_object(
        zeta_trace.Object(kind="prompt", schema="v1", data={"n": 3})
    )

    assert [object_id for object_id, _ in store.objects()] == [third, second, first]
    assert [object_id for object_id, _ in store.objects(kind="prompt", limit=1)] == [
        third
    ]
    assert store.prompt_object_ids() == [third, first]


def test_zeta_trace_sqlite_objects_filter_by_multiple_kinds(tmp_path: Path) -> None:
    store = zeta_trace.SqliteStore(tmp_path / "trace.sqlite3")
    prompt = store.put_object(
        zeta_trace.Object(kind="prompt", schema="v1", data={"n": 1})
    )
    answer = store.put_object(
        zeta_trace.Object(kind="assistant_message", schema="v1", data={"n": 2})
    )
    store.put_object(zeta_trace.Object(kind="tool_result", schema="v1", data={"n": 3}))

    listed = store.objects(kind=("prompt", "assistant_message"))

    assert {object_id for object_id, _ in listed} == {prompt, answer}
    assert len(store.objects(kind=("prompt", "assistant_message"), limit=1)) == 1
    store.close()


def test_zeta_inmemory_store_objects_filter_by_multiple_kinds() -> None:
    store = zeta_trace.InMemoryStore()
    prompt = store.put_object(
        zeta_trace.Object(kind="prompt", schema="v1", data={"n": 1})
    )
    answer = store.put_object(
        zeta_trace.Object(kind="assistant_message", schema="v1", data={"n": 2})
    )
    store.put_object(zeta_trace.Object(kind="tool_result", schema="v1", data={"n": 3}))

    listed = store.objects(kind=("prompt", "assistant_message"))

    assert [object_id for object_id, _ in listed] == [answer, prompt]
    assert [object_id for object_id, _ in store.objects(kind=("prompt",))] == [prompt]


def narrative_log_store() -> tuple[zeta_trace.InMemoryStore, str, str, str]:
    store = zeta_trace.InMemoryStore()
    component_id = store.put_object(
        zeta_trace.Object(
            kind="user_message",
            schema="zeta.prompt_component.v1",
            data={"message": {"role": "user", "content": "why did it fail?"}},
        )
    )
    prompt_id = store.put_object(
        zeta_trace.Object(
            kind="prompt",
            schema="zeta.prompt.v1",
            data={"payload_sha256": "sha256:feed"},
            links=(component_id,),
        )
    )
    answer_id = store.put_object(
        zeta_trace.Object(
            kind="assistant_message",
            schema="zeta.assistant_output.v1",
            data={
                "message": {
                    "role": "assistant",
                    "content": "the test imports a stale fixture",
                }
            },
            links=(prompt_id,),
        )
    )
    store.record_derivation(
        zeta_trace.Derivation(
            producer="PromptBuilder",
            output_id=prompt_id,
            input_ids=(component_id,),
        )
    )
    store.record_derivation(
        zeta_trace.Derivation(
            producer="ModelResponse",
            output_id=answer_id,
            input_ids=(prompt_id,),
        )
    )
    return store, component_id, prompt_id, answer_id


def prompt_diff_store() -> tuple[zeta_trace.InMemoryStore, dict[str, str]]:
    store = zeta_trace.InMemoryStore()

    def component(kind: str, role: str, content: str) -> str:
        return store.put_object(
            zeta_trace.Object(
                kind=kind,
                schema="zeta.prompt_component.v1",
                data={"message": {"role": role, "content": content}},
            )
        )

    ids = {
        "system": component("system_prompt", "system", "system text"),
        "old_objective": component("user_message", "user", "old objective line"),
        "new_objective": component("user_message", "user", "new objective line"),
        "transcript": component("assistant_message", "assistant", "shared transcript"),
    }
    ids["prompt_a"] = store.put_object(
        zeta_trace.Object(
            kind="prompt",
            schema="zeta.prompt.v1",
            data={"payload_sha256": "sha256:a"},
            links=(ids["system"], ids["transcript"], ids["old_objective"]),
        )
    )
    ids["prompt_b"] = store.put_object(
        zeta_trace.Object(
            kind="prompt",
            schema="zeta.prompt.v1",
            data={"payload_sha256": "sha256:b"},
            links=(ids["system"], ids["new_objective"]),
        )
    )
    return store, ids


def test_sigil_trace_helpers_read_provider_neutral_model_output() -> None:
    store = zeta_trace.InMemoryStore()
    prompt_id = store.put_object(
        zeta_trace.Object(
            kind="prompt",
            schema="zeta.prompt.v1",
            data={"payload_sha256": "sha256:prompt"},
        )
    )
    answer_id = store.put_object(
        zeta_trace.Object(
            kind="assistant_message",
            schema="zeta.model_output.v1",
            data={
                "model_output": {
                    "message": {
                        "role": "assistant",
                        "content": "neutral answer",
                    }
                }
            },
            links=(prompt_id,),
        )
    )
    store.record_derivation(
        zeta_trace.Derivation(
            producer="ModelResponse",
            output_id=answer_id,
            input_ids=(prompt_id,),
        )
    )

    assert (
        assistant_trace_summary(
            {
                "model_output": {
                    "message": {
                        "role": "assistant",
                        "content": "neutral answer",
                    }
                }
            }
        )
        == "neutral answer"
    )
    assert latest_model_answer(store, prompt_id) == (answer_id, "neutral answer")


def test_sigil_zeta_trace_diff_reports_component_changes(monkeypatch) -> None:
    store, ids = prompt_diff_store()
    monkeypatch.setattr("sigil.cli.trace.current_store", lambda: store)

    result = CliRunner().invoke(
        sigil_cli, ["trace", "diff", ids["prompt_a"], ids["prompt_b"]]
    )

    assert result.exit_code == 0
    output = result.output
    changed_line = next(
        line for line in output.splitlines() if line.startswith("~ user_message")
    )
    assert zeta_trace_short(ids["old_objective"]) in changed_line
    assert zeta_trace_short(ids["new_objective"]) in changed_line
    assert "-old objective line" in output
    assert "+new objective line" in output
    removed_line = next(
        line for line in output.splitlines() if line.startswith("- assistant_message")
    )
    assert zeta_trace_short(ids["transcript"]) in removed_line
    assert "= 1 unchanged" in output
    assert zeta_trace_short(ids["system"]) not in output


def test_sigil_zeta_trace_diff_stat_keeps_one_line_per_change(monkeypatch) -> None:
    store, ids = prompt_diff_store()
    monkeypatch.setattr("sigil.cli.trace.current_store", lambda: store)

    result = CliRunner().invoke(
        sigil_cli,
        ["trace", "diff", "--stat", ids["prompt_a"], ids["prompt_b"]],
    )

    assert result.exit_code == 0
    assert "~ user_message" in result.output
    assert "- assistant_message" in result.output
    assert "+new objective line" not in result.output


def test_sigil_zeta_trace_diff_requires_prompt_objects(monkeypatch) -> None:
    store, ids = prompt_diff_store()
    monkeypatch.setattr("sigil.cli.trace.current_store", lambda: store)

    result = CliRunner().invoke(
        sigil_cli, ["trace", "diff", ids["prompt_a"], ids["system"]]
    )

    assert result.exit_code != 0
    assert "not a prompt" in result.output


def test_sigil_zeta_trace_replay_records_a_traced_answer(monkeypatch) -> None:
    store, component_id, prompt_id, answer_id = narrative_log_store()
    monkeypatch.setattr("sigil.cli.trace.current_store", lambda: store)
    captured: dict[str, object] = {}

    def fake_chat(messages, **kwargs):
        captured["messages"] = messages
        captured.update(kwargs)
        return {"role": "assistant", "content": "a fresh answer"}

    monkeypatch.setattr("sigil.cli.trace.chat_completion_messages", fake_chat)

    result = CliRunner().invoke(sigil_cli, ["trace", "replay", prompt_id])

    assert result.exit_code == 0
    assert "the test imports a stale fixture" in result.output
    assert "a fresh answer" in result.output
    assert captured["messages"] == [{"role": "user", "content": "why did it fail?"}]
    replays = [
        derivation
        for derivation in store.derivations_for_input(prompt_id)
        if derivation.producer == "ModelReplay"
    ]
    assert len(replays) == 1
    replay_object = store.get_object(replays[0].output_id)
    assert replay_object is not None
    assert replay_object.kind == "assistant_message"
    assert replay_object.schema == "zeta.model_output.v1"
    assert replay_object.data["message"]["content"] == "a fresh answer"
    assert replay_object.data["model_output"]["message"]["content"] == "a fresh answer"
    assert replay_object.links == (prompt_id,)
    assert replays[0].output_id != answer_id


def test_sigil_zeta_trace_replay_diffs_old_and_new(monkeypatch) -> None:
    store, _, prompt_id, _ = narrative_log_store()
    monkeypatch.setattr("sigil.cli.trace.current_store", lambda: store)
    monkeypatch.setattr(
        "sigil.cli.trace.chat_completion_messages",
        lambda messages, **kwargs: {"role": "assistant", "content": "a fresh answer"},
    )

    result = CliRunner().invoke(sigil_cli, ["trace", "replay", "--diff", prompt_id])

    assert result.exit_code == 0
    assert "-the test imports a stale fixture" in result.output
    assert "+a fresh answer" in result.output


def test_sigil_zeta_trace_replay_renders_tool_call_answers(monkeypatch) -> None:
    store, _, prompt_id, _ = narrative_log_store()
    monkeypatch.setattr("sigil.cli.trace.current_store", lambda: store)
    monkeypatch.setattr(
        "sigil.cli.trace.chat_completion_messages",
        lambda messages, **kwargs: {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "read", "arguments": "{}"},
                }
            ],
        },
    )

    result = CliRunner().invoke(sigil_cli, ["trace", "replay", prompt_id])

    assert result.exit_code == 0
    assert "→ read" in result.output


def test_sigil_zeta_trace_replay_honors_a_named_profile(monkeypatch) -> None:
    store, _, prompt_id, _ = narrative_log_store()
    monkeypatch.setattr("sigil.cli.trace.current_store", lambda: store)
    captured: dict[str, object] = {}

    def fake_chat(messages, **kwargs):
        captured.update(kwargs)
        return {"role": "assistant", "content": "a fresh answer"}

    monkeypatch.setattr("sigil.cli.trace.chat_completion_messages", fake_chat)
    monkeypatch.setattr(
        "sigil.trace.replay.resolve_model_profile",
        lambda name: (
            zeta_models.ModelSelection(
                profile=name,
                model="fast-model",
                url="http://127.0.0.1:8081/v1/chat/completions",
            )
            if name == "fast"
            else None
        ),
    )

    ok = CliRunner().invoke(
        sigil_cli, ["trace", "replay", "--model", "fast", prompt_id]
    )
    unknown = CliRunner().invoke(
        sigil_cli, ["trace", "replay", "--model", "nope", prompt_id]
    )

    assert ok.exit_code == 0
    assert captured["selected_model"] == "fast-model"
    assert captured["selected_url"] == "http://127.0.0.1:8081/v1/chat/completions"
    assert unknown.exit_code != 0
    assert "unknown model profile" in unknown.output


def test_sigil_zeta_trace_log_defaults_to_the_narrative_kinds(monkeypatch) -> None:
    store, component_id, prompt_id, answer_id = narrative_log_store()
    monkeypatch.setattr("sigil.cli.trace.current_store", lambda: store)

    result = CliRunner().invoke(sigil_cli, ["trace", "log"])

    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith(zeta_trace_short(answer_id))
    assert "assistant_message" in lines[0]
    assert "stale fixture" in lines[0]
    assert lines[1].startswith(zeta_trace_short(prompt_id))
    assert "1 component" in lines[1]
    assert zeta_trace_short(component_id) not in result.output


def test_sigil_zeta_trace_log_widens_with_kind_and_all(monkeypatch) -> None:
    store, component_id, _, answer_id = narrative_log_store()
    monkeypatch.setattr("sigil.cli.trace.current_store", lambda: store)
    runner = CliRunner()

    only_components = runner.invoke(
        sigil_cli, ["trace", "log", "--kind", "user_message"]
    )
    everything = runner.invoke(sigil_cli, ["trace", "log", "--all"])
    limited = runner.invoke(sigil_cli, ["trace", "log", "--all", "--limit", "1"])

    assert only_components.exit_code == 0
    assert only_components.output.splitlines() == [
        line
        for line in only_components.output.splitlines()
        if line.startswith(zeta_trace_short(component_id))
    ]
    assert everything.exit_code == 0
    assert len(everything.output.splitlines()) == 3
    assert limited.exit_code == 0
    assert len(limited.output.splitlines()) == 1
    assert limited.output.startswith(zeta_trace_short(answer_id))


def test_sigil_zeta_trace_tools_json_joins_calls_and_results(monkeypatch) -> None:
    store = zeta_trace.InMemoryStore()
    ok_call_id = store.put_object(
        zeta_trace.Object(
            kind="tool_call",
            schema="zeta.tool_call.v1",
            data={
                "tool_call_id": "call-ok",
                "name": "read",
                "input": {"path": "README.md"},
            },
        )
    )
    store.put_object(
        zeta_trace.Object(
            kind="tool_result",
            schema="zeta.tool_result.v1",
            data={
                "tool_call_id": "call-ok",
                "name": "read",
                "result": {"ok": True, "metadata": {"path": "README.md"}},
            },
            links=(ok_call_id,),
        )
    )
    failed_call_id = store.put_object(
        zeta_trace.Object(
            kind="tool_call",
            schema="zeta.tool_call.v1",
            data={
                "tool_call_id": "call-fail",
                "name": "read",
                "input": {"path": "missing.md"},
            },
        )
    )
    failed_result_id = store.put_object(
        zeta_trace.Object(
            kind="tool_result",
            schema="zeta.tool_result.v1",
            data={
                "tool_call_id": "call-fail",
                "name": "read",
                "result": {
                    "ok": False,
                    "error": {"code": "read-failed", "message": "missing.md"},
                },
            },
            links=(failed_call_id,),
        )
    )
    monkeypatch.setattr("sigil.cli.trace.current_store", lambda: store)

    result = CliRunner().invoke(sigil_cli, ["trace", "tools", "--json"])

    assert result.exit_code == 0
    rows = json.loads(result.output)
    assert [row["tool_call_id"] for row in rows] == ["call-fail", "call-ok"]
    assert rows[0]["ok"] is False
    assert rows[0]["error"]["code"] == "read-failed"
    assert rows[0]["error"]["message"] == "missing.md"
    assert rows[0]["tool_call_object_id"] == failed_call_id
    assert rows[0]["tool_result_object_id"] == failed_result_id
    assert rows[1]["ok"] is True
    assert "error" not in rows[1]
    assert rows[1]["input"] == {"path": "README.md"}


def test_sigil_zeta_trace_tools_failed_filters_json(monkeypatch) -> None:
    store = zeta_trace.InMemoryStore()
    store.put_object(
        zeta_trace.Object(
            kind="tool_call",
            schema="zeta.tool_call.v1",
            data={"tool_call_id": "call-ok", "name": "grep", "input": {}},
        )
    )
    store.put_object(
        zeta_trace.Object(
            kind="tool_result",
            schema="zeta.tool_result.v1",
            data={"tool_call_id": "call-ok", "name": "grep", "result": {"ok": True}},
        )
    )
    failed_call_id = store.put_object(
        zeta_trace.Object(
            kind="tool_call",
            schema="zeta.tool_call.v1",
            data={"tool_call_id": "call-fail", "name": "edit", "input": {}},
        )
    )
    store.put_object(
        zeta_trace.Object(
            kind="tool_result",
            schema="zeta.tool_result.v1",
            data={
                "tool_call_id": "call-fail",
                "name": "edit",
                "result": {
                    "ok": False,
                    "error": {"code": "old-text-not-found", "message": "missing"},
                },
            },
            links=(failed_call_id,),
        )
    )
    monkeypatch.setattr("sigil.cli.trace.current_store", lambda: store)

    result = CliRunner().invoke(sigil_cli, ["trace", "tools", "--failed", "--json"])

    assert result.exit_code == 0
    rows = json.loads(result.output)
    assert len(rows) == 1
    assert rows[0]["tool_call_id"] == "call-fail"
    assert rows[0]["ok"] is False


def test_sigil_zeta_trace_tools_failed_json_recovers_content_error(
    monkeypatch,
) -> None:
    store = zeta_trace.InMemoryStore()
    call_id = store.put_object(
        zeta_trace.Object(
            kind="tool_call",
            schema="zeta.tool_call.v1",
            data={"tool_call_id": "call-fail", "name": "grep", "input": {}},
        )
    )
    store.put_object(
        zeta_trace.Object(
            kind="tool_result",
            schema="zeta.tool_result.v1",
            data={
                "tool_call_id": "call-fail",
                "name": "grep",
                "result": {
                    "ok": False,
                    "content": [
                        {
                            "type": "text",
                            "text": "rg: src tests: No such file or directory" * 8,
                        }
                    ],
                    "metadata": {"status": 2},
                },
            },
            links=(call_id,),
        )
    )
    monkeypatch.setattr("sigil.cli.trace.current_store", lambda: store)

    result = CliRunner().invoke(sigil_cli, ["trace", "tools", "--failed", "--json"])

    assert result.exit_code == 0
    rows = json.loads(result.output)
    assert rows[0]["error"]["code"] == "grep-failed"
    assert rows[0]["error"]["message"].startswith(
        "rg: src tests: No such file or directory"
    )


def test_sigil_zeta_trace_tools_failed_json_recovers_bash_error(
    monkeypatch,
) -> None:
    store = zeta_trace.InMemoryStore()
    call_id = store.put_object(
        zeta_trace.Object(
            kind="tool_call",
            schema="zeta.tool_call.v1",
            data={"tool_call_id": "call-fail", "name": "bash", "input": {}},
        )
    )
    store.put_object(
        zeta_trace.Object(
            kind="tool_result",
            schema="zeta.tool_result.v1",
            data={
                "tool_call_id": "call-fail",
                "name": "bash",
                "result": {
                    "ok": False,
                    "content": [
                        {
                            "type": "text",
                            "text": "$ run something\nexit 1\nstderr:\nTraceback\nValueError: bad input",
                        }
                    ],
                    "metadata": {"status": 1},
                },
            },
            links=(call_id,),
        )
    )
    monkeypatch.setattr("sigil.cli.trace.current_store", lambda: store)

    result = CliRunner().invoke(sigil_cli, ["trace", "tools", "--failed", "--json"])

    assert result.exit_code == 0
    rows = json.loads(result.output)
    assert rows[0]["error"] == {
        "code": "bash-failed",
        "message": "ValueError: bad input",
    }


def test_sigil_zeta_trace_tools_failed_plain_output_uses_uniform_error(
    monkeypatch,
) -> None:
    store = zeta_trace.InMemoryStore()
    call_id = store.put_object(
        zeta_trace.Object(
            kind="tool_call",
            schema="zeta.tool_call.v1",
            data={"tool_call_id": "call-fail", "name": "bash", "input": {}},
        )
    )
    store.put_object(
        zeta_trace.Object(
            kind="tool_result",
            schema="zeta.tool_result.v1",
            data={
                "tool_call_id": "call-fail",
                "name": "bash",
                "result": {
                    "ok": False,
                    "content": [
                        {
                            "type": "text",
                            "text": "$ run something\nexit 1\nstderr:\nTraceback\nValueError: bad input",
                        }
                    ],
                    "metadata": {"status": 1},
                },
            },
            links=(call_id,),
        )
    )
    monkeypatch.setattr("sigil.cli.trace.current_store", lambda: store)

    result = CliRunner().invoke(sigil_cli, ["trace", "tools", "--failed"])

    assert result.exit_code == 0
    assert "failed · bash-failed: ValueError: bad input" in result.output
    assert "$ run something" not in result.output


def test_sigil_zeta_trace_tools_successful_filters_json(monkeypatch) -> None:
    store = zeta_trace.InMemoryStore()
    store.put_object(
        zeta_trace.Object(
            kind="tool_call",
            schema="zeta.tool_call.v1",
            data={"tool_call_id": "call-ok", "name": "read", "input": {}},
        )
    )
    store.put_object(
        zeta_trace.Object(
            kind="tool_result",
            schema="zeta.tool_result.v1",
            data={"tool_call_id": "call-ok", "name": "read", "result": {"ok": True}},
        )
    )
    store.put_object(
        zeta_trace.Object(
            kind="tool_call",
            schema="zeta.tool_call.v1",
            data={"tool_call_id": "call-fail", "name": "read", "input": {}},
        )
    )
    store.put_object(
        zeta_trace.Object(
            kind="tool_result",
            schema="zeta.tool_result.v1",
            data={
                "tool_call_id": "call-fail",
                "name": "read",
                "result": {"ok": False},
            },
        )
    )
    monkeypatch.setattr("sigil.cli.trace.current_store", lambda: store)

    result = CliRunner().invoke(sigil_cli, ["trace", "tools", "--successful", "--json"])

    assert result.exit_code == 0
    rows = json.loads(result.output)
    assert len(rows) == 1
    assert rows[0]["tool_call_id"] == "call-ok"
    assert rows[0]["ok"] is True


def test_sigil_zeta_trace_tools_status_filters_conflict() -> None:
    result = CliRunner().invoke(
        sigil_cli, ["trace", "tools", "--failed", "--successful"]
    )

    assert result.exit_code != 0
    assert "--failed conflicts with --successful" in result.output


def test_sigil_zeta_trace_tools_all_sessions_sorts_by_trace_time(
    monkeypatch,
    tmp_path: Path,
) -> None:
    def seed_tool_result(session: str, call_id: str, created_at: float) -> None:
        store = zeta_trace.SqliteStore(tmp_path / "zeta.sqlite3", session_id=session)
        call_object_id = store.put_object(
            zeta_trace.Object(
                kind="tool_call",
                schema="zeta.tool_call.v1",
                data={"tool_call_id": call_id, "name": "read", "input": {}},
            )
        )
        result_object_id = store.put_object(
            zeta_trace.Object(
                kind="tool_result",
                schema="zeta.tool_result.v1",
                data={
                    "tool_call_id": call_id,
                    "name": "read",
                    "result": {"ok": True},
                },
                links=(call_object_id,),
            )
        )
        store.import_derivation(
            f"derivation:{call_id}",
            zeta_trace.Derivation(
                producer="unit:test",
                output_id=result_object_id,
                input_ids=(call_object_id,),
            ),
            created_at,
        )
        store.close()

    seed_tool_result("old", "call-old", 10.0)
    seed_tool_result("new", "call-new", 20.0)

    monkeypatch.setattr("sigil.cli.trace.available_session_ids", lambda: ["old", "new"])
    monkeypatch.setattr(
        "sigil.cli.trace.open_session_store",
        lambda session: zeta_trace.SqliteStore(
            tmp_path / "zeta.sqlite3",
            session_id=session,
            read_only=True,
        ),
    )

    result = CliRunner().invoke(
        sigil_cli,
        ["trace", "tools", "--all-sessions", "--json", "--limit", "2"],
    )

    assert result.exit_code == 0
    rows = json.loads(result.output)
    assert [row["tool_call_id"] for row in rows] == ["call-new", "call-old"]


def zeta_trace_short(object_id: str) -> str:
    return object_id.split(":", 1)[-1][:8]


def test_sigil_zeta_trace_tree_walks_producers_by_default(monkeypatch) -> None:
    store, component_id, prompt_id, answer_id = narrative_log_store()
    monkeypatch.setattr("sigil.cli.trace.current_store", lambda: store)

    result = CliRunner().invoke(sigil_cli, ["trace", "tree", answer_id])

    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert lines[0].startswith(zeta_trace_short(answer_id))
    assert "ModelResponse" in result.output
    assert zeta_trace_short(prompt_id) in result.output
    assert "PromptBuilder" in result.output
    assert zeta_trace_short(component_id) in result.output
    assert "why did it fail?" in result.output


def test_sigil_zeta_trace_tree_walks_consumers_with_down(monkeypatch) -> None:
    store, component_id, prompt_id, answer_id = narrative_log_store()
    monkeypatch.setattr("sigil.cli.trace.current_store", lambda: store)

    result = CliRunner().invoke(sigil_cli, ["trace", "tree", "--down", component_id])

    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert lines[0].startswith(zeta_trace_short(component_id))
    assert "PromptBuilder" in result.output
    assert zeta_trace_short(prompt_id) in result.output
    assert "ModelResponse" in result.output
    assert zeta_trace_short(answer_id) in result.output


def test_sigil_zeta_trace_tree_respects_depth(monkeypatch) -> None:
    store, component_id, prompt_id, answer_id = narrative_log_store()
    monkeypatch.setattr("sigil.cli.trace.current_store", lambda: store)

    result = CliRunner().invoke(sigil_cli, ["trace", "tree", "--depth", "1", answer_id])

    assert result.exit_code == 0
    assert zeta_trace_short(prompt_id) in result.output
    assert zeta_trace_short(component_id) not in result.output


def test_sigil_zeta_trace_show_renders_humans_first(monkeypatch) -> None:
    store, component_id, prompt_id, answer_id = narrative_log_store()
    monkeypatch.setattr("sigil.cli.trace.current_store", lambda: store)

    result = CliRunner().invoke(sigil_cli, ["trace", "show", prompt_id])

    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert lines[0].startswith(zeta_trace_short(prompt_id))
    assert "prompt" in lines[0]
    assert f"id      {prompt_id}" in result.output
    assert "schema  zeta.prompt.v1" in result.output
    assert "components" in result.output
    assert zeta_trace_short(component_id) in result.output
    assert "why did it fail?" in result.output
    assert "produced by" in result.output
    assert "PromptBuilder" in result.output
    assert "consumed by" in result.output
    assert "ModelResponse" in result.output
    assert zeta_trace_short(answer_id) in result.output


def test_sigil_zeta_trace_show_renders_message_bodies(monkeypatch) -> None:
    store, _, _, answer_id = narrative_log_store()
    monkeypatch.setattr("sigil.cli.trace.current_store", lambda: store)

    result = CliRunner().invoke(sigil_cli, ["trace", "show", answer_id])

    assert result.exit_code == 0
    assert "the test imports a stale fixture" in result.output
    assert "produced by" in result.output
    assert "consumed by" not in result.output


def test_sigil_zeta_trace_cli_resolves_refs_and_prefixes(monkeypatch) -> None:
    store = zeta_trace.InMemoryStore()
    prompt_id = store.put_object(
        zeta_trace.Object(kind="prompt", schema="zeta.prompt.v1", data={"n": 1})
    )
    store.move_ref("prompt/current", None, prompt_id)
    digest_prefix = prompt_id.removeprefix("sha256:")[:8]
    monkeypatch.setattr("sigil.cli.trace.current_store", lambda: store)

    runner = CliRunner()
    by_ref = runner.invoke(sigil_cli, ["trace", "show", "--json", "prompt/current"])
    by_prefix = runner.invoke(sigil_cli, ["trace", "show", "--json", digest_prefix])
    closure = runner.invoke(sigil_cli, ["trace", "closure", digest_prefix])

    assert by_ref.exit_code == 0
    assert json.loads(by_ref.output)["id"] == prompt_id
    assert by_prefix.exit_code == 0
    assert json.loads(by_prefix.output)["id"] == prompt_id
    assert closure.exit_code == 0


def test_sigil_zeta_trace_cli_reports_ambiguous_and_unknown_ids(
    monkeypatch,
) -> None:
    store = zeta_trace.InMemoryStore()
    obj = zeta_trace.Object(kind="prompt", schema="v1", data={"n": 1})
    store._objects["sha256:aaaa1111"] = obj
    store._objects["sha256:aaaa2222"] = obj
    monkeypatch.setattr("sigil.cli.trace.current_store", lambda: store)

    runner = CliRunner()
    ambiguous = runner.invoke(sigil_cli, ["trace", "show", "aaaa"])
    unknown = runner.invoke(sigil_cli, ["trace", "show", "ffff"])

    assert ambiguous.exit_code != 0
    assert "sha256:aaaa1111" in ambiguous.output
    assert "sha256:aaaa2222" in ambiguous.output
    assert unknown.exit_code != 0
    assert "ffff" in unknown.output
