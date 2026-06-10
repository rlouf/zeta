"""Trace store and run-timeline tests."""

from __future__ import annotations

import json
from pathlib import Path

from _zeta_helpers import (
    BatchSpyStore,
)
from click.testing import CliRunner

from sigil.cli import cli as sigil_cli
from sigil.zeta import prompt as zeta_prompt
from sigil.zeta import timeline as zeta_timeline
from sigil.zeta import trace as zeta_trace


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

    assert zeta_trace.object_id(first) == zeta_trace.object_id(second)


def test_zeta_trace_object_ids_change_for_schema_data_and_links() -> None:
    base = zeta_trace.object_id(
        zeta_trace.Object(kind="example", schema="v1", data={"value": 1})
    )

    assert base != zeta_trace.object_id(
        zeta_trace.Object(kind="example", schema="v2", data={"value": 1})
    )
    assert base != zeta_trace.object_id(
        zeta_trace.Object(kind="example", schema="v1", data={"value": 2})
    )
    assert zeta_trace.object_id(
        zeta_trace.Object(
            kind="example",
            schema="v1",
            data={"value": 1},
            links=("left", "right"),
        )
    ) != zeta_trace.object_id(
        zeta_trace.Object(
            kind="example",
            schema="v1",
            data={"value": 1},
            links=("right", "left"),
        )
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
    store.set_ref("prompt/current", child_id)
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
    assert reopened.get_ref("prompt/current") == child_id
    assert reopened.derivations_for_output(child_id)[0].producer == "test:v1"
    assert set(reopened.graph_closure([child_id])) == {parent_id, child_id}
    assert reopened.stats().object_count == 2


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
    store.set_ref("prompt/current", prompt_id)
    monkeypatch.setattr("sigil.cli.zeta.default_store", lambda: store)

    runner = CliRunner()
    show = runner.invoke(sigil_cli, ["zeta", "trace", "show", prompt_id])
    closure = runner.invoke(sigil_cli, ["zeta", "trace", "closure", prompt_id])
    refs = runner.invoke(sigil_cli, ["zeta", "trace", "refs"])
    prompts = runner.invoke(sigil_cli, ["zeta", "trace", "prompts"])

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
    monkeypatch.setattr("sigil.cli.zeta.default_store", lambda: store)

    result = CliRunner().invoke(sigil_cli, ["zeta", "trace", "prompts"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["stats"]["object_count"] == 1
    assert data["prompts"][0]["id"] == prompt_id


def test_zeta_chat_messages_keeps_full_history_and_current_events() -> None:
    transcript = [{"role": "user", "content": f"prior-{index}"} for index in range(25)]
    current_events = [
        {"type": "assistant_message", "content": f"current-{index}"}
        for index in range(25)
    ]

    messages = zeta_prompt.component_messages(
        zeta_prompt.prompt_components(
            "inspect",
            transcript,
            allowed_tools=(),
            current_events=current_events,
            include_non_message_components=False,
        )
    )
    contents = [str(message.get("content") or "") for message in messages]

    assert "prior-0" in contents
    assert "prior-24" in contents
    assert "Objective:\ninspect" in contents[26]
    assert "current-0" in contents
    assert "current-24" in contents


def test_zeta_timeline_record_and_tail(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "zeta-test")

    zeta_timeline.record_event({"type": "tool_call", "name": "read"})

    events = zeta_timeline.current_timeline(1)
    assert events[0]["type"] == "tool_call"
    assert events[0]["name"] == "read"
    refs = zeta_trace.default_store().refs()
    assert refs[zeta_timeline.run_head_ref("zeta-test")]
    assert refs[zeta_timeline.event_head_ref("zeta-test")]


def test_zeta_timeline_projects_from_ref_and_object(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "zeta-test")

    zeta_timeline.record_event({"type": "user_message", "content": "first"})
    store = zeta_trace.default_store()
    first_head = store.get_ref(zeta_timeline.run_head_ref("zeta-test"))
    assert first_head is not None
    store.set_ref("run/custom/head", first_head)

    zeta_timeline.record_event({"type": "assistant_message", "content": "second"})

    assert [
        event["content"] for event in zeta_timeline.timeline_from_ref("run/custom/head")
    ] == ["first"]
    assert [
        event["content"]
        for event in zeta_timeline.timeline_from_ref(
            zeta_timeline.run_head_ref("zeta-test")
        )
    ] == ["first", "second"]
    assert [
        event["content"]
        for event in zeta_timeline.timeline_from_object(first_head, store=store)
    ] == ["first"]
    assert zeta_timeline.timeline_from_ref("run/missing/head") == []
    assert (
        zeta_timeline.timeline_from_ref(
            zeta_timeline.run_head_ref("zeta-test"), limit=1
        )[0]["content"]
        == "second"
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

    messages = zeta_timeline.chat_messages([event])

    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    content = str(messages[0]["content"])
    assert "data" in content
    assert "sha256:" not in content
    assert "model_telemetry" not in content
    assert "prompt_trace" not in content


def test_zeta_sqlite_store_batch_defers_commit(tmp_path: Path) -> None:
    store = zeta_trace.SqliteStore(tmp_path / "trace.sqlite3")
    reader = zeta_trace.SqliteStore(tmp_path / "trace.sqlite3")

    with store.batch():
        object_id = store.put_object(
            zeta_trace.Object(kind="value", schema="v1", data={"n": 1})
        )
        assert reader.get_object(object_id) is None

    assert reader.get_object(object_id) is not None


def test_zeta_record_event_writes_in_a_single_batch(monkeypatch) -> None:
    store = BatchSpyStore()
    monkeypatch.setattr(zeta_timeline, "default_store", lambda: store)

    zeta_timeline.record_event({"type": "user_message", "content": "hello"})

    assert store.batches == 1
