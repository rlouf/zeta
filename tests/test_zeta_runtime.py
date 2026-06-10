from __future__ import annotations

import json
import re
import shutil
import sys
from collections.abc import Callable, Iterator
from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest
from click.testing import CliRunner

from sigil.cli import cli as sigil_cli
from sigil.protocols import (
    SHELL_HANDOFF_CANCEL_EXPECTED_NOT_EXECUTED,
    SHELL_HANDOFF_OUTCOME_CANCELLED,
    SHELL_HANDOFF_OUTCOME_EXECUTED,
    SHELL_HANDOFF_OUTCOME_NO_PENDING,
    SHELL_HANDOFF_RESULT_SCHEMA,
    SHELL_HANDOFF_RESULT_TYPE,
    SHELL_PROMPT_HANDOFF_TYPE,
)
from sigil.routes import _turn as turn_routes
from sigil.routes import ask as answers_runner
from sigil.routes import zeta_step as zeta_runner
from sigil import handoff as sigil_handoff
from sigil.session import read_event_log, recent_turns, record_turn
from sigil.state import read_jsonl
from sigil import display as sigil_display
from sigil.zeta import agent as zeta_agent
from sigil.zeta import prompt as zeta_prompt
from sigil.zeta import runtime as zeta
from sigil.zeta import skills as zeta_skills
from sigil.zeta import tools as zeta_tools
from sigil.zeta import timeline as zeta_timeline
from sigil.zeta import model as zeta_model
from sigil.zeta import models as zeta_models
from sigil.zeta import trace as zeta_trace
from sigil.zeta.tools import bash as bash_tool
from sigil.zeta.tools import grep as grep_tool
from sigil.zeta.tools import read as read_tool
from sigil.zeta.tools import validate_tool_args


class TtyBuffer(StringIO):
    def isatty(self) -> bool:
        return True


ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def visible_terminal_text(text: str) -> str:
    return ANSI_RE.sub("", text).replace("\r", "")


class FakeStreamingResponse:
    def __init__(self, lines: list[bytes]) -> None:
        self.lines = lines
        self.closed = False

    def __enter__(self) -> FakeStreamingResponse:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def __iter__(self) -> Iterator[bytes]:
        return iter(self.lines)

    def close(self) -> None:
        self.closed = True


class DeltaSink:
    def __init__(self) -> None:
        self.deltas: list[str] = []

    def content_delta(self, text: str) -> None:
        self.deltas.append(text)


def required_stream_sink(
    kwargs: dict[str, object],
) -> zeta_model.ChatCompletionStreamSink:
    stream_sink = kwargs.get("stream_sink")
    assert stream_sink is not None
    return cast(zeta_model.ChatCompletionStreamSink, stream_sink)


def sse_lines(*payloads: dict[str, Any] | str) -> list[bytes]:
    lines: list[bytes] = []
    for payload in payloads:
        data = payload if isinstance(payload, str) else json.dumps(payload)
        lines.append(f"data: {data}\n".encode("utf-8"))
        lines.append(b"\n")
    return lines


def write_models_config(home: Path, text: str) -> Path:
    config_dir = home / ".zeta"
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "models.toml"
    path.write_text(text, encoding="utf-8")
    return path


def tool_call_fixture(
    call_id: str = "call-read",
    *,
    name: str = "read",
    path: str = "big.txt",
) -> list[dict[str, Any]]:
    return [
        {
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps({"path": path})},
        }
    ]


def tool_result_event(
    call_id: str,
    text: str,
    *,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_call_id": call_id,
        "result": {
            "ok": True,
            "content": [{"type": "text", "text": text}],
            "metadata": metadata,
        },
    }


def tool_result_transcript(
    call_id: str,
    text: str,
    *,
    metadata: dict[str, Any],
    tool_name: str = "read",
) -> list[dict[str, Any]]:
    return [
        {
            "type": "assistant_message",
            "tool_calls": tool_call_fixture(call_id, name=tool_name),
        },
        tool_result_event(call_id, text, metadata=metadata),
    ]


def linked_ids_by_kind(
    store: zeta_trace.Store,
    prompt: zeta_trace.Object,
    kind: str,
) -> list[zeta_trace.ObjectId]:
    matches = []
    for object_id in prompt.links:
        linked = store.get_object(object_id)
        if linked is not None and linked.kind == kind:
            matches.append(object_id)
    return matches


def linked_kinds(store: zeta_trace.Store, prompt: zeta_trace.Object) -> list[str]:
    kinds = []
    for object_id in prompt.links:
        linked = store.get_object(object_id)
        if linked is not None:
            kinds.append(linked.kind)
    return kinds


def event_by_type(
    events: list[dict[str, Any]],
    event_type: str,
) -> dict[str, Any]:
    return next(event for event in events if event.get("type") == event_type)


def read_tool_call_response(target: Path) -> dict[str, Any]:
    return {
        "content": "",
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "read",
                    "arguments": json.dumps({"path": str(target)}),
                },
            }
        ],
    }


def read_tool_payload(target: Path) -> dict[str, Any]:
    return {
        "ok": True,
        "content": [{"type": "text", "text": "README"}],
        "metadata": {"path": str(target)},
    }


def assert_structural_trim_payload(
    payload: dict[str, Any],
    *,
    call_id: str,
    metadata: dict[str, Any],
    text_lines: int,
) -> None:
    assert payload["trimmed"] is True
    assert payload["trim_method"] == "structural"
    assert payload["tool_call_id"] == call_id
    assert payload["source_object_id"].startswith("sha256:")
    assert payload["tool_result"]["metadata"] == metadata
    assert payload["tool_result"]["content"][0]["text_lines"] == text_lines


def assert_structural_trim_graph(
    store: zeta_trace.InMemoryStore,
    prepared: zeta_prompt.PreparedPrompt,
    payload: dict[str, Any],
    *,
    metadata: dict[str, Any],
) -> None:
    assert prepared.prompt_object_id is not None
    prompt = store.get_object(prepared.prompt_object_id)
    assert prompt is not None
    compacted_ids = linked_ids_by_kind(store, prompt, "compacted_context")
    assert len(compacted_ids) == 1
    compacted = store.get_object(compacted_ids[0])
    assert compacted is not None
    assert compacted.links == (payload["source_object_id"],)
    source = store.get_object(payload["source_object_id"])
    assert source is not None
    assert source.data["source_event"]["type"] == "tool_result"
    assert source.data["source_event"]["result"]["metadata"] == metadata
    assert store.derivations_for_output(compacted_ids[0])[0].producer == (
        "PromptStructuralTrim:v1"
    )
    closure = store.graph_closure([prepared.prompt_object_id])
    assert payload["source_object_id"] in closure


def assert_task_state_graph(
    store: zeta_trace.InMemoryStore,
    prepared: zeta_prompt.PreparedPrompt,
    *,
    source_count: int,
) -> zeta_trace.Object:
    assert prepared.prompt_object_id is not None
    prompt = store.get_object(prepared.prompt_object_id)
    assert prompt is not None
    task_state_ids = linked_ids_by_kind(store, prompt, "task_state")
    assert len(task_state_ids) == 1
    task_state = store.get_object(task_state_ids[0])
    assert task_state is not None
    assert len(task_state.links) == source_count
    assert store.derivations_for_output(task_state_ids[0])[0].producer == (
        "PromptTaskStateExtractor:v1"
    )
    closure = store.graph_closure([prepared.prompt_object_id])
    assert set(task_state.links).issubset(closure)
    return task_state


def assert_tool_result_derivation_graph(
    store: zeta_trace.InMemoryStore,
    result: zeta_agent.AgentTurnResult,
    call_event: dict[str, Any],
    result_event: dict[str, Any],
) -> None:
    call_object_id = call_event["tool_call_object_id"]
    result_object_id = result_event["tool_result_object_id"]
    assert_tool_call_derivation(store, result, call_object_id)
    assert_tool_result_derivation(store, call_object_id, result_object_id)
    assert_prompt_closure_contains_tool_result(
        store,
        result,
        call_object_id,
        result_object_id,
    )


def assert_tool_call_derivation(
    store: zeta_trace.InMemoryStore,
    result: zeta_agent.AgentTurnResult,
    call_object_id: zeta_trace.ObjectId,
) -> None:
    call_object = store.get_object(call_object_id)
    assert call_object is not None
    assert call_object.kind == "tool_call"
    assert call_object.links == (result.prompt_traces[0].assistant_message_object_id,)
    call_derivation = store.derivations_for_output(call_object_id)[0]
    assert call_derivation.producer == "SigilToolCallProjection:v1"
    assert call_derivation.input_ids == call_object.links


def assert_tool_result_derivation(
    store: zeta_trace.InMemoryStore,
    call_object_id: zeta_trace.ObjectId,
    result_object_id: zeta_trace.ObjectId,
) -> None:
    result_object = store.get_object(result_object_id)
    assert result_object is not None
    assert result_object.kind == "tool_result"
    assert result_object.links == (call_object_id,)
    result_derivation = store.derivations_for_output(result_object_id)[0]
    assert result_derivation.producer == "SigilToolExecution:v1"
    assert result_derivation.input_ids == (call_object_id,)


def assert_prompt_closure_contains_tool_result(
    store: zeta_trace.InMemoryStore,
    result: zeta_agent.AgentTurnResult,
    call_object_id: zeta_trace.ObjectId,
    result_object_id: zeta_trace.ObjectId,
) -> None:
    second_prompt_id = result.prompt_traces[1].prompt_object_id
    second_closure = store.graph_closure([second_prompt_id])
    assert call_object_id in second_closure
    assert result_object_id in second_closure


def task_state_fixture(
    *,
    objective: str = "continue the implementation",
) -> dict[str, Any]:
    return {
        "objective": objective,
        "constraints": [{"text": "Do not touch unrelated notes.md"}],
        "decisions": [
            {
                "text": "Use structured outputs for task-state extraction",
                "rationale": "The extracted state should be schema-validated",
            }
        ],
        "open_questions": [],
        "files_touched": [
            {
                "path": "src/sigil/zeta/prompt/transforms.py",
                "operation": "modified",
                "status": "in_progress",
                "notes": "Add task-state extraction transform",
            }
        ],
        "pending_tasks": [{"text": "Run regression tests", "priority": "high"}],
        "failed_attempts": [],
    }


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
            resolved_refs={"context/current": parent_id},
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


def test_zeta_trace_conditional_ref_moves_protect_against_outdated_writers() -> None:
    store = zeta_trace.InMemoryStore()
    first_id = store.put_object(
        zeta_trace.Object(kind="value", schema="v1", data={"value": 1})
    )
    second_id = store.put_object(
        zeta_trace.Object(kind="value", schema="v1", data={"value": 2})
    )

    assert store.move_ref("value/current", first_id, expected_id=None) is True
    assert store.move_ref("value/current", second_id, expected_id=None) is False
    assert store.get_ref("value/current") == first_id
    assert store.move_ref("value/current", second_id, expected_id=first_id) is True
    assert store.get_ref("value/current") == second_id


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


def test_zeta_prompt_builder_noop_transform_matches_chat_messages() -> None:
    store = zeta_trace.InMemoryStore()
    tools = zeta_tools.model_tool_descriptors(())
    transcript = [{"role": "user", "content": "prior"}]
    current_events = [{"type": "assistant_message", "content": "current"}]

    prepared = zeta_prompt.PromptBuilder(store=store).build(
        "inspect",
        transcript,
        allowed_tools=(),
        context="Project context",
        current_events=current_events,
        tools=tools,
        selected_model="unit-model",
    )

    expected_messages = zeta_prompt.component_messages(
        zeta_prompt.prompt_components(
            "inspect",
            transcript,
            allowed_tools=(),
            context="Project context",
            current_events=current_events,
            include_non_message_components=False,
        )
    )
    assert prepared.messages == expected_messages
    assert prepared.payload == zeta_model.chat_completion_request_body(
        expected_messages,
        tools=tools,
        tool_choice="auto",
        selected_model="unit-model",
    )


def test_zeta_prompt_builder_links_prompt_components() -> None:
    store = zeta_trace.InMemoryStore()
    prepared = zeta_prompt.PromptBuilder(store=store).build(
        "inspect",
        [{"role": "user", "content": "prior"}],
        allowed_tools=("read",),
        context="Project context",
        current_events=[
            {"type": "assistant_message", "tool_calls": tool_call_fixture("call-1")},
            {"type": "tool_result", "tool_call_id": "call-1", "result": {"ok": True}},
        ],
        tools=zeta_tools.model_tool_descriptors(("read",)),
    )

    assert prepared.prompt_object_id is not None
    prompt = store.get_object(prepared.prompt_object_id)
    assert prompt is not None
    kinds = linked_kinds(store, prompt)
    assert "system_prompt" in kinds
    assert "user_objective" in kinds
    assert "transcript_message" in kinds
    assert "project_context" in kinds
    assert "tool_descriptor_set" in kinds
    assert "tool_result" in kinds


def test_zeta_prompt_components_have_representation_and_token_cost() -> None:
    component = zeta_prompt.PromptComponent(
        kind="example",
        message={"role": "user", "content": "abcdefgh"},
        source_object_id="sha256:source",
    )

    assert component.representation == "full"
    assert component.source_object_id == "sha256:source"
    assert zeta_prompt.estimated_tokens(component) == 2


def test_zeta_budget_measure_returns_total_and_breakdown() -> None:
    usage = zeta_prompt.measure(
        [
            zeta_prompt.PromptComponent(
                kind="one",
                message={"role": "user", "content": "abcd"},
                object_id="sha256:one",
            ),
            zeta_prompt.PromptComponent(
                kind="two",
                message={"role": "user", "content": "abcdefgh"},
                representation="summary",
                object_id="sha256:two",
            ),
        ]
    )

    assert usage.total_tokens == 3
    assert [component.kind for component in usage.components] == ["one", "two"]
    assert usage.components[1].representation == "summary"


def test_zeta_prompt_transform_factory_from_env() -> None:
    transform = zeta_prompt.prompt_transform_from_env(
        {"ZETA_TRIM": "structural", "ZETA_TRIM_THRESHOLD_TOKENS": "7"}
    )

    assert isinstance(transform, zeta_prompt.BudgetThresholdPromptTransform)
    assert transform.budget == zeta_prompt.ContextBudget(7)
    assert isinstance(transform.transform, zeta_prompt.StructuralTrimPromptTransform)
    assert isinstance(
        zeta_prompt.prompt_transform_from_env({}), zeta_prompt.NoOpPromptTransform
    )


def test_zeta_chained_transform_applies_in_order() -> None:
    class AppendKind:
        def __init__(self, suffix: str) -> None:
            self.suffix = suffix

        def apply(
            self,
            components: list[zeta_prompt.PromptComponent],
        ) -> list[zeta_prompt.PromptComponent]:
            return [
                zeta_prompt.PromptComponent(
                    kind=component.kind + self.suffix,
                    message=component.message,
                )
                for component in components
            ]

    chained = zeta_prompt.ChainedTransform((AppendKind("a"), AppendKind("b")))

    assert chained.apply([zeta_prompt.PromptComponent(kind="x")])[0].kind == "xab"


def test_zeta_render_stub_contract() -> None:
    component = zeta_prompt.PromptComponent(
        kind="tool_result",
        message={"role": "tool", "content": "abcd"},
        object_id="sha256:abc",
    )

    assert (
        zeta_prompt.render_stub(component)
        == "[elided tool_result 1~tok id=sha256:abc — content retrievable by id]"
    )


def test_zeta_prompt_components_prefix_order(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    components = zeta_prompt.prompt_components(
        "inspect",
        [{"role": "user", "content": "prior"}],
        allowed_tools=("read",),
        context="Project context",
        current_events=[{"type": "assistant_message", "content": "current"}],
        tools=zeta_tools.model_tool_descriptors(("read",)),
    )

    assert [component.kind for component in components[:4]] == [
        "system_prompt",
        "tool_descriptor_set",
        "project_context",
        "transcript_message",
    ]


def test_zeta_prompt_builder_compaction_transform_preserves_source_links() -> None:
    class CompactTranscript:
        producer = "PromptCompactor:v1"

        def apply(
            self,
            components: list[zeta_prompt.PromptComponent],
        ) -> list[zeta_prompt.PromptComponent]:
            sources = [
                component
                for component in components
                if component.kind == "transcript_message"
            ]
            source_ids = tuple(
                component.object_id
                for component in sources
                if component.object_id is not None
            )
            compacted = zeta_prompt.PromptComponent(
                kind="compacted_context",
                data={"source_count": len(source_ids)},
                message={"role": "user", "content": "Compacted history"},
                links=source_ids,
            )
            output: list[zeta_prompt.PromptComponent] = []
            inserted = False
            for component in components:
                if component.kind != "transcript_message":
                    output.append(component)
                    continue
                if not inserted:
                    output.append(compacted)
                    inserted = True
            return output

    store = zeta_trace.InMemoryStore()
    prepared = zeta_prompt.PromptBuilder(
        store=store,
        transform=CompactTranscript(),
    ).build(
        "continue",
        [
            {"role": "user", "content": "prior user"},
            {"role": "assistant", "content": "prior assistant"},
        ],
        allowed_tools=(),
        tools=[],
    )

    contents = [str(message.get("content") or "") for message in prepared.messages]
    assert "Compacted history" in contents
    assert "prior user" not in contents
    assert prepared.prompt_object_id is not None
    prompt = store.get_object(prepared.prompt_object_id)
    assert prompt is not None
    compacted_ids = linked_ids_by_kind(store, prompt, "compacted_context")
    assert len(compacted_ids) == 1
    compacted = store.get_object(compacted_ids[0])
    assert compacted is not None
    assert len(compacted.links) == 2
    derivations = store.derivations_for_output(compacted_ids[0])
    assert derivations[0].producer == "PromptCompactor:v1"
    closure = store.graph_closure([prepared.prompt_object_id])
    assert set(compacted.links).issubset(closure)


def test_zeta_task_state_transform_replaces_transcript_with_structured_state() -> None:
    class FakeExtractor:
        def __init__(self) -> None:
            self.components: list[zeta_prompt.PromptComponent] = []

        def extract(
            self,
            components: list[zeta_prompt.PromptComponent],
        ) -> dict[str, Any]:
            self.components = components
            return task_state_fixture(objective="implement task-state extraction")

    store = zeta_trace.InMemoryStore()
    extractor = FakeExtractor()
    prepared = zeta_prompt.PromptBuilder(
        store=store,
        transform=zeta_prompt.TaskStateExtractionPromptTransform(extractor=extractor),
    ).build(
        "continue",
        [
            {"role": "user", "content": "Implement task-state extraction"},
            {"role": "assistant", "content": "Decision: use structured outputs"},
            {"role": "user", "content": "Do not touch unrelated notes.md"},
        ],
        allowed_tools=(),
        current_events=[{"type": "assistant_message", "content": "Fresh evidence"}],
        tools=[],
    )

    assert len(extractor.components) == 3
    assert all(
        component.kind == "transcript_message" for component in extractor.components
    )
    contents = [str(message.get("content") or "") for message in prepared.messages]
    joined = "\n".join(contents)
    assert "Task state JSON:" in joined
    assert "implement task-state extraction" in joined
    assert "Decision: use structured outputs" not in joined
    assert "Fresh evidence" in joined

    task_state = assert_task_state_graph(store, prepared, source_count=3)
    assert task_state.data["state"]["constraints"] == [
        {"text": "Do not touch unrelated notes.md"}
    ]


def test_zeta_task_state_transform_fails_open() -> None:
    class FailingExtractor:
        def extract(
            self,
            components: list[zeta_prompt.PromptComponent],
        ) -> dict[str, Any]:
            del components
            raise RuntimeError("extractor unavailable")

    store = zeta_trace.InMemoryStore()
    prepared = zeta_prompt.PromptBuilder(
        store=store,
        transform=zeta_prompt.TaskStateExtractionPromptTransform(
            extractor=FailingExtractor()
        ),
    ).build(
        "continue",
        [{"role": "user", "content": "keep raw transcript"}],
        allowed_tools=(),
        tools=[],
    )

    assert "keep raw transcript" in "\n".join(
        str(message.get("content") or "") for message in prepared.messages
    )
    assert prepared.prompt_object_id is not None
    prompt = store.get_object(prepared.prompt_object_id)
    assert prompt is not None
    assert "task_state" not in linked_kinds(store, prompt)


def test_zeta_prompt_components_keep_source_events() -> None:
    transcript = [
        {"type": "assistant_message", "tool_calls": tool_call_fixture()},
        tool_result_event(
            "call-read",
            "raw result",
            metadata={"path": "big.txt"},
        ),
    ]

    components = zeta_prompt.prompt_components(
        "continue",
        transcript,
        allowed_tools=(),
        tools=[],
    )

    tool_component = next(
        component
        for component in components
        if component.data.get("source_event", {}).get("type") == "tool_result"
    )
    assert tool_component.kind == "transcript_message"
    assert tool_component.data["source_tool_name"] == "read"
    assert tool_component.data["source_event"]["tool_call_id"] == "call-read"
    assert tool_component.data["source_event"]["tool_name"] == "read"
    assert tool_component.data["source_event"]["result"]["metadata"] == {
        "path": "big.txt"
    }
    assert tool_component.message is not None
    assert json.loads(str(tool_component.message["content"]))["metadata"] == {
        "path": "big.txt"
    }


@pytest.mark.parametrize(
    ("tool_name", "metadata"),
    [
        ("read", {"path": "big.txt", "offset": 0, "limit": 80}),
        (
            "grep",
            {
                "pattern": "important",
                "path": ".",
                "limit": 80,
                "matches": 80,
                "files": 1,
            },
        ),
    ],
)
def test_zeta_structural_trim_compacts_old_bulky_read_or_grep_tool_results(
    tool_name: str,
    metadata: dict[str, Any],
) -> None:
    store = zeta_trace.InMemoryStore()
    raw_text = "\n".join(f"line {index}: important but bulky" for index in range(80))

    prepared = zeta_prompt.PromptBuilder(
        store=store,
        transform=zeta_prompt.StructuralTrimPromptTransform(max_content_chars=120),
    ).build(
        "continue",
        tool_result_transcript(
            "call-read",
            raw_text,
            metadata=metadata,
            tool_name=tool_name,
        ),
        allowed_tools=(),
        tools=[],
    )

    tool_messages = [
        message for message in prepared.messages if message.get("role") == "tool"
    ]
    assert len(tool_messages) == 1
    stub = str(tool_messages[0]["content"])
    assert tool_messages[0]["tool_call_id"] == "call-read"
    assert stub.startswith("[elided transcript_message ")
    assert " content retrievable by id]" in stub
    assert "line 79" not in str(tool_messages[0]["content"])
    assert prepared.prompt_object_id is not None
    prompt = store.get_object(prepared.prompt_object_id)
    assert prompt is not None
    compacted_ids = linked_ids_by_kind(store, prompt, "compacted_context")
    assert len(compacted_ids) == 1
    compacted = store.get_object(compacted_ids[0])
    assert compacted is not None
    assert compacted.data["representation"] == "stub"
    assert compacted.data["source_object_id"] in stub


def test_zeta_structural_trim_skips_non_read_grep_tool_results() -> None:
    store = zeta_trace.InMemoryStore()
    raw_text = "non-recoverable tool evidence " * 100

    prepared = zeta_prompt.PromptBuilder(
        store=store,
        transform=zeta_prompt.StructuralTrimPromptTransform(max_content_chars=120),
    ).build(
        "continue",
        tool_result_transcript(
            "call-bash",
            raw_text,
            metadata={"command": "python script.py"},
            tool_name="bash",
        ),
        allowed_tools=(),
        tools=[],
    )

    tool_messages = [
        message for message in prepared.messages if message.get("role") == "tool"
    ]
    assert len(tool_messages) == 1
    assert "non-recoverable tool evidence" in str(tool_messages[0]["content"])
    assert "source_object_id" not in str(tool_messages[0]["content"])

    assert prepared.prompt_object_id is not None
    prompt = store.get_object(prepared.prompt_object_id)
    assert prompt is not None
    assert "transcript_message" in linked_kinds(store, prompt)
    assert "compacted_context" not in linked_kinds(store, prompt)


def test_zeta_structural_trim_default_is_late_safety_valve() -> None:
    transform = zeta_prompt.StructuralTrimPromptTransform()
    below = zeta_prompt.PromptComponent(
        kind="transcript_message",
        data={
            "source_event": {
                "type": "tool_result",
                "tool_call_id": "call-below",
                "tool_name": "read",
            }
        },
        message={
            "role": "tool",
            "tool_call_id": "call-below",
            "content": "x" * 119_999,
        },
        object_id="sha256:below",
    )
    above = zeta_prompt.PromptComponent(
        kind="transcript_message",
        data={
            "source_event": {
                "type": "tool_result",
                "tool_call_id": "call-above",
                "tool_name": "read",
            }
        },
        message={
            "role": "tool",
            "tool_call_id": "call-above",
            "content": "x" * 120_001,
        },
        object_id="sha256:above",
    )

    trimmed = transform.apply([below, above])

    assert trimmed[0].kind == "transcript_message"
    assert trimmed[1].kind == "compacted_context"


def test_zeta_structural_trim_preserves_current_tool_results_by_default() -> None:
    store = zeta_trace.InMemoryStore()
    raw_text = "fresh evidence " * 100

    prepared = zeta_prompt.PromptBuilder(
        store=store,
        transform=zeta_prompt.StructuralTrimPromptTransform(max_content_chars=20),
    ).build(
        "continue",
        [],
        allowed_tools=(),
        current_events=[
            {
                "type": "assistant_message",
                "tool_calls": tool_call_fixture("call-fresh", path="fresh.txt"),
            },
            tool_result_event(
                "call-fresh",
                raw_text,
                metadata={"path": "fresh.txt"},
            ),
        ],
        tools=[],
    )

    tool_messages = [
        message for message in prepared.messages if message.get("role") == "tool"
    ]
    assert len(tool_messages) == 1
    assert "fresh evidence" in str(tool_messages[0]["content"])
    assert "source_object_id" not in str(tool_messages[0]["content"])

    assert prepared.prompt_object_id is not None
    prompt = store.get_object(prepared.prompt_object_id)
    assert prompt is not None
    kinds = linked_kinds(store, prompt)
    assert "tool_result" in kinds
    assert "compacted_context" not in kinds


def test_zeta_structural_trim_uses_source_event_without_message_json() -> None:
    raw_text = "invalid json but still bulky " * 20
    component = zeta_prompt.PromptComponent(
        kind="transcript_message",
        data={
            "source_event": {
                "type": "tool_result",
                "tool_call_id": "call-structured",
                "tool_name": "read",
                "result": {
                    "ok": True,
                    "content": [{"type": "text", "text": raw_text}],
                    "metadata": {"path": "structured.txt"},
                },
            }
        },
        message={
            "role": "tool",
            "tool_call_id": "call-structured",
            "content": raw_text,
        },
        object_id="sha256:source",
    )

    trimmed = zeta_prompt.StructuralTrimPromptTransform(max_content_chars=20).apply(
        [component]
    )[0]

    assert trimmed.kind == "compacted_context"
    assert trimmed.representation == "stub"
    assert trimmed.message is not None
    assert str(trimmed.message["content"]) == (
        "[elided transcript_message 145~tok id=sha256:source "
        "— content retrievable by id]"
    )


def test_zeta_model_config_uses_zeta_env(monkeypatch) -> None:
    monkeypatch.delenv("ZETA_MODEL_URL", raising=False)
    monkeypatch.delenv("ZETA_MODEL_NAME", raising=False)
    monkeypatch.delenv("ZETA_MODEL_IDLE_TIMEOUT_SECONDS", raising=False)

    assert zeta_model.model_url() == zeta_model.DEFAULT_MODEL_URL
    assert zeta_model.model_name() == zeta_model.DEFAULT_MODEL_NAME
    assert (
        zeta_model.model_idle_timeout() == zeta_model.DEFAULT_MODEL_IDLE_TIMEOUT_SECONDS
    )
    assert zeta_model.DEFAULT_MODEL_IDLE_TIMEOUT_SECONDS == 120.0

    monkeypatch.setenv("ZETA_MODEL_URL", "http://zeta.invalid/v1/chat/completions")
    monkeypatch.setenv("ZETA_MODEL_NAME", "zeta-model")
    monkeypatch.setenv("ZETA_MODEL_IDLE_TIMEOUT_SECONDS", "2.5")

    assert zeta_model.model_url() == "http://zeta.invalid/v1/chat/completions"
    assert zeta_model.model_name() == "zeta-model"
    assert zeta_model.model_idle_timeout() == 2.5

    monkeypatch.setenv("ZETA_MODEL_IDLE_TIMEOUT_SECONDS", "0")
    assert zeta_model.model_idle_timeout() is None


def test_zeta_request_chat_completion_streams_final_message(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    response = FakeStreamingResponse(
        sse_lines(
            {
                "id": "chatcmpl-test",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "hel"},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "lo"},
                        "finish_reason": "stop",
                    }
                ],
            },
            "[DONE]",
        )
    )

    def fake_urlopen(req: Any, timeout: float | None = None) -> FakeStreamingResponse:
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["accept"] = req.get_header("Accept")
        captured["timeout"] = timeout
        return response

    monkeypatch.delenv("ZETA_MODEL_IDLE_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(zeta_model.urllib.request, "urlopen", fake_urlopen)
    body = {"model": "local-model", "messages": []}

    payload = zeta_model.request_chat_completion(body)

    assert body == {"model": "local-model", "messages": []}
    assert captured["body"]["stream"] is True
    assert captured["accept"] == "text/event-stream"
    assert captured["timeout"] == zeta_model.DEFAULT_MODEL_IDLE_TIMEOUT_SECONDS
    assert response.closed is True
    assert payload["id"] == "chatcmpl-test"
    assert payload["choices"][0]["message"] == {
        "role": "assistant",
        "content": "hello",
    }
    assert payload["choices"][0]["finish_reason"] == "stop"


def test_zeta_stream_replaces_invalid_utf8_bytes() -> None:
    chunk = (
        b'data: {"choices":[{"index":0,"delta":{"content":"caf\xff"},'
        b'"finish_reason":"stop"}]}\n'
    )
    lines = [chunk, b"\n", b"data: [DONE]\n"]

    payload = zeta_model.read_streamed_chat_completion(iter(lines))

    assert payload["choices"][0]["message"]["content"] == "caf�"


def test_zeta_stream_reassembles_chunks_split_mid_character() -> None:
    frame = (
        'data: {"choices":[{"index":0,"delta":{"content":"café"},'
        '"finish_reason":"stop"}]}\n'
    ).encode("utf-8")
    split = frame.index(b"\xc3") + 1
    lines = [frame[:split], frame[split:], b"\n", b"data: [DONE]\n"]

    payload = zeta_model.read_streamed_chat_completion(iter(lines))

    assert payload["choices"][0]["message"]["content"] == "café"


def test_zeta_stream_emits_content_deltas_in_order() -> None:
    sink = DeltaSink()

    payload = zeta_model.read_streamed_chat_completion(
        sse_lines(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "hel"},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "lo"},
                        "finish_reason": "stop",
                    }
                ],
            },
            "[DONE]",
        ),
        stream_sink=sink,
    )

    assert sink.deltas == ["hel", "lo"]
    assert payload["choices"][0]["message"]["content"] == "hello"


def test_zeta_stream_preserves_usage_chunk() -> None:
    payload = zeta_model.read_streamed_chat_completion(
        sse_lines(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "hello"},
                        "finish_reason": "stop",
                    }
                ],
            },
            {
                "usage": {
                    "prompt_tokens": 123,
                    "completion_tokens": 4,
                    "total_tokens": 127,
                },
            },
            "[DONE]",
        )
    )

    assert payload["usage"] == {
        "prompt_tokens": 123,
        "completion_tokens": 4,
        "total_tokens": 127,
    }


def test_zeta_stream_sink_does_not_change_reconstructed_message() -> None:
    frames = sse_lines(
        {
            "id": "chatcmpl-test",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": "done"},
                    "finish_reason": "stop",
                }
            ],
        },
        "[DONE]",
    )
    sink = DeltaSink()

    without_sink = zeta_model.read_streamed_chat_completion(frames)
    with_sink = zeta_model.read_streamed_chat_completion(frames, stream_sink=sink)

    assert with_sink == without_sink
    assert sink.deltas == ["done"]


def test_zeta_stream_does_not_render_tool_call_fragments() -> None:
    sink = DeltaSink()

    payload = zeta_model.read_streamed_chat_completion(
        sse_lines(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-read",
                                    "type": "function",
                                    "function": {
                                        "name": "read",
                                        "arguments": '{"path"',
                                    },
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": ': "README.md"}'},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            },
            "[DONE]",
        ),
        stream_sink=sink,
    )

    assert sink.deltas == []
    assert payload["choices"][0]["message"]["tool_calls"][0]["function"] == {
        "name": "read",
        "arguments": '{"path": "README.md"}',
    }


def test_zeta_stream_mixed_content_and_tool_call_exposes_completed_call() -> None:
    sink = DeltaSink()

    payload = zeta_model.read_streamed_chat_completion(
        sse_lines(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "content": "I'll inspect README.",
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-read",
                                    "type": "function",
                                    "function": {
                                        "name": "read",
                                        "arguments": '{"path":"README.md"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            },
            "[DONE]",
        ),
        stream_sink=sink,
    )

    message = payload["choices"][0]["message"]
    assert sink.deltas == ["I'll inspect README."]
    assert message["content"] == "I'll inspect README."
    assert message["tool_calls"][0]["function"]["name"] == "read"


def test_zeta_stream_reconstructs_split_tool_calls() -> None:
    payload = zeta_model.read_streamed_chat_completion(
        sse_lines(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-read",
                                    "type": "function",
                                    "function": {
                                        "name": "read",
                                        "arguments": '{"path"',
                                    },
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": ': "README.md"}'},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            },
            "[DONE]",
        )
    )

    message = payload["choices"][0]["message"]
    assert message["tool_calls"] == [
        {
            "id": "call-read",
            "type": "function",
            "function": {
                "name": "read",
                "arguments": '{"path": "README.md"}',
            },
        }
    ]
    assert payload["choices"][0]["finish_reason"] == "tool_calls"


def test_zeta_stream_orders_multiple_tool_calls_by_index() -> None:
    payload = zeta_model.read_streamed_chat_completion(
        sse_lines(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 1,
                                    "id": "call-ls",
                                    "type": "function",
                                    "function": {
                                        "name": "ls",
                                        "arguments": '{"path":"."}',
                                    },
                                },
                                {
                                    "index": 0,
                                    "id": "call-read",
                                    "type": "function",
                                    "function": {
                                        "name": "read",
                                        "arguments": '{"path":"README.md"}',
                                    },
                                },
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            },
            "[DONE]",
        )
    )

    calls = payload["choices"][0]["message"]["tool_calls"]
    assert [call["id"] for call in calls] == ["call-read", "call-ls"]


def test_zeta_request_chat_completion_closes_stream_on_error(monkeypatch) -> None:
    response = FakeStreamingResponse(
        sse_lines({"error": {"message": "generation failed"}})
    )

    def fake_urlopen(req: Any, timeout: float | None = None) -> FakeStreamingResponse:
        return response

    monkeypatch.setattr(zeta_model.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError, match="generation failed"):
        zeta_model.request_chat_completion({"model": "local-model", "messages": []})

    assert response.closed is True


def test_zeta_stream_rejects_malformed_events() -> None:
    with pytest.raises(RuntimeError, match="invalid JSON event"):
        zeta_model.read_streamed_chat_completion([b"data: nope\n", b"\n"])


def test_zeta_model_profiles_load_user_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    write_models_config(
        home,
        """
[[models]]
name = "fast"
model = "fast-model"
url = "http://127.0.0.1:8081/v1/chat/completions"

[[models]]
name = "default-url"
model = "default-url-model"
""",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ZETA_MODEL_URL", "http://env.invalid/v1/chat/completions")

    catalog = zeta_models.load_model_profiles()
    fast = zeta_models.resolve_model_profile("fast", catalog=catalog)
    default_url = zeta_models.resolve_model_profile("default-url", catalog=catalog)

    assert catalog.diagnostics == []
    assert fast == zeta_models.ModelSelection(
        profile="fast",
        model="fast-model",
        url="http://127.0.0.1:8081/v1/chat/completions",
    )
    assert default_url == zeta_models.ModelSelection(
        profile="default-url",
        model="default-url-model",
        url="http://env.invalid/v1/chat/completions",
    )


def test_zeta_model_profiles_report_invalid_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    write_models_config(
        home,
        """
[[models]]
name = "Bad_Name"
model = "bad"
""",
    )
    monkeypatch.setenv("HOME", str(home))

    catalog = zeta_models.load_model_profiles()

    assert catalog.profiles == {}
    assert len(catalog.diagnostics) == 1
    assert "lowercase letters" in catalog.diagnostics[0].message


def test_sigil_model_cli_switches_model_per_session(
    tmp_path: Path,
    monkeypatch,
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
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SIGIL_SESSION_ID", "one")

    use = CliRunner().invoke(sigil_cli, ["model", "use", "fast"])

    assert use.exit_code == 0, use.output
    assert "model: fast -> fast-model" in use.output
    assert zeta_models.active_model_profile() == "fast"

    show = CliRunner().invoke(sigil_cli, ["model", "show"])
    assert show.exit_code == 0, show.output
    assert "model: fast -> fast-model" in show.output

    monkeypatch.setenv("SIGIL_SESSION_ID", "two")
    other_session = CliRunner().invoke(sigil_cli, ["model", "show"])
    assert other_session.exit_code == 0, other_session.output
    assert "model: default ->" in other_session.output

    monkeypatch.setenv("SIGIL_SESSION_ID", "one")
    clear = CliRunner().invoke(sigil_cli, ["model", "clear"])
    assert clear.exit_code == 0, clear.output
    assert zeta_models.active_model_profile() is None


def test_sigil_model_cli_rejects_unknown_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    write_models_config(home, "")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SIGIL_SESSION_ID", "model-test")

    result = CliRunner().invoke(sigil_cli, ["model", "use", "missing"])

    assert result.exit_code != 0
    assert "unknown model profile: missing" in result.output
    assert zeta_models.active_model_profile() is None


def test_zeta_model_context_tokens_prefers_props(monkeypatch) -> None:
    zeta_model._MODEL_CONTEXT_TOKENS_CACHE.clear()
    calls: list[str] = []

    def fake_metadata(
        path: str,
        *,
        selected_url: str | None = None,
    ) -> dict[str, Any] | None:
        del selected_url
        calls.append(path)
        return {"default_generation_settings": {"n_ctx": 262_144}}

    monkeypatch.setattr(zeta_model, "request_model_metadata", fake_metadata)

    tokens = zeta_model.model_context_tokens(
        "http://127.0.0.1:8080/v1/chat/completions",
        "local-model",
    )

    assert tokens == 262_144
    assert calls == ["/props"]


def test_zeta_model_context_tokens_falls_back_to_selected_model(
    monkeypatch,
) -> None:
    zeta_model._MODEL_CONTEXT_TOKENS_CACHE.clear()

    def fake_metadata(
        path: str,
        *,
        selected_url: str | None = None,
    ) -> dict[str, Any] | None:
        del selected_url
        if path == "/props":
            return {}
        return {
            "data": [
                {"id": "other-model", "meta": {"n_ctx": 8_192}},
                {
                    "id": "fast-model",
                    "aliases": ["fast"],
                    "meta": {"n_ctx": 65_536},
                },
            ]
        }

    monkeypatch.setattr(zeta_model, "request_model_metadata", fake_metadata)

    tokens = zeta_model.model_context_tokens(
        "http://127.0.0.1:8080/v1/chat/completions",
        "fast",
    )

    assert tokens == 65_536


def test_zeta_model_context_tokens_returns_none_when_unavailable(
    monkeypatch,
) -> None:
    zeta_model._MODEL_CONTEXT_TOKENS_CACHE.clear()
    monkeypatch.setattr(
        zeta_model,
        "request_model_metadata",
        lambda *args, **kwargs: {},
    )

    tokens = zeta_model.model_context_tokens(
        "http://127.0.0.1:8080/v1/chat/completions",
        "local-model",
    )

    assert tokens is None


def test_zeta_chat_completion_messages_accepts_request_model(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_request(
        body: dict[str, Any],
        *,
        selected_url: str | None = None,
    ) -> dict[str, Any]:
        captured["body"] = body
        captured["selected_url"] = selected_url
        return {"choices": [{"message": {"content": "done"}}]}

    monkeypatch.setattr(zeta_model, "request_chat_completion", fake_request)

    message = zeta_model.chat_completion_messages(
        [{"role": "user", "content": "hi"}],
        selected_model="fast-model",
        selected_url="http://127.0.0.1:8081/v1/chat/completions",
    )

    assert message == {"content": "done"}
    body = cast(dict[str, Any], captured["body"])
    assert body["model"] == "fast-model"
    assert body["stream_options"] == {"include_usage": True}
    assert captured["selected_url"] == "http://127.0.0.1:8081/v1/chat/completions"


def test_zeta_chat_completion_messages_sends_native_tools(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_request(body: dict[str, Any]) -> dict[str, Any]:
        captured["body"] = body
        return {"choices": [{"message": {"content": "done"}}]}

    monkeypatch.setattr(zeta_model, "request_chat_completion", fake_request)

    message = zeta_model.chat_completion_messages(
        [{"role": "user", "content": "hi"}],
        tools=[
            {
                "type": "function",
                "function": {"name": "read", "description": "", "parameters": {}},
            }
        ],
    )

    assert message == {"content": "done"}
    body = cast(dict[str, Any], captured["body"])
    assert body["tools"][0]["function"]["name"] == "read"
    assert body["tool_choice"] == "auto"
    assert body["stream_options"] == {"include_usage": True}
    assert "response_format" not in body


def test_zeta_chat_structured_output_sends_json_schema(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    state = task_state_fixture(objective="extract task state")

    def fake_request(
        body: dict[str, Any],
        *,
        selected_url: str | None = None,
    ) -> dict[str, Any]:
        captured["body"] = body
        captured["selected_url"] = selected_url
        return {"choices": [{"message": {"content": json.dumps(state)}}]}

    monkeypatch.setattr(zeta_model, "request_chat_completion", fake_request)

    extracted = zeta_model.chat_structured_output(
        [{"role": "user", "content": "history"}],
        schema=zeta_prompt.TASK_STATE_SCHEMA,
        response_name="zeta_task_state",
        selected_model="state-model",
        selected_url="http://127.0.0.1:8081/v1/chat/completions",
    )

    assert extracted == state
    body = cast(dict[str, Any], captured["body"])
    assert body["model"] == "state-model"
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["name"] == "zeta_task_state"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert (
        body["response_format"]["json_schema"]["schema"]
        == zeta_prompt.TASK_STATE_SCHEMA
    )
    assert captured["selected_url"] == "http://127.0.0.1:8081/v1/chat/completions"


def test_zeta_chat_structured_output_rejects_invalid_json_schema(
    monkeypatch,
) -> None:
    def fake_request(
        body: dict[str, Any],
        *,
        selected_url: str | None = None,
    ) -> dict[str, Any]:
        del body
        del selected_url
        return {"choices": [{"message": {"content": "{}"}}]}

    monkeypatch.setattr(zeta_model, "request_chat_completion", fake_request)

    with pytest.raises(RuntimeError, match="validation"):
        zeta_model.chat_structured_output(
            [{"role": "user", "content": "history"}],
            schema=zeta_prompt.TASK_STATE_SCHEMA,
            response_name="zeta_task_state",
        )


def test_zeta_chat_completion_messages_reports_model_telemetry(
    monkeypatch,
) -> None:
    telemetry: list[dict[str, Any]] = []

    def fake_request(body: dict[str, Any]) -> dict[str, Any]:
        del body
        return {
            "usage": {
                "prompt_tokens": 123,
                "completion_tokens": 4,
                "total_tokens": 127,
            },
            "choices": [{"message": {"content": "done"}}],
        }

    monkeypatch.setattr(zeta_model, "model_context_tokens", lambda *args: 262_144)
    monkeypatch.setattr(zeta_model, "request_chat_completion", fake_request)

    message = zeta_model.chat_completion_messages(
        [{"role": "user", "content": "hi"}],
        telemetry_sink=telemetry.append,
    )

    assert message == {"content": "done"}
    assert telemetry == [
        {
            "usage": {
                "prompt_tokens": 123,
                "completion_tokens": 4,
                "total_tokens": 127,
            },
            "model_context_tokens": 262_144,
        }
    ]


def test_zeta_agent_turn_finalizes_text(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        **kwargs: object,
    ) -> dict[str, Any]:
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return {"content": "done"}

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent, "chat_completion_messages", fake_chat_completion_messages
    )

    result = zeta_agent.run_agent_turn(
        "answer",
        [],
        zeta_agent.AgentConfig(allowed_tools=("read",), max_turns=1),
    )

    assert result.final_text == "done"
    assert result.events[0]["type"] == "assistant_message"
    assert result.events[0]["content"] == "done"
    assert result.events[0]["prompt_trace"]["prompt_object_id"]
    assert len(result.prompt_traces) == 1
    kwargs = cast(dict[str, Any], captured["kwargs"])
    assert kwargs["tools"][0]["function"]["name"] == "read"


def test_zeta_agent_turn_stores_prompt_and_assistant_trace(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    store = zeta_trace.InMemoryStore()

    def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        **kwargs: object,
    ) -> dict[str, Any]:
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return {"content": "done"}

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent, "chat_completion_messages", fake_chat_completion_messages
    )

    result = zeta_agent.run_agent_turn(
        "answer",
        [{"role": "user", "content": "prior"}],
        zeta_agent.AgentConfig(
            allowed_tools=("read",),
            max_turns=1,
            model_name="unit-model",
        ),
        context="Project context",
        prompt_builder=zeta_prompt.PromptBuilder(store=store),
    )

    assert len(result.prompt_traces) == 1
    trace = result.prompt_traces[0]
    prompt = store.get_object(trace.prompt_object_id)
    assert prompt is not None
    kwargs = cast(dict[str, Any], captured["kwargs"])
    assert prompt.data["payload"] == zeta_model.chat_completion_request_body(
        cast(list[dict[str, Any]], captured["messages"]),
        tools=cast(list[dict[str, Any]], kwargs["tools"]),
        tool_choice=cast(str, kwargs["tool_choice"]),
        selected_model="unit-model",
    )
    assistant = store.get_object(cast(str, trace.assistant_message_object_id))
    assert assistant is not None
    assert assistant.kind == "assistant_message"
    assert assistant.links == (trace.prompt_object_id,)
    assert assistant.data["message"] == {"content": "done"}
    assert result.events[0]["prompt_trace"]["assistant_message_object_id"] == (
        trace.assistant_message_object_id
    )


def test_zeta_agent_turn_captures_model_telemetry(monkeypatch) -> None:
    def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        **kwargs: object,
    ) -> dict[str, Any]:
        del messages
        telemetry_sink = cast(
            "Callable[[dict[str, Any]], None]", kwargs["telemetry_sink"]
        )
        telemetry_sink(
            {
                "usage": {
                    "prompt_tokens": 123,
                    "completion_tokens": 4,
                    "total_tokens": 127,
                },
                "model_context_tokens": 262_144,
            }
        )
        return {"content": "done"}

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent, "chat_completion_messages", fake_chat_completion_messages
    )

    result = zeta_agent.run_agent_turn(
        "answer",
        [],
        zeta_agent.AgentConfig(allowed_tools=("read",), max_turns=1),
    )

    assert result.final_text == "done"
    assert result.model_telemetry == {
        "usage": {
            "prompt_tokens": 123,
            "completion_tokens": 4,
            "total_tokens": 127,
        },
        "model_context_tokens": 262_144,
    }


def test_zeta_agent_turn_attaches_model_telemetry_to_first_tool_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    first = tmp_path / "README.md"
    second = tmp_path / "pyproject.toml"
    first.write_text("README\n", encoding="utf-8")
    second.write_text("[project]\n", encoding="utf-8")
    tool_telemetry = {
        "usage": {"prompt_tokens": 123, "completion_tokens": 8, "total_tokens": 131},
        "model_context_tokens": 262_144,
    }
    final_telemetry = {
        "usage": {"prompt_tokens": 456, "completion_tokens": 4, "total_tokens": 460},
        "model_context_tokens": 262_144,
    }
    responses = iter(
        [
            (
                tool_telemetry,
                {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "read",
                                "arguments": json.dumps({"path": str(first)}),
                            },
                        },
                        {
                            "id": "call-2",
                            "type": "function",
                            "function": {
                                "name": "read",
                                "arguments": json.dumps({"path": str(second)}),
                            },
                        },
                    ],
                },
            ),
            (final_telemetry, {"content": "done"}),
        ]
    )

    def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        **kwargs: object,
    ) -> dict[str, Any]:
        del messages
        telemetry, response = next(responses)
        telemetry_sink = cast(
            "Callable[[dict[str, Any]], None]", kwargs["telemetry_sink"]
        )
        telemetry_sink(telemetry)
        return response

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent, "chat_completion_messages", fake_chat_completion_messages
    )

    result = zeta_agent.run_agent_turn(
        "inspect",
        [],
        zeta_agent.AgentConfig(allowed_tools=("read",), max_turns=2),
    )

    tool_results = [
        event for event in result.events if event.get("type") == "tool_result"
    ]
    assert tool_results[0]["model_telemetry"] == tool_telemetry
    assert "model_telemetry" not in tool_results[1]
    assert result.model_telemetry == final_telemetry


def test_zeta_agent_turn_records_one_prompt_trace_per_model_request(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "README.md"
    target.write_text("README\n", encoding="utf-8")
    store = zeta_trace.InMemoryStore()
    responses = iter([read_tool_call_response(target), {"content": "done"}])

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        lambda messages, **kwargs: next(responses),
    )
    monkeypatch.setattr(
        zeta_agent,
        "analyze_tool",
        lambda name, params: {"valid": True, "resolved": True},
    )
    monkeypatch.setattr(
        zeta_agent,
        "run_tool",
        lambda name, params: read_tool_payload(target),
    )

    result = zeta_agent.run_agent_turn(
        "inspect",
        [],
        zeta_agent.AgentConfig(allowed_tools=("read",), max_turns=2),
        prompt_builder=zeta_prompt.PromptBuilder(store=store),
    )

    assert result.final_text == "done"
    assert len(result.prompt_traces) == 2
    assert result.prompt_traces[0].prompt_object_id != (
        result.prompt_traces[1].prompt_object_id
    )
    second_prompt = store.get_object(result.prompt_traces[1].prompt_object_id)
    assert second_prompt is not None
    second_payload = cast(dict[str, Any], second_prompt.data["payload"])
    assert [message["role"] for message in second_payload["messages"]][-2:] == [
        "assistant",
        "tool",
    ]


def test_zeta_agent_turn_records_tool_result_derivation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "README.md"
    target.write_text("README\n", encoding="utf-8")
    store = zeta_trace.InMemoryStore()
    responses = iter([read_tool_call_response(target), {"content": "done"}])

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        lambda messages, **kwargs: next(responses),
    )
    monkeypatch.setattr(
        zeta_agent,
        "analyze_tool",
        lambda name, params: {"valid": True, "resolved": True},
    )
    monkeypatch.setattr(
        zeta_agent,
        "run_tool",
        lambda name, params: read_tool_payload(target),
    )

    result = zeta_agent.run_agent_turn(
        "inspect",
        [],
        zeta_agent.AgentConfig(allowed_tools=("read",), max_turns=2),
        prompt_builder=zeta_prompt.PromptBuilder(store=store),
    )

    assert_tool_result_derivation_graph(
        store,
        result,
        event_by_type(result.events, "tool_call"),
        event_by_type(result.events, "tool_result"),
    )


def test_zeta_agent_turn_wraps_model_request_in_status(monkeypatch) -> None:
    events: list[str] = []

    class Status:
        def __enter__(self) -> object:
            events.append("start")
            return self

        def __exit__(self, *exc: object) -> bool:
            events.append("stop")
            return False

    def fake_chat_completion_messages(
        *args: object, **kwargs: object
    ) -> dict[str, Any]:
        del args, kwargs
        assert events == ["start"]
        return {"content": "done"}

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        fake_chat_completion_messages,
    )

    result = zeta_agent.run_agent_turn(
        "answer",
        [],
        zeta_agent.AgentConfig(),
        model_status=Status,
    )

    assert result.final_text == "done"
    assert events == ["start", "stop"]


def test_zeta_agent_turn_forwards_content_deltas_and_marks_final(monkeypatch) -> None:
    sink = DeltaSink()

    def fake_chat_completion_messages(
        *args: object,
        **kwargs: object,
    ) -> dict[str, Any]:
        del args
        stream_sink = required_stream_sink(kwargs)
        stream_sink.content_delta("hel")
        stream_sink.content_delta("lo")
        return {"content": "hello"}

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        fake_chat_completion_messages,
    )

    result = zeta_agent.run_agent_turn(
        "answer",
        [],
        zeta_agent.AgentConfig(max_turns=1),
        stream_sink=sink,
    )

    assert sink.deltas == ["hel", "lo"]
    assert result.final_text == "hello"
    assert result.final_text_streamed is True


def test_zeta_agent_turn_stops_status_before_first_stream_delta(monkeypatch) -> None:
    events: list[str] = []

    class Status:
        def __enter__(self) -> object:
            events.append("start")
            return self

        def __exit__(self, *exc: object) -> bool:
            events.append("stop")
            return False

    class AssertingSink:
        def content_delta(self, text: str) -> None:
            assert text == "done"
            assert events == ["start", "stop"]
            events.append("delta")

    def fake_chat_completion_messages(
        *args: object,
        **kwargs: object,
    ) -> dict[str, Any]:
        del args
        stream_sink = required_stream_sink(kwargs)
        stream_sink.content_delta("done")
        return {"content": "done"}

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        fake_chat_completion_messages,
    )

    result = zeta_agent.run_agent_turn(
        "answer",
        [],
        zeta_agent.AgentConfig(max_turns=1),
        model_status=Status,
        stream_sink=AssertingSink(),
    )

    assert result.final_text == "done"
    assert events == ["start", "stop", "delta"]


def test_zeta_agent_turn_uses_request_model(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_model_endpoint_open(selected_url: str | None = None) -> bool:
        captured["endpoint_url"] = selected_url
        return True

    def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        **kwargs: object,
    ) -> dict[str, Any]:
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return {"content": "done"}

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", fake_model_endpoint_open)
    monkeypatch.setattr(
        zeta_agent, "chat_completion_messages", fake_chat_completion_messages
    )

    result = zeta_agent.run_agent_turn(
        "answer",
        [],
        zeta_agent.AgentConfig(
            allowed_tools=("read",),
            max_turns=1,
            model_name="fast-model",
            model_url="http://127.0.0.1:8081/v1/chat/completions",
        ),
    )

    assert result.final_text == "done"
    assert captured["endpoint_url"] == "http://127.0.0.1:8081/v1/chat/completions"
    kwargs = cast(dict[str, Any], captured["kwargs"])
    assert kwargs["selected_model"] == "fast-model"
    assert kwargs["selected_url"] == "http://127.0.0.1:8081/v1/chat/completions"


def test_zeta_agent_turn_runs_multiple_read_only_tools_in_order(monkeypatch) -> None:
    responses = iter(
        [
            {
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "read",
                            "arguments": '{"path":"README.md"}',
                        },
                    },
                    {
                        "id": "call-2",
                        "type": "function",
                        "function": {
                            "name": "ls",
                            "arguments": '{"path":"src"}',
                        },
                    },
                ]
            },
            {"content": "done"},
        ]
    )
    ran: list[tuple[str, dict[str, Any]]] = []

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        lambda *args, **kwargs: next(responses),
    )
    monkeypatch.setattr(
        zeta_agent,
        "analyze_tool",
        lambda name, params: {"valid": True, "resolved": True},
    )

    def fake_run_tool(name: str, params: dict[str, Any]) -> dict[str, Any]:
        ran.append((name, params))
        return {"ok": True, "content": [{"type": "text", "text": name}]}

    monkeypatch.setattr(zeta_agent, "run_tool", fake_run_tool)

    result = zeta_agent.run_agent_turn(
        "inspect",
        [],
        zeta_agent.AgentConfig(allowed_tools=("read", "ls"), max_turns=2),
    )

    assert ran == [
        ("read", {"path": "README.md"}),
        ("ls", {"path": "src"}),
    ]
    assert result.final_text == "done"
    assert [
        event["name"] for event in result.events if event.get("type") == "tool_call"
    ] == ["read", "ls"]


def test_zeta_agent_turn_streams_text_between_tool_turns(monkeypatch) -> None:
    sink = DeltaSink()
    responses = iter(
        [
            {
                "content": "I'll inspect README.",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "read",
                            "arguments": '{"path":"README.md"}',
                        },
                    }
                ],
            },
            {"content": "It is a README."},
        ]
    )

    def fake_chat_completion_messages(
        *args: object,
        **kwargs: object,
    ) -> dict[str, Any]:
        del args
        response = next(responses)
        stream_sink = kwargs.get("stream_sink")
        if response.get("content") and stream_sink is not None:
            stream_sink = cast(zeta_model.ChatCompletionStreamSink, stream_sink)
            stream_sink.content_delta(str(response["content"]))
        return response

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        fake_chat_completion_messages,
    )
    monkeypatch.setattr(
        zeta_agent,
        "analyze_tool",
        lambda name, params: {"valid": True, "resolved": True},
    )
    monkeypatch.setattr(
        zeta_agent,
        "run_tool",
        lambda name, params: {
            "ok": True,
            "content": [{"type": "text", "text": "README"}],
        },
    )

    result = zeta_agent.run_agent_turn(
        "inspect",
        [],
        zeta_agent.AgentConfig(allowed_tools=("read",), max_turns=2),
        stream_sink=sink,
    )

    assert sink.deltas == ["I'll inspect README.", "It is a README."]
    assert result.final_text == "It is a README."
    assert result.final_text_streamed is True
    assert result.events[0]["content"] == "I'll inspect README."


def test_zeta_agent_turn_does_not_duplicate_current_objective(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        **kwargs: object,
    ) -> dict[str, Any]:
        del kwargs
        captured["messages"] = messages
        return {"content": "done"}

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        fake_chat_completion_messages,
    )

    result = zeta_agent.run_agent_turn(
        "inspect the repo",
        [],
        zeta_agent.AgentConfig(allowed_tools=("read",), max_turns=1),
    )

    assert result.final_text == "done"
    messages = cast(list[dict[str, Any]], captured["messages"])
    prompt_messages = [
        message
        for message in messages
        if message.get("role") == "user"
        and "Objective:\ninspect the repo" in str(message.get("content"))
    ]
    assert len(prompt_messages) == 1


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


def test_zeta_agent_turn_orders_follow_up_history_before_current_events(
    monkeypatch,
) -> None:
    captured: list[list[dict[str, Any]]] = []
    responses = iter(
        [
            {
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "read",
                            "arguments": '{"path":"DECISIONS.md"}',
                        },
                    }
                ]
            },
            {"content": "Improve the decision log."},
        ]
    )

    def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        **kwargs: object,
    ) -> dict[str, Any]:
        del kwargs
        captured.append(messages)
        return next(responses)

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        fake_chat_completion_messages,
    )
    monkeypatch.setattr(
        zeta_agent,
        "analyze_tool",
        lambda name, params: {"valid": True, "resolved": True},
    )
    monkeypatch.setattr(
        zeta_agent,
        "run_tool",
        lambda name, params: {
            "ok": True,
            "content": [{"type": "text", "text": "Decision log"}],
            "metadata": {"path": "DECISIONS.md"},
        },
    )

    result = zeta_agent.run_agent_turn(
        "How would you improve it?",
        [
            {"role": "user", "content": "What is this vault about?"},
            {"role": "assistant", "content": "It is a CEO vault."},
        ],
        zeta_agent.AgentConfig(allowed_tools=("read",), max_turns=2),
    )

    assert result.final_text == "Improve the decision log."
    second_turn = captured[1]
    assert [message["role"] for message in second_turn] == [
        "system",
        "user",
        "assistant",
        "user",
        "assistant",
        "tool",
    ]
    assert second_turn[1]["content"] == "What is this vault about?"
    assert second_turn[2]["content"] == "It is a CEO vault."
    assert "Objective:\nHow would you improve it?" in second_turn[3]["content"]
    assert second_turn[4]["tool_calls"][0]["id"] == "call-1"
    assert second_turn[5]["tool_call_id"] == "call-1"


def test_zeta_agent_turn_streams_tool_call_before_running_tool(monkeypatch) -> None:
    streamed: list[dict[str, Any]] = []

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        lambda *args, **kwargs: {
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "read",
                        "arguments": '{"path":"README.md"}',
                    },
                }
            ]
        },
    )
    monkeypatch.setattr(
        zeta_agent,
        "analyze_tool",
        lambda name, params: {"valid": True, "resolved": True},
    )

    def fake_run_tool(name: str, params: dict[str, Any]) -> dict[str, Any]:
        del name, params
        assert [event.get("type") for event in streamed] == [
            "assistant_message",
            "tool_call",
            "tool_analysis",
        ]
        return {"ok": True, "content": [{"type": "text", "text": "README"}]}

    monkeypatch.setattr(zeta_agent, "run_tool", fake_run_tool)

    result = zeta_agent.run_agent_turn(
        "inspect",
        [],
        zeta_agent.AgentConfig(allowed_tools=("read",), max_turns=1),
        event_sink=streamed.append,
    )

    assert result.events == streamed
    assert [event.get("type") for event in streamed] == [
        "assistant_message",
        "tool_call",
        "tool_analysis",
        "tool_result",
    ]


def test_zeta_agent_turn_stops_after_handoff_tool(monkeypatch) -> None:
    requests = 0

    def fake_chat_completion_messages(
        *args: object, **kwargs: object
    ) -> dict[str, Any]:
        nonlocal requests
        requests += 1
        return {
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "bash",
                        "arguments": '{"command":"uv run pytest"}',
                    },
                }
            ]
        }

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent, "chat_completion_messages", fake_chat_completion_messages
    )
    monkeypatch.setattr(
        zeta_agent,
        "analyze_tool",
        lambda name, params: {"valid": True, "resolved": True},
    )
    monkeypatch.setattr(
        zeta_agent,
        "run_tool",
        lambda name, params: {
            "ok": True,
            "handoff": {
                "type": SHELL_PROMPT_HANDOFF_TYPE,
                "command": "uv run pytest",
                "reason": "Run tests.",
            },
        },
    )

    result = zeta_agent.run_agent_turn(
        "test",
        [],
        zeta_agent.AgentConfig(allowed_tools=("bash",), max_turns=3),
    )

    assert requests == 1
    assert result.handoff == {
        "type": SHELL_PROMPT_HANDOFF_TYPE,
        "command": "uv run pytest",
        "reason": "Run tests.",
    }


def test_zeta_agent_direct_mode_continues_after_bash(monkeypatch) -> None:
    requests = 0
    responses = iter(
        [
            {
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "bash",
                            "arguments": '{"command":"printf direct-bash"}',
                        },
                    }
                ]
            },
            {"content": "done"},
        ]
    )

    def fake_chat_completion_messages(
        *args: object, **kwargs: object
    ) -> dict[str, Any]:
        nonlocal requests
        requests += 1
        return next(responses)

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent, "chat_completion_messages", fake_chat_completion_messages
    )

    result = zeta_agent.run_agent_turn(
        "test",
        [],
        zeta_agent.AgentConfig(
            allowed_tools=("bash",),
            execution_mode="direct",
            max_turns=3,
        ),
    )

    assert requests == 2
    assert result.handoff is None
    assert result.final_text == "done"
    tool_result = next(
        event for event in result.events if event.get("type") == "tool_result"
    )
    assert "direct-bash" in tool_result["result"]["content"][0]["text"]


def test_zeta_agent_turn_stops_after_default_max_turns(monkeypatch) -> None:
    requests = 0

    def fake_chat_completion_messages(*args: object, **kwargs: object) -> dict:
        del args, kwargs
        nonlocal requests
        requests += 1
        return {
            "tool_calls": [
                {
                    "id": f"call-{requests}",
                    "type": "function",
                    "function": {"name": "ls", "arguments": '{"path":"."}'},
                }
            ]
        }

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent, "chat_completion_messages", fake_chat_completion_messages
    )
    monkeypatch.setattr(
        zeta_agent, "run_tool", lambda name, params, **kwargs: {"ok": True}
    )

    result = zeta_agent.run_agent_turn(
        "test",
        [],
        zeta_agent.AgentConfig(allowed_tools=("ls",)),
    )

    assert requests == zeta_agent.DEFAULT_MAX_TURNS
    assert result.final_text == ""


def test_zeta_agent_turn_converts_tool_crash_to_error_result(monkeypatch) -> None:
    responses = iter(
        [
            {
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "read",
                            "arguments": '{"path":"x"}',
                        },
                    }
                ]
            },
            {"content": "recovered"},
        ]
    )

    def crash_run_tool(name: str, params: dict[str, Any], **kwargs: object) -> dict:
        raise ValueError("boom")

    def fake_chat_completion_messages(*args: object, **kwargs: object) -> dict:
        del args, kwargs
        return next(responses)

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent, "chat_completion_messages", fake_chat_completion_messages
    )
    monkeypatch.setattr(zeta_agent, "run_tool", crash_run_tool)

    result = zeta_agent.run_agent_turn(
        "test",
        [],
        zeta_agent.AgentConfig(allowed_tools=("read",), max_turns=3),
    )

    assert result.final_text == "recovered"
    tool_result = next(
        event for event in result.events if event.get("type") == "tool_result"
    )
    assert tool_result["result"]["ok"] is False
    assert tool_result["result"]["error"]["code"] == "tool-crashed"
    assert "boom" in tool_result["result"]["error"]["message"]


def test_zeta_agent_turn_rejects_schema_mismatch_before_running(monkeypatch) -> None:
    ran = False

    def fail_run_tool(name: str, params: dict[str, Any]) -> dict[str, Any]:
        nonlocal ran
        ran = True
        return {"ok": True}

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        lambda *args, **kwargs: {
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "read",
                        "arguments": '{"path":"README.md","unexpected":true}',
                    },
                }
            ]
        },
    )
    monkeypatch.setattr(zeta_agent, "run_tool", fail_run_tool)

    result = zeta_agent.run_agent_turn(
        "inspect",
        [],
        zeta_agent.AgentConfig(allowed_tools=("read",), max_turns=1),
    )

    assert ran is False
    tool_result = next(
        event for event in result.events if event.get("type") == "tool_result"
    )
    assert tool_result["result"]["ok"] is False
    assert tool_result["result"]["error"]["code"] == "schema-mismatch"


def test_zeta_agent_turn_rejects_disallowed_tool_before_running(monkeypatch) -> None:
    ran = False

    def fail_run_tool(name: str, params: dict[str, Any]) -> dict[str, Any]:
        nonlocal ran
        ran = True
        return {"ok": True}

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        lambda *args, **kwargs: {
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "bash",
                        "arguments": '{"command":"uv run pytest"}',
                    },
                }
            ]
        },
    )
    monkeypatch.setattr(zeta_agent, "run_tool", fail_run_tool)

    result = zeta_agent.run_agent_turn(
        "inspect",
        [],
        zeta_agent.AgentConfig(allowed_tools=("read",), max_turns=1),
    )

    assert ran is False
    tool_result = next(
        event for event in result.events if event.get("type") == "tool_result"
    )
    assert tool_result["result"]["ok"] is False
    assert tool_result["result"]["error"]["code"] == "disallowed-tool"


def test_zeta_project_context_loads_global_to_local(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    global_context = home / ".zeta"
    root = tmp_path / "repo"
    child = root / "pkg"
    global_context.mkdir(parents=True)
    child.mkdir(parents=True)
    (global_context / "AGENTS.md").write_text("global instructions\n", encoding="utf-8")
    (root / "AGENTS.md").write_text("root instructions\n", encoding="utf-8")
    (child / "AGENTS.md").write_text("child instructions\n", encoding="utf-8")
    (child / "CLAUDE.md").write_text("ignored instructions\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(child)

    context = zeta.load_project_context()

    assert context.index("global instructions") < context.index("root instructions")
    assert context.index("root instructions") < context.index("child instructions")
    assert "AGENTS.md" in context
    assert "CLAUDE.md" not in context
    assert "ignored instructions" not in context


def test_zeta_project_context_requires_exact_agents_filename(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "repo"
    project.mkdir()
    (project / "AGENTS.MD").write_text("uppercase ignored\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(project)

    context = zeta.load_project_context()

    assert "uppercase ignored" not in context


def test_zeta_project_context_ignores_missing_global_directory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "repo"
    project.mkdir()
    (project / "AGENTS.md").write_text("project instructions\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(project)

    context = zeta.load_project_context()

    assert "project instructions" in context


def write_skill(
    root: Path,
    name: str,
    *,
    description: str = "Use this skill.",
    body: str = "Skill body.\n",
    metadata_name: str | None = None,
    disabled: bool = False,
) -> Path:
    skill = root / name
    skill.mkdir(parents=True)
    metadata = [
        "---",
        f"description: {description}",
    ]
    if metadata_name is not None:
        metadata.append(f"name: {metadata_name}")
    if disabled:
        metadata.append("disable-model-invocation: true")
    metadata.append("---")
    (skill / "SKILL.md").write_text(
        "\n".join(metadata) + "\n" + body,
        encoding="utf-8",
    )
    return skill


def test_zeta_skill_discovery_loads_user_and_project_skills(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "repo"
    child = project / "pkg"
    child.mkdir(parents=True)
    write_skill(home / ".zeta" / "skills", "zeta-skill")
    write_skill(home / ".agents" / "skills", "agents-skill")
    write_skill(project / ".agents" / "skills", "project-skill")
    write_skill(child / ".agents" / "skills", "child-skill")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(child)

    catalog = zeta_skills.discover_skills()

    assert set(catalog.skills) == {
        "zeta-skill",
        "agents-skill",
        "project-skill",
        "child-skill",
    }
    assert catalog.diagnostics == []


def test_zeta_skill_collision_precedence_and_duplicate_canonical_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "repo"
    child = project / "pkg"
    child.mkdir(parents=True)
    write_skill(home / ".zeta" / "skills", "shared", description="zeta")
    write_skill(home / ".agents" / "skills", "shared", description="agents")
    write_skill(project / ".agents" / "skills", "shared", description="outer")
    write_skill(child / ".agents" / "skills", "shared", description="inner")
    original = write_skill(home / ".zeta" / "skills", "dupe")
    link = home / ".agents" / "skills" / "dupe-link"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(original, target_is_directory=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(child)

    catalog = zeta_skills.discover_skills()

    assert catalog.skills["shared"].description == "inner"
    assert sum(1 for skill in catalog.skills.values() if skill.name == "dupe") == 1


def test_zeta_skill_discovery_reports_invalid_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "repo"
    project.mkdir()
    write_skill(home / ".zeta" / "skills", "bad-name", metadata_name="Bad_Name")
    write_skill(home / ".zeta" / "skills", "missing-description", description="")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(project)

    catalog = zeta_skills.discover_skills()

    assert catalog.skills == {}
    assert len(catalog.diagnostics) == 2
    assert "invalid skill name" in catalog.diagnostics[0].message
    assert "missing non-empty description" in catalog.diagnostics[1].message


def test_zeta_system_prompt_advertises_enabled_skills_only_with_read(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "repo"
    project.mkdir()
    enabled = write_skill(
        home / ".zeta" / "skills",
        "enabled-skill",
        description="Do enabled work.",
    )
    write_skill(home / ".zeta" / "skills", "hidden-skill", disabled=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(project)

    prompt = zeta.zeta_system_prompt(allowed_tools=("read", "ls"))
    no_read_prompt = zeta.zeta_system_prompt(allowed_tools=("ls",))

    assert "<available_skills>" in prompt
    assert "name: enabled-skill" in prompt
    assert "description: Do enabled work." in prompt
    assert f"location: {enabled}" in prompt
    assert "hidden-skill" not in prompt
    assert "<available_skills>" not in no_read_prompt


def test_zeta_tools_list_exposes_v1_builtins() -> None:
    data = zeta_tools.tools_list()
    names = {tool["name"] for tool in data["tools"]}
    assert {"read", "grep", "ls", "bash", "edit", "write"} <= names
    assert data["tools"][0]["origin"] == "builtin"


def test_zeta_grep_metadata_guides_model_tool_choice() -> None:
    metadata = zeta_tools.tool_metadata("grep")
    schema = metadata["schema"]

    assert (
        metadata["description"]
        == "Search file contents recursively. Use before read when looking for symbols, errors, strings, or definitions."
    )
    assert schema["properties"]["pattern"]["description"] == (
        "Text or regular expression to search for."
    )
    assert schema["properties"]["path"]["description"] == (
        "File or directory to search. Defaults to the current working directory."
    )
    assert schema["properties"]["limit"]["description"] == (
        "Maximum number of matching lines to return."
    )


def write_cli_plugin(
    path: Path,
    *,
    name: str = "docs_search",
    invalid_metadata: bool = False,
    sleep_metadata: bool = False,
    fail_run: bool = False,
) -> None:
    metadata = {
        "name": name,
        "description": "Search project docs.",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["query"],
            "properties": {"query": {"type": "string"}},
        },
        "interactive": False,
    }
    script = f"""
from __future__ import annotations

import json
import sys
import time

if "--metadata" in sys.argv:
    if {sleep_metadata!r}:
        time.sleep(1)
    if {invalid_metadata!r}:
        print("not json")
    else:
        print(json.dumps({metadata!r}))
    raise SystemExit(0)

params = json.loads(sys.stdin.read() or "{{}}")
if "--analyze" in sys.argv:
    print(json.dumps({{
        "valid": True,
        "resolved": True,
        "effects": [{{
            "kind": "search",
            "resource": "path",
            "target": params["query"],
            "certainty": "certain",
        }}],
        "diagnostics": [],
    }}))
else:
    if {fail_run!r}:
        print("execution failed", file=sys.stderr)
        raise SystemExit(7)
    print(json.dumps({{
        "ok": True,
        "content": [{{"type": "text", "text": "docs:" + params["query"]}}],
        "metadata": {{"query": params["query"]}},
    }}))
"""
    path.write_text(script, encoding="utf-8")


def write_tools_config(
    home: Path,
    command: list[str],
    *,
    timeout_ms: int = 30_000,
) -> None:
    config_dir = home / ".zeta"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_dir.joinpath("tools.toml").write_text(
        "\n".join(
            [
                "[[tools]]",
                f"command = {json.dumps(command)}",
                f"timeout_ms = {timeout_ms}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_zeta_plugin_tool_flows_through_registry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    script = tmp_path / "plugin.py"
    write_cli_plugin(script)
    write_tools_config(home, [sys.executable, str(script)])
    monkeypatch.setenv("HOME", str(home))

    tools = zeta_tools.tools_list()["tools"]
    plugin = next(tool for tool in tools if tool["name"] == "docs_search")
    assert plugin["origin"] == "plugin"
    assert plugin["plugin"] == sys.executable

    descriptors = zeta_tools.model_tool_descriptors(("docs_search",))
    assert descriptors == [
        {
            "type": "function",
            "function": {
                "name": "docs_search",
                "description": "Search project docs.",
                "parameters": plugin["schema"],
            },
        }
    ]
    assert validate_tool_args("docs_search", {}) == [
        "$: 'query' is a required property"
    ]
    assert validate_tool_args("docs_search", {"query": "install"}) == []

    analysis = zeta_tools.analyze_tool("docs_search", {"query": "install"})
    assert analysis["valid"] is True
    assert analysis["effects"][0]["target"] == "install"

    data = zeta_tools.run_tool("docs_search", {"query": "install"})
    assert data["ok"] is True
    assert data["content"][0]["text"] == "docs:install"


def test_zeta_plugin_name_collision_is_ignored(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    script = tmp_path / "plugin.py"
    write_cli_plugin(script, name="read")
    write_tools_config(home, [sys.executable, str(script)])
    monkeypatch.setenv("HOME", str(home))

    data = zeta_tools.tools_list()
    read_tools = [tool for tool in data["tools"] if tool["name"] == "read"]
    assert len(read_tools) == 1
    assert read_tools[0]["origin"] == "builtin"
    assert data["diagnostics"][0]["code"] == "plugin-name-collision"


def test_zeta_plugin_invalid_metadata_reports_diagnostic(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    script = tmp_path / "plugin.py"
    write_cli_plugin(script, invalid_metadata=True)
    write_tools_config(home, [sys.executable, str(script)])
    monkeypatch.setenv("HOME", str(home))

    data = zeta_tools.tools_list()
    assert "docs_search" not in {tool["name"] for tool in data["tools"]}
    assert data["diagnostics"][0]["code"] == "plugin-metadata-invalid-json"


def test_zeta_plugin_missing_command_reports_diagnostic(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    write_tools_config(home, [str(tmp_path / "missing-tool")])
    monkeypatch.setenv("HOME", str(home))

    data = zeta_tools.tools_list()
    assert data["diagnostics"][0]["code"] == "plugin-metadata-failed"


def test_zeta_plugin_metadata_timeout_reports_diagnostic(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    script = tmp_path / "plugin.py"
    write_cli_plugin(script, sleep_metadata=True)
    write_tools_config(home, [sys.executable, str(script)], timeout_ms=10)
    monkeypatch.setenv("HOME", str(home))

    data = zeta_tools.tools_list()
    assert data["diagnostics"][0]["code"] == "plugin-metadata-timeout"


def test_zeta_plugin_nonzero_execution_returns_tool_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    script = tmp_path / "plugin.py"
    write_cli_plugin(script, fail_run=True)
    write_tools_config(home, [sys.executable, str(script)])
    monkeypatch.setenv("HOME", str(home))

    data = zeta_tools.run_tool("docs_search", {"query": "install"})
    assert data["ok"] is False
    assert data["error"]["code"] == "plugin-run-failed"
    assert "status 7" in data["error"]["message"]


def test_zeta_tool_read_schema_and_run(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("hello zeta\n", encoding="utf-8")

    assert zeta_tools.tool_metadata("read")["schema"]["required"] == ["path"]

    data = zeta_tools.run_tool("read", {"path": str(target)})
    assert data["ok"] is True
    assert data["content"][0]["text"] == "hello zeta\n"


def test_zeta_tool_read_offset_and_limit_select_lines(tmp_path: Path) -> None:
    target = tmp_path / "lines.txt"
    target.write_text("one\ntwo\nthree\nfour\nfive\n", encoding="utf-8")

    data = zeta_tools.run_tool("read", {"path": str(target), "offset": 1, "limit": 2})

    assert data["ok"] is True
    assert data["content"][0]["text"] == "two\nthree\n"
    assert data["metadata"]["offset"] == 1
    assert data["metadata"]["limit"] == 2


def test_zeta_tool_read_limit_past_end_returns_remaining_lines(tmp_path: Path) -> None:
    target = tmp_path / "short.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")

    data = zeta_tools.run_tool("read", {"path": str(target), "offset": 1, "limit": 10})

    assert data["content"][0]["text"] == "beta\n"


def test_zeta_tool_read_rejects_binary_file(tmp_path: Path) -> None:
    target = tmp_path / "image.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")

    data = zeta_tools.run_tool("read", {"path": str(target)})

    assert data["ok"] is False
    assert data["error"]["code"] == "binary-file"


def test_zeta_tool_read_caps_returned_characters(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(read_tool, "MAX_READ_CHARS", 100)
    target = tmp_path / "wide.txt"
    target.write_text("x" * 1_000 + "\n", encoding="utf-8")

    data = zeta_tools.run_tool("read", {"path": str(target)})

    assert data["ok"] is True
    assert len(data["content"][0]["text"]) == 100
    assert data["metadata"]["truncated"] is True


def test_zeta_tool_grep_reports_total_limited_metadata(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("needle one\nneedle two\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("needle three\n", encoding="utf-8")

    data = zeta_tools.run_tool(
        "grep", {"path": str(tmp_path), "pattern": "needle", "limit": 2}
    )

    assert data["ok"] is True
    assert data["content"][0]["text"].count("needle") == 2
    assert data["metadata"]["matches"] == 2
    assert data["metadata"]["files"] == 1
    assert data["metadata"]["limit"] == 2
    assert data["metadata"]["truncated"] is True
    assert data["metadata"]["match_limit_reached"] is True


def test_zeta_tool_grep_reports_content_truncation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "long.txt"
    target.write_text("needle " + ("x" * 80) + "\n", encoding="utf-8")
    monkeypatch.setattr(grep_tool, "MAX_TOOL_RESULT_CHARS", 20)

    data = zeta_tools.run_tool("grep", {"path": str(target), "pattern": "needle"})

    assert data["ok"] is True
    assert len(data["content"][0]["text"]) == 20
    assert data["metadata"]["matches"] == 1
    assert data["metadata"]["files"] == 1
    assert data["metadata"]["truncated"] is True
    assert data["metadata"]["match_limit_reached"] is False
    assert data["metadata"]["content_truncated"] is True


def test_zeta_tool_grep_fallback_searches_without_ripgrep(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("needle two\n", encoding="utf-8")
    (tmp_path / "a.txt").write_text("needle one\n", encoding="utf-8")

    def missing_rg(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("rg")

    monkeypatch.setattr(grep_tool.subprocess, "Popen", missing_rg)

    data = zeta_tools.run_tool("grep", {"path": str(tmp_path), "pattern": "needle"})

    assert data["ok"] is True
    assert data["metadata"]["matches"] == 2
    lines = data["content"][0]["text"].splitlines()
    assert lines[0].endswith("needle one")
    assert lines[1].endswith("needle two")


def test_zeta_tool_grep_fallback_stops_at_limit(tmp_path: Path, monkeypatch) -> None:
    for index in range(20):
        (tmp_path / f"file-{index:02}.txt").write_text("needle\n", encoding="utf-8")

    def missing_rg(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("rg")

    monkeypatch.setattr(grep_tool.subprocess, "Popen", missing_rg)

    data = zeta_tools.run_tool(
        "grep", {"path": str(tmp_path), "pattern": "needle", "limit": 3}
    )

    assert data["metadata"]["matches"] == 3
    assert data["metadata"]["truncated"] is True


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is not installed")
def test_zeta_tool_grep_reports_invalid_pattern_error(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("text\n", encoding="utf-8")

    data = zeta_tools.run_tool("grep", {"path": str(tmp_path), "pattern": "("})

    assert data["ok"] is False
    assert data["metadata"]["status"] not in {0, 1}
    assert data["content"][0]["text"]


def test_zeta_tool_bash_returns_handoff() -> None:
    data = zeta_tools.run_tool(
        "bash", {"command": "uv run pytest", "reason": "Run tests."}
    )

    assert data["handoff"]["command"] == "uv run pytest"
    assert data["handoff"]["reason"] == "Run tests."


def test_zeta_tool_bash_direct_executes_command() -> None:
    data = zeta_tools.run_tool(
        "bash",
        {"command": "printf direct-bash"},
        execution_mode="direct",
    )

    assert data["ok"] is True
    assert data["metadata"]["mode"] == "direct"
    assert data["metadata"]["status"] == 0
    assert "stdout" not in data["metadata"]
    assert "stderr" not in data["metadata"]
    assert "direct-bash" in data["content"][0]["text"]


def test_zeta_tool_bash_direct_replaces_invalid_utf8_output() -> None:
    data = zeta_tools.run_tool(
        "bash",
        {"command": "printf '\\xff\\xfe'"},
        execution_mode="direct",
    )

    assert data["ok"] is True
    assert "�" in data["content"][0]["text"]


def test_zeta_tool_bash_direct_kills_command_on_timeout(monkeypatch) -> None:
    monkeypatch.setattr(bash_tool, "DEFAULT_TIMEOUT_SECONDS", 0.2)

    data = zeta_tools.run_tool(
        "bash",
        {"command": "sleep 5"},
        execution_mode="direct",
    )

    assert data["ok"] is False
    assert data["error"]["code"] == "bash-timeout"
    assert data["metadata"]["timed_out"] is True
    assert "timed out" in data["content"][0]["text"]


def test_zeta_tool_bash_direct_truncates_large_output() -> None:
    data = zeta_tools.run_tool(
        "bash",
        {"command": "head -c 100000 /dev/zero | tr '\\0' 'x'"},
        execution_mode="direct",
    )

    assert data["ok"] is True
    assert data["metadata"]["stdout_truncated"] is True
    text = data["content"][0]["text"]
    assert len(text) < 2 * bash_tool.MAX_OUTPUT_CHARS
    assert "truncated" in text


def test_zeta_tool_write_direct_writes_file(tmp_path: Path) -> None:
    target = tmp_path / "direct.txt"

    data = zeta_tools.run_tool(
        "write",
        {"path": str(target), "content": "hello\n"},
        execution_mode="direct",
    )

    assert data["ok"] is True
    assert data["metadata"] == {"mode": "direct", "path": str(target)}
    assert target.read_text(encoding="utf-8") == "hello\n"


def test_sigil_display_summarizes_tool_results() -> None:
    assert sigil_display.tool_result_summary(
        "bash",
        {
            "ok": True,
            "handoff": {
                "type": SHELL_PROMPT_HANDOFF_TYPE,
                "command": "uv run pytest",
            },
        },
    ) == ["staged"]
    assert sigil_display.tool_result_summary(
        "bash",
        {
            "ok": True,
            "metadata": {"mode": "direct", "status": 0},
        },
    ) == ["succeeded"]
    assert sigil_display.tool_result_summary(
        "bash",
        {
            "ok": False,
            "metadata": {"mode": "direct", "status": 2},
        },
    ) == ["failed · exit 2"]
    assert sigil_display.tool_result_summary(
        "read",
        {"ok": True, "content": [{"type": "text", "text": "a\nb\n"}]},
    ) == ["2 lines"]
    assert sigil_display.tool_result_summary(
        "read",
        {
            "ok": False,
            "error": {
                "code": "read-failed",
                "message": "[Errno 2] No such file or directory: 'missing.md'",
            },
        },
    ) == ["read-failed: [Errno 2] No such file or directory: 'missing.md'"]
    assert sigil_display.tool_result_summary(
        "write",
        {
            "ok": True,
            "metadata": {"mode": "direct", "path": "notes.txt"},
        },
    ) == ["wrote · notes.txt"]
    assert sigil_display.tool_result_summary(
        "grep",
        {"ok": True, "content": [{"type": "text", "text": "a.py:1:x\nb.py:2:y\n"}]},
    ) == ["2 matches · 2 files"]
    assert sigil_display.tool_result_summary(
        "grep",
        {
            "ok": True,
            "content": [{"type": "text", "text": "a.py:1:x\n"}],
            "metadata": {"matches": 10, "files": 3, "truncated": True},
        },
    ) == ["10 matches · 3 files · truncated"]


def test_sigil_display_summarizes_current_context_estimate() -> None:
    line = sigil_display.context_usage_line(
        {
            "usage": {
                "prompt_tokens": 18_432,
                "completion_tokens": 391,
                "total_tokens": 18_823,
            },
            "model_context_tokens": 262_144,
        }
    )

    assert line == "context  [█░░░░░░░░░░░░░░░░░░░] 7%"
    assert (
        sigil_display.context_usage_line(
            {"usage": {"prompt_tokens": 18_432, "completion_tokens": 391}}
        )
        == ""
    )
    assert (
        sigil_display.context_usage_line(
            {"estimated_context_tokens": 200, "model_context_tokens": 1_000}
        )
        == "context  [████░░░░░░░░░░░░░░░░] 20% est."
    )


def test_sigil_display_context_usage_footer_estimates_tool_result_tokens() -> None:
    output = StringIO()
    footer = sigil_display.ContextUsageFooter(output)
    base_telemetry = {
        "usage": {"prompt_tokens": 100, "completion_tokens": 0},
        "model_context_tokens": 1_000,
    }
    result = {"ok": True, "content": [{"type": "text", "text": "x" * 200}]}

    footer.update(base_telemetry)
    footer.update_for_tool_result(None, result)

    estimated_tokens = 100 + sigil_display.estimated_tool_result_context_tokens(result)
    assert footer.current_line() == sigil_display.context_usage_line(
        {
            "estimated_context_tokens": estimated_tokens,
            "model_context_tokens": 1_000,
        }
    )
    assert footer.current_line().endswith(" est.")
    assert output.getvalue() == ""

    real_telemetry = {
        "usage": {"prompt_tokens": 250, "completion_tokens": 10},
        "model_context_tokens": 1_000,
    }
    footer.finalize(real_telemetry)

    assert output.getvalue() == "context  [█████░░░░░░░░░░░░░░░] 26%\n"


def test_sigil_display_tool_result_telemetry_replaces_stale_estimates() -> None:
    footer = sigil_display.ContextUsageFooter(StringIO())
    stale_result = {"ok": True, "content": [{"type": "text", "text": "x" * 400}]}
    fresh_result = {"ok": True, "content": [{"type": "text", "text": "y" * 40}]}
    fresh_telemetry = {
        "usage": {"prompt_tokens": 400, "completion_tokens": 20},
        "model_context_tokens": 1_000,
    }

    footer.update(
        {
            "usage": {"prompt_tokens": 100, "completion_tokens": 0},
            "model_context_tokens": 1_000,
        }
    )
    footer.update_for_tool_result(None, stale_result)
    footer.update_for_tool_result(fresh_telemetry, fresh_result)

    expected_tokens = 420 + sigil_display.estimated_tool_result_context_tokens(
        fresh_result
    )
    assert footer.current_line() == sigil_display.context_usage_line(
        {
            "estimated_context_tokens": expected_tokens,
            "model_context_tokens": 1_000,
        }
    )
    assert sigil_display.context_usage_line({"model_context_tokens": 262_144}) == ""
    assert (
        sigil_display.context_usage_line(
            {
                "usage": {"prompt_tokens": 18_432},
                "model_context_tokens": 262_144,
            }
        )
        == ""
    )


def test_sigil_display_context_usage_footer_is_ephemeral_for_tty(
    monkeypatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    telemetry = {
        "usage": {"prompt_tokens": 18_432, "completion_tokens": 391},
        "model_context_tokens": 262_144,
    }
    output = TtyBuffer()
    footer = sigil_display.ContextUsageFooter(output)

    assert footer.update(telemetry)
    assert not output.getvalue().endswith("\n")
    assert output.getvalue() == "\r\x1b[2Kcontext  [█░░░░░░░░░░░░░░░░░░░] 7%"

    footer.clear()
    assert output.getvalue().endswith("\r\x1b[2K")
    assert footer.finalize(telemetry)
    assert output.getvalue().endswith("context  [█░░░░░░░░░░░░░░░░░░░] 7%\n")


def test_sigil_display_context_usage_footer_prints_final_only_for_non_tty() -> None:
    telemetry = {
        "usage": {"prompt_tokens": 18_432, "completion_tokens": 391},
        "model_context_tokens": 262_144,
    }
    output = StringIO()
    footer = sigil_display.ContextUsageFooter(output)

    assert not footer.update(telemetry)
    assert output.getvalue() == ""
    assert footer.finalize()
    assert output.getvalue() == "context  [█░░░░░░░░░░░░░░░░░░░] 7%\n"


def test_sigil_display_stream_renderer_factory_selects_output_mode() -> None:
    assert isinstance(
        sigil_display.create_stream_renderer(StringIO()),
        sigil_display.TerminalStreamRenderer,
    )
    assert sigil_display.create_stream_renderer(StringIO(), json_output=True) is None
    assert isinstance(
        sigil_display.create_stream_renderer(TtyBuffer()),
        sigil_display.RichStreamRenderer,
    )


def test_sigil_display_rich_stream_renderer_renders_markdown() -> None:
    output = TtyBuffer()
    renderer = sigil_display.RichStreamRenderer(output, refresh_interval=0)

    renderer.content_delta("Hello ")
    renderer.content_delta("**world**")
    renderer.finish()

    text = visible_terminal_text(output.getvalue())
    assert "Hello world" in text
    assert "**world**" not in text


def test_sigil_display_rich_stream_renderer_wraps_with_left_padding() -> None:
    output = TtyBuffer()
    renderer = sigil_display.RichStreamRenderer(
        output,
        width=24,
        refresh_interval=0,
    )

    renderer.content_delta("alpha beta gamma delta epsilon")
    renderer.finish()

    lines = [
        line.rstrip()
        for line in visible_terminal_text(output.getvalue()).splitlines()
        if line.strip()
    ]
    assert "  alpha beta gamma delta" in lines
    assert "  epsilon" in lines


def test_sigil_display_rich_stream_renderer_finalizes_trace_boundaries() -> None:
    output = TtyBuffer()
    renderer = sigil_display.RichStreamRenderer(output, refresh_interval=0)

    renderer.content_delta("First")
    renderer.ensure_trace_boundary()
    assert renderer.live is None
    assert renderer.buffer == []
    assert renderer.wrote_text is False

    renderer.content_delta("Second")
    renderer.finish()

    text = visible_terminal_text(output.getvalue())
    assert "First" in text
    assert "Second" in text
    assert renderer.live is None


def test_sigil_display_thinking_status_updates_and_clears(monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    output = TtyBuffer()
    now = 0.0

    def clock() -> float:
        return now

    with sigil_display.ThinkingStatus(output, interval=60, clock=clock) as status:
        now = 10.4
        status.refresh()

    text = output.getvalue()
    assert "\n\r\x1b[2K  thinking 0s" in text
    assert "\n\r\x1b[2K  thinking 10s" in text
    assert text.endswith("\r\x1b[2K\x1b[1A\r\x1b[2K")


def test_sigil_display_thinking_status_is_muted(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    output = TtyBuffer()

    with sigil_display.ThinkingStatus(output, interval=60):
        pass

    assert f"{sigil_display.MUTED}  thinking 0s{sigil_display.RESET}" in (
        output.getvalue()
    )


def test_sigil_display_thinking_status_includes_context_detail(
    monkeypatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    output = TtyBuffer()

    with sigil_display.ThinkingStatus(
        output,
        interval=60,
        detail=lambda: "context  [█░░░░░░░░░░░░░░░░░░░] 7%",
    ):
        pass

    assert (
        "\n\r\x1b[2K  context  [█░░░░░░░░░░░░░░░░░░░] 7%\n  thinking 0s"
        in output.getvalue()
    )
    assert output.getvalue().endswith("\r\x1b[2K\x1b[1A\r\x1b[2K\x1b[1A\r\x1b[2K")


def test_sigil_display_thinking_status_skips_non_tty() -> None:
    output = StringIO()

    with sigil_display.ThinkingStatus(output):
        pass

    assert output.getvalue() == ""


def test_sigil_display_summarizes_shell_results() -> None:
    assert sigil_display.shell_result_summary(
        {
            "type": "tool_result",
            "result": {
                "outcome": SHELL_HANDOFF_OUTCOME_EXECUTED,
                "executed_command": "uv run pytest",
                "status": 0,
                "shell_turns": [{"command": "uv run pytest"}],
            },
        }
    ) == ["❯ shell  captured", "  uv run pytest", "  exit 0 · 1 shell turn"]
    assert sigil_display.shell_result_summary(
        {
            "type": "tool_result",
            "result": {
                "outcome": SHELL_HANDOFF_OUTCOME_CANCELLED,
                "expected_command": "uv run pytest",
                "actual_command": "uv run pytest -q",
            },
        }
    ) == [
        "❯ shell  changed",
        "  expected: uv run pytest",
        "  ran:      uv run pytest -q",
    ]


def test_zeta_tool_ls_lists_directory_contents(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    data = zeta_tools.run_tool("ls", {"path": str(tmp_path)})

    assert data["ok"] is True
    assert data["content"][0]["text"].splitlines() == [
        "-\tdir\tsrc/",
        "10\tfile\tpyproject.toml",
    ]
    assert data["metadata"]["entries"] == 2


def test_zeta_tool_ls_can_filter_large_files_without_shelling_out(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "large-object").write_bytes(b"x" * 12)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "large.bin").write_bytes(b"x" * 12)
    (tmp_path / "small.txt").write_bytes(b"x" * 4)

    data = zeta_tools.run_tool(
        "ls",
        {
            "path": str(tmp_path),
            "recursive": True,
            "min_size_bytes": 10,
            "exclude": [".git"],
        },
    )

    assert data["ok"] is True
    assert data["content"][0]["text"].splitlines() == ["12\tfile\tsrc/large.bin"]
    assert data["metadata"]["entries"] == 1
    assert data["metadata"]["exclude"] == [".git"]


def test_zeta_tool_edit_writes_patch_artifact(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("old\n", encoding="utf-8")

    data = zeta_tools.run_tool(
        "edit", {"location": str(target), "old": "old\n", "new": "new\n"}
    )
    artifact = Path(data["handoff"]["artifact"])
    assert artifact.exists()
    patch = artifact.read_text(encoding="utf-8")
    assert "-old\n" in patch
    assert "+new\n" in patch
    assert data["handoff"]["command"].startswith("git apply ")


def test_zeta_tool_edit_accepts_exact_replacement(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("hello\nold\nbye\n", encoding="utf-8")
    payload = {
        "location": str(target),
        "old": "old\n",
        "new": "new\n",
        "reason": "Replace one line.",
    }

    data = zeta_tools.run_tool("edit", payload)

    assert validate_tool_args("edit", payload) == []
    artifact = Path(data["handoff"]["artifact"])
    patch = artifact.read_text(encoding="utf-8")
    assert data["handoff"]["command"].startswith("git apply ")
    assert data["handoff"]["reason"] == "Replace one line."
    assert "-old\n" in patch
    assert "+new\n" in patch


def test_zeta_tool_edit_direct_replace_writes_file(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("hello\nold\nbye\n", encoding="utf-8")

    data = zeta_tools.run_tool(
        "edit",
        {"location": str(target), "old": "old\n", "new": "new\n"},
        edit_mode="direct_replace",
    )

    assert data["ok"] is True
    assert target.read_text(encoding="utf-8") == "hello\nnew\nbye\n"
    assert "handoff" not in data
    metadata = data["metadata"]
    assert metadata["mode"] == "direct_replace"
    artifact = Path(metadata["artifact"])
    assert artifact.exists()
    assert "+new\n" in artifact.read_text(encoding="utf-8")


def test_zeta_tool_edit_rejects_non_utf8_file(tmp_path: Path) -> None:
    target = tmp_path / "latin1.txt"
    target.write_bytes(b"caf\xe9 old\n")

    data = zeta_tools.run_tool(
        "edit",
        {"location": str(target), "old": "old", "new": "new"},
        edit_mode="direct_replace",
    )

    assert data["ok"] is False
    assert data["error"]["code"] == "not-utf8"
    assert target.read_bytes() == b"caf\xe9 old\n"


def test_zeta_tool_edit_direct_reports_write_failure(tmp_path: Path) -> None:
    target = tmp_path / "readonly.txt"
    target.write_text("old\n", encoding="utf-8")
    target.chmod(0o444)

    data = zeta_tools.run_tool(
        "edit",
        {"location": str(target), "old": "old\n", "new": "new\n"},
        edit_mode="direct_replace",
    )

    target.chmod(0o644)
    assert data["ok"] is False
    assert data["error"]["code"] == "write-failed"
    assert target.read_text(encoding="utf-8") == "old\n"


def test_zeta_tool_edit_rejects_ambiguous_exact_replacement(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("old\nold\n", encoding="utf-8")

    data = zeta_tools.run_tool(
        "edit", {"location": str(target), "old": "old\n", "new": "new\n"}
    )

    assert data["ok"] is False
    assert data["error"]["code"] == "old-text-not-unique"


def test_zeta_tool_edit_marks_no_newline_exact_replacement(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("old", encoding="utf-8")

    data = zeta_tools.run_tool(
        "edit", {"location": str(target), "old": "old", "new": "new"}
    )

    artifact = Path(data["handoff"]["artifact"])
    patch = artifact.read_text(encoding="utf-8")
    assert "-old\n\\ No newline at end of file\n" in patch
    assert "+new\n\\ No newline at end of file\n" in patch


def test_zeta_timeline_record_and_tail(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "zeta-test")

    zeta.record_event({"type": "tool_call", "name": "read"})

    events = zeta.current_timeline(1)
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

    zeta.record_event({"type": "user_message", "content": "first"})
    store = zeta_trace.default_store()
    first_head = store.get_ref(zeta_timeline.run_head_ref("zeta-test"))
    assert first_head is not None
    store.set_ref("run/custom/head", first_head)

    zeta.record_event({"type": "assistant_message", "content": "second"})

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


def test_sigil_zeta_step_writes_handoff_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    handoff_file = tmp_path / "handoff.json"

    monkeypatch.setattr(turn_routes, "ensure_server", lambda: True)
    monkeypatch.setattr(
        zeta_runner,
        "run_agent_turn",
        lambda *args, **kwargs: zeta_agent.AgentTurnResult(
            events=[
                {
                    "type": "tool_call",
                    "id": "call-1",
                    "tool_call_id": "call-1",
                    "name": "bash",
                    "input": {"command": "uv run pytest", "reason": "Run tests."},
                },
                {
                    "type": "tool_result",
                    "tool_call_id": "call-1",
                    "name": "bash",
                    "result": {
                        "ok": True,
                        "handoff": {
                            "type": SHELL_PROMPT_HANDOFF_TYPE,
                            "command": "uv run pytest",
                            "reason": "Run tests.",
                        },
                    },
                },
            ],
            handoff={
                "type": SHELL_PROMPT_HANDOFF_TYPE,
                "command": "uv run pytest",
                "reason": "Run tests.",
            },
        ),
    )

    result = CliRunner().invoke(
        sigil_cli,
        ["zeta-step", "--handoff-file", str(handoff_file), "repair"],
    )

    assert result.exit_code == 0
    assert "❯ bash   uv run pytest  (staged)" in result.output
    assert json.loads(handoff_file.read_text(encoding="utf-8")) == {
        "type": SHELL_PROMPT_HANDOFF_TYPE,
        "command": "uv run pytest",
        "reason": "Run tests.",
    }


def test_sigil_zeta_step_keeps_trace_off_stdout(monkeypatch) -> None:
    monkeypatch.setattr(turn_routes, "ensure_server", lambda: True)
    monkeypatch.setattr(
        zeta_runner,
        "run_agent_turn",
        lambda *args, **kwargs: zeta_agent.AgentTurnResult(
            final_text="summary",
            events=[
                {
                    "type": "tool_call",
                    "tool_call_id": "call-1",
                    "name": "read",
                    "input": {"path": "README.md"},
                },
                {
                    "type": "tool_result",
                    "tool_call_id": "call-1",
                    "name": "read",
                    "result": {
                        "ok": True,
                        "content": [{"type": "text", "text": "a\n"}],
                    },
                },
                {"type": "assistant_message", "content": "summary"},
            ],
        ),
    )

    result = CliRunner().invoke(sigil_cli, ["zeta-step", "summarize"])

    assert result.exit_code == 0
    assert result.stdout == "\nsummary\n\n"
    assert "❯ read" in result.stderr
    assert "❯ read" not in result.stdout


def test_zeta_agent_step_separates_trace_from_final_answer(
    monkeypatch,
    capsys,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config
        captured["context"] = kwargs.get("context")
        return zeta_agent.AgentTurnResult(
            final_text="The answer.",
            events=[
                {
                    "type": "tool_call",
                    "tool_call_id": "call-1",
                    "name": "read",
                    "input": {"path": "README.md"},
                },
                {
                    "type": "tool_result",
                    "tool_call_id": "call-1",
                    "name": "read",
                    "result": {
                        "ok": True,
                        "content": [{"type": "text", "text": "a\n"}],
                    },
                },
                {"type": "assistant_message", "content": "The answer."},
            ],
        )

    monkeypatch.setattr(turn_routes, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)
    monkeypatch.setattr(zeta_runner.runtime, "load_project_context", lambda: "ctx")

    code = zeta_runner.run_agent_step("answer me", glyph=",,")

    assert code == 0
    output = capsys.readouterr()
    assert output.out == "\nThe answer.\n\n"
    assert "❯ read" in output.err
    assert captured["context"] == "ctx"


def test_zeta_agent_step_renders_context_usage_on_trace_stream(
    monkeypatch,
    capsys,
) -> None:
    telemetry = {
        "usage": {
            "prompt_tokens": 18_432,
            "completion_tokens": 391,
            "total_tokens": 18_823,
        },
        "model_context_tokens": 262_144,
    }

    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config, kwargs
        return zeta_agent.AgentTurnResult(
            final_text="done",
            model_telemetry=telemetry,
        )

    monkeypatch.setattr(turn_routes, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)

    code = zeta_runner.run_agent_step("answer me", glyph=",,")

    assert code == 0
    output = capsys.readouterr()
    assert output.out == "\ndone\n\n"
    assert "context  [█░░░░░░░░░░░░░░░░░░░] 7%" in output.err
    assert "18,823 / 262,144 tokens" not in output.err


def test_zeta_agent_step_renders_context_usage_after_buffered_answer(
    monkeypatch,
    capsys,
) -> None:
    telemetry = {
        "usage": {"prompt_tokens": 18_432, "completion_tokens": 391},
        "model_context_tokens": 262_144,
    }

    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config, kwargs
        return zeta_agent.AgentTurnResult(
            final_text="done",
            model_telemetry=telemetry,
        )

    monkeypatch.setattr(turn_routes, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)

    code = zeta_runner.run_agent_step("answer me", glyph=",,", trace_output=sys.stdout)

    assert code == 0
    output = capsys.readouterr().out
    assert output.index("done") < output.index("context  [█░░░░░░░░░░░░░░░░░░░] 7%")


def test_zeta_agent_step_renders_context_usage_at_bottom_after_tools(
    monkeypatch,
    capsys,
) -> None:
    tool_telemetry = {
        "usage": {"prompt_tokens": 123, "completion_tokens": 4},
        "model_context_tokens": 262_144,
    }

    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config
        event_sink = cast("Callable[[dict[str, Any]], None]", kwargs.get("event_sink"))
        first_call = {
            "type": "tool_call",
            "id": "call-1",
            "tool_call_id": "call-1",
            "name": "read",
            "input": {"path": "a.md"},
        }
        first_result = {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "name": "read",
            "result": {
                "ok": True,
                "content": [{"type": "text", "text": "A\n"}],
            },
        }
        second_call = {
            "type": "tool_call",
            "id": "call-2",
            "tool_call_id": "call-2",
            "name": "read",
            "input": {"path": "b.md"},
        }
        second_result = {
            "type": "tool_result",
            "tool_call_id": "call-2",
            "name": "read",
            "result": {
                "ok": True,
                "content": [{"type": "text", "text": "B\n"}],
            },
            "model_telemetry": tool_telemetry,
        }
        events = [first_call, first_result, second_call, second_result]
        for event in events:
            event_sink(event)
        return zeta_agent.AgentTurnResult(
            final_text="done",
            events=events,
            model_telemetry={
                "usage": {"prompt_tokens": 456, "completion_tokens": 4},
                "model_context_tokens": 262_144,
            },
        )

    monkeypatch.setattr(turn_routes, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)

    code = zeta_runner.run_agent_step(
        "inspect",
        glyph=",,",
        trace_output=sys.stdout,
    )

    assert code == 0
    output = capsys.readouterr().out
    assert ("❯ read   a.md  (1 lines)\n❯ read   b.md  (1 lines)") in output
    assert output.count("context  [") == 1
    assert "123 / 262,144 tokens" not in output
    assert output.index("done") < output.index("context  [░░░░░░░░░░░░░░░░░░░░] 0%")


def test_zeta_agent_step_does_not_pass_current_user_event_as_transcript(
    monkeypatch,
    capsys,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, config, kwargs
        captured["transcript"] = transcript
        return zeta_agent.AgentTurnResult(final_text="done")

    monkeypatch.setattr(turn_routes, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)

    code = zeta_runner.run_agent_step("answer me", glyph=",,")

    assert code == 0
    assert cast(list[dict[str, Any]], captured["transcript"]) == []
    assert zeta.current_timeline()[-1]["type"] == "user_message"
    assert capsys.readouterr().out == "\ndone\n\n"


def test_zeta_agent_step_double_comma_uses_handoff_mode(
    monkeypatch,
    capsys,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, kwargs
        captured["config"] = config
        return zeta_agent.AgentTurnResult(final_text="done")

    monkeypatch.setattr(turn_routes, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)

    code = zeta_runner.run_agent_step("review", glyph=",,")

    assert code == 0
    config = cast(zeta_agent.AgentConfig, captured["config"])
    assert config.edit_mode == "review_patch"
    assert config.execution_mode == "handoff"
    assert config.max_turns is None


def test_zeta_answer_route_has_no_default_step_budget(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, kwargs
        captured["config"] = config
        return zeta_agent.AgentTurnResult(final_text="done")

    monkeypatch.setattr(turn_routes, "ensure_server", lambda: True)
    monkeypatch.setattr(answers_runner, "run_agent_turn", fake_run_agent_turn)

    code = answers_runner.run_tool_answer("system", "question")

    assert code == 0
    config = cast(zeta_agent.AgentConfig, captured["config"])
    assert config.max_turns is None


def test_zeta_agent_step_double_comma_stages_bash_handoff(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    handoff_file = tmp_path / "handoff.json"

    responses = iter(
        [
            {
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "bash",
                            "arguments": '{"command":"echo Review complete"}',
                        },
                    }
                ]
            }
        ]
    )

    monkeypatch.setattr(turn_routes, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        lambda *args, **kwargs: next(responses),
    )

    code = zeta_runner.run_agent_step(
        "Review the changes",
        glyph=",,",
        allowed_tools=("bash",),
        handoff_path=handoff_file,
        handoff_output="summary",
    )

    assert code == 0
    output = capsys.readouterr()
    assert "(staged)" in output.err
    assert "exit 0" not in output.err
    assert "Review complete" not in output.out
    assert json.loads(handoff_file.read_text(encoding="utf-8"))["command"] == (
        "echo Review complete"
    )


def test_zeta_agent_step_prints_tool_start_while_agent_runs(
    monkeypatch,
    capsys,
) -> None:
    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config
        event_sink = cast("Callable[[dict[str, Any]], None]", kwargs.get("event_sink"))
        assert callable(event_sink)
        tool_call = {
            "type": "tool_call",
            "id": "call-1",
            "tool_call_id": "call-1",
            "name": "read",
            "input": {"path": "README.md"},
        }
        event_sink(tool_call)
        assert "❯ read   README.md" in capsys.readouterr().err
        tool_result = {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "name": "read",
            "result": {
                "ok": True,
                "content": [{"type": "text", "text": "README"}],
            },
        }
        event_sink(tool_result)
        return zeta_agent.AgentTurnResult(
            final_text="It is a README.",
            events=[tool_call, tool_result],
        )

    monkeypatch.setattr(turn_routes, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)

    for glyph in (",,", ",,,"):
        code = zeta_runner.run_agent_step("inspect", glyph=glyph)

        assert code == 0
        assert capsys.readouterr().out == "\nIt is a README.\n\n"


def test_zeta_agent_step_streams_text_before_tool_trace(
    monkeypatch,
    capsys,
) -> None:
    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config
        stream_sink = required_stream_sink(kwargs)
        stream_sink.content_delta("I'll inspect README.")
        event_sink = cast("Callable[[dict[str, Any]], None]", kwargs.get("event_sink"))
        tool_call = {
            "type": "tool_call",
            "id": "call-1",
            "tool_call_id": "call-1",
            "name": "read",
            "input": {"path": "README.md"},
        }
        event_sink(tool_call)
        return zeta_agent.AgentTurnResult(
            final_text="It is a README.",
            events=[tool_call],
        )

    monkeypatch.setattr(turn_routes, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)

    code = zeta_runner.run_agent_step("inspect", glyph=",,")

    assert code == 0
    output = capsys.readouterr()
    assert output.out.startswith("\nI'll inspect README.\n\n")
    assert "\nIt is a README.\n" in output.out
    assert "❯ read   README.md" in output.err


@pytest.mark.parametrize("glyph", [",,", ",,,"])
def test_zeta_agent_step_separates_tool_result_from_later_streamed_text(
    glyph: str,
    monkeypatch,
    capsys,
) -> None:
    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config
        stream_sink = required_stream_sink(kwargs)
        stream_sink.content_delta("I'll inspect README.")
        event_sink = cast("Callable[[dict[str, Any]], None]", kwargs.get("event_sink"))
        tool_call = {
            "type": "tool_call",
            "id": "call-1",
            "tool_call_id": "call-1",
            "name": "read",
            "input": {"path": "README.md"},
        }
        tool_result = {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "name": "read",
            "result": {
                "ok": True,
                "content": [{"type": "text", "text": "README\n"}],
            },
        }
        event_sink(tool_call)
        event_sink(tool_result)
        stream_sink.content_delta("It is a README.")
        return zeta_agent.AgentTurnResult(
            final_text="It is a README.",
            events=[tool_call, tool_result],
            final_text_streamed=True,
        )

    monkeypatch.setattr(turn_routes, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)

    code = zeta_runner.run_agent_step(
        "inspect",
        glyph=glyph,
        trace_output=sys.stdout,
    )

    assert code == 0
    output = capsys.readouterr().out
    assert output == (
        "\nI'll inspect README.\n\n❯ read   README.md  (1 lines)\n\nIt is a README.\n\n"
    )


def test_zeta_agent_step_does_not_insert_blank_lines_between_tool_calls(
    monkeypatch,
    capsys,
) -> None:
    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config
        event_sink = cast("Callable[[dict[str, Any]], None]", kwargs.get("event_sink"))
        events = [
            {
                "type": "tool_call",
                "id": "call-1",
                "tool_call_id": "call-1",
                "name": "read",
                "input": {"path": "a.md"},
            },
            {
                "type": "tool_result",
                "tool_call_id": "call-1",
                "name": "read",
                "result": {
                    "ok": True,
                    "content": [{"type": "text", "text": "A\n"}],
                },
            },
            {
                "type": "tool_call",
                "id": "call-2",
                "tool_call_id": "call-2",
                "name": "read",
                "input": {"path": "b.md"},
            },
            {
                "type": "tool_result",
                "tool_call_id": "call-2",
                "name": "read",
                "result": {
                    "ok": True,
                    "content": [{"type": "text", "text": "B\n"}],
                },
            },
        ]
        for event in events:
            event_sink(event)
        return zeta_agent.AgentTurnResult(final_text="Done.", events=events)

    monkeypatch.setattr(turn_routes, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)

    code = zeta_runner.run_agent_step(
        "inspect",
        glyph=",,",
        trace_output=sys.stdout,
    )

    assert code == 0
    assert capsys.readouterr().out == (
        "❯ read   a.md  (1 lines)\n❯ read   b.md  (1 lines)\n\nDone.\n\n"
    )


def test_zeta_agent_step_aligns_thinking_status_after_tool_trace(
    monkeypatch,
    capsys,
) -> None:
    output = TtyBuffer()

    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config
        event_sink = cast("Callable[[dict[str, Any]], None]", kwargs.get("event_sink"))
        tool_call = {
            "type": "tool_call",
            "id": "call-1",
            "tool_call_id": "call-1",
            "name": "read",
            "input": {"path": "README.md"},
        }
        tool_result = {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "name": "read",
            "result": {
                "ok": True,
                "content": [{"type": "text", "text": "README\n"}],
            },
        }
        event_sink(tool_call)
        event_sink(tool_result)
        model_status = cast("Callable[[], Any]", kwargs.get("model_status"))
        with model_status():
            pass
        return zeta_agent.AgentTurnResult(final_text="Done.", events=[])

    monkeypatch.setattr(turn_routes, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)

    code = zeta_runner.run_agent_step(
        "inspect",
        glyph=",,",
        trace_output=output,
    )

    assert code == 0
    assert capsys.readouterr().out == "\nDone.\n\n"
    trace_text = visible_terminal_text(output.getvalue())
    assert "❯ read   README.md  (1 lines)\n\n  thinking 0s" in trace_text


def test_zeta_agent_step_prints_final_answer_after_direct_edit(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(turn_routes, "ensure_server", lambda: True)
    monkeypatch.setattr(
        zeta_runner,
        "run_agent_turn",
        lambda *args, **kwargs: zeta_agent.AgentTurnResult(
            final_text="edited and verified",
            events=[
                {
                    "type": "tool_call",
                    "id": "call-1",
                    "tool_call_id": "call-1",
                    "name": "edit",
                    "input": {"location": "a.txt", "old": "old", "new": "new"},
                },
                {
                    "type": "tool_result",
                    "tool_call_id": "call-1",
                    "name": "edit",
                    "result": {
                        "ok": True,
                        "metadata": {"mode": "direct_replace", "location": "a.txt"},
                    },
                },
                {"type": "assistant_message", "content": "edited and verified"},
            ],
        ),
    )

    code = zeta_runner.run_agent_step("edit", glyph=",,,")

    assert code == 0
    output = capsys.readouterr()
    assert output.out == "\nedited and verified\n\n"
    assert "❯ edit   a.txt  (applied · a.txt)" in output.err


def test_sigil_handoff_shell_turn_records_recent_turn(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "zeta-test")

    result = CliRunner().invoke(
        sigil_cli,
        [
            "handoff",
            "shell-turn",
            "--command",
            "uv run pytest",
            "--status",
            "1",
            "--cwd",
            "/repo",
        ],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["type"] == "shell_turn_recorded"
    assert data["command"] == "uv run pytest"
    turns = recent_turns()
    assert len(turns) == 1
    assert turns[0]["command"] == "uv run pytest"
    assert turns[0]["status"] == 1
    assert turns[0]["turn_cwd"] == "/repo"


def test_zeta_edit_analysis_reports_location() -> None:
    data = zeta_tools.analyze_tool(
        "edit",
        {"location": "src/new.py", "old": "x", "new": "y"},
    )
    assert data["valid"] is True
    assert data["resolved"] is True
    assert [effect["target"] for effect in data["effects"]] == ["src/new.py"]


def test_zeta_agent_direct_edit_stops_after_applying(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "a.txt"
    target.write_text("old\n", encoding="utf-8")
    requests = 0

    def fake_chat_completion_messages(
        *args: object,
        **kwargs: object,
    ) -> dict[str, Any]:
        nonlocal requests
        requests += 1
        return {
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "edit",
                        "arguments": json.dumps(
                            {
                                "location": str(target),
                                "old": "old\n",
                                "new": "new\n",
                            }
                        ),
                    },
                }
            ]
        }

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        fake_chat_completion_messages,
    )

    result = zeta_agent.run_agent_turn(
        "edit",
        [],
        zeta_agent.AgentConfig(
            allowed_tools=("edit",),
            edit_mode="direct_replace",
            max_turns=3,
        ),
    )

    assert requests == 1
    assert result.handoff is None
    assert target.read_text(encoding="utf-8") == "new\n"
    tool_result = next(
        event for event in result.events if event.get("type") == "tool_result"
    )
    assert tool_result["result"]["ok"] is True
    assert tool_result["result"]["metadata"]["mode"] == "direct_replace"


def test_zeta_agent_direct_mode_continues_after_edit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "a.txt"
    target.write_text("old\n", encoding="utf-8")
    requests = 0

    responses = iter(
        [
            {
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "edit",
                            "arguments": json.dumps(
                                {
                                    "location": str(target),
                                    "old": "old\n",
                                    "new": "new\n",
                                }
                            ),
                        },
                    }
                ]
            },
            {"content": "done"},
        ]
    )

    def fake_chat_completion_messages(
        *args: object,
        **kwargs: object,
    ) -> dict[str, Any]:
        nonlocal requests
        requests += 1
        return next(responses)

    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        fake_chat_completion_messages,
    )

    result = zeta_agent.run_agent_turn(
        "edit",
        [],
        zeta_agent.AgentConfig(
            allowed_tools=("edit",),
            edit_mode="direct_replace",
            execution_mode="direct",
            max_turns=3,
        ),
    )

    assert requests == 2
    assert result.final_text == "done"
    assert target.read_text(encoding="utf-8") == "new\n"


def test_zeta_step_glyph_selects_edit_mode() -> None:
    assert zeta_runner.edit_mode_for_glyph(",,") == "review_patch"
    assert zeta_runner.edit_mode_for_glyph(",,,") == "direct_replace"
    assert zeta_runner.execution_mode_for_glyph(",,") == "handoff"
    assert zeta_runner.execution_mode_for_glyph(",,,") == "direct"


def test_zeta_system_prompt_is_product_neutral_and_dynamic() -> None:
    prompt = zeta.zeta_system_prompt(allowed_tools=("read", "ls"))
    grep_prompt = zeta.zeta_system_prompt(allowed_tools=("read", "grep", "ls"))

    assert "Sigil" not in prompt
    assert "Preserve user changes." in prompt
    assert "Do not commit unless asked." in prompt
    assert "more local instructions\noverride earlier ones" in prompt
    assert "Available tools:" in prompt
    assert "- read(path, offset?, limit?): Read a UTF-8 text file." in prompt
    assert "- ls(path?, limit?, recursive?, min_size_bytes?, exclude?):" in prompt
    assert "Use `grep` to locate occurrences" not in prompt
    assert (
        "Use `grep` to locate occurrences before reading files when the target "
        "text/symbol is known."
    ) in grep_prompt
    assert (
        "- grep(pattern, path?, limit?): Search file contents recursively. Use "
        "before read when looking for symbols, errors, strings, or definitions."
    ) in grep_prompt
    assert '"parameters"' not in prompt
    assert "- bash(" not in prompt


def test_zeta_skill_directive_expands_in_context_message(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "repo"
    project.mkdir()
    skill = write_skill(
        project / ".agents" / "skills",
        "reviewer",
        description="Review code.",
        body="# Reviewer\nRead references/sample.md first.\n",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(project)

    message = zeta_prompt.zeta_context_message("@reviewer: inspect the patch")

    assert f'<skill name="reviewer" location="{skill}">' in message
    assert f"References are relative to {skill}." in message
    assert "# Reviewer\nRead references/sample.md first." in message
    assert "description: Review code." not in message
    assert "\n\ninspect the patch\n\ncwd:" in message


def test_zeta_skill_directive_leaves_unknown_skill_unchanged(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "repo"
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(project)

    message = zeta_prompt.zeta_context_message("@missing: inspect")

    assert "Objective:\n@missing: inspect" in message


def test_zeta_skill_directive_leaves_old_skill_form_unchanged(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "repo"
    project.mkdir()
    write_skill(project / ".agents" / "skills", "reviewer")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(project)

    message = zeta_prompt.zeta_context_message("@skill reviewer inspect")

    assert "Objective:\n@skill reviewer inspect" in message
    assert '<skill name="reviewer"' not in message


def test_zeta_skill_directive_leaves_bare_handle_unchanged(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "repo"
    project.mkdir()
    write_skill(project / ".agents" / "skills", "reviewer")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(project)

    message = zeta_prompt.zeta_context_message("@reviewer inspect")

    assert "Objective:\n@reviewer inspect" in message
    assert '<skill name="reviewer"' not in message


def test_zeta_skill_directive_expands_through_agent_step_route(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "repo"
    project.mkdir()
    write_skill(
        project / ".agents" / "skills",
        "route-skill",
        description="Route work.",
        body="Route skill body.\n",
    )
    captured: dict[str, str] = {}

    def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        **kwargs: object,
    ) -> dict[str, Any]:
        del kwargs
        captured["user"] = str(messages[1]["content"])
        return {"content": "done"}

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(project)
    monkeypatch.setattr(turn_routes, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        fake_chat_completion_messages,
    )

    code = zeta_runner.run_agent_step("@route-skill: do route work", glyph=",,")

    assert code == 0
    assert '<skill name="route-skill"' in captured["user"]
    assert "Route skill body." in captured["user"]
    assert "do route work" in captured["user"]


def test_zeta_agent_step_route_uses_active_session_model(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    write_models_config(
        home,
        """
[[models]]
name = "coder"
model = "coder-model"
url = "http://127.0.0.1:8082/v1/chat/completions"
""",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SIGIL_SESSION_ID", "agent-model")
    zeta_models.set_active_model_profile("coder")
    captured: dict[str, Any] = {}

    def fake_ensure_server(**kwargs: object) -> bool:
        captured["server"] = kwargs
        return True

    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, kwargs
        captured["config"] = config
        return zeta_agent.AgentTurnResult(final_text="done")

    monkeypatch.setattr(turn_routes, "ensure_server", fake_ensure_server)
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)

    code = zeta_runner.run_agent_step("do work", glyph=",,")

    assert code == 0
    assert capsys.readouterr().out == "\ndone\n\n"
    assert captured["server"] == {
        "selected_url": "http://127.0.0.1:8082/v1/chat/completions",
        "selected_model": "coder-model",
    }
    config = cast(zeta_agent.AgentConfig, captured["config"])
    assert config.model_profile == "coder"
    assert config.model_name == "coder-model"
    assert config.model_url == "http://127.0.0.1:8082/v1/chat/completions"


def test_zeta_skill_directive_expands_through_answer_route(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "repo"
    project.mkdir()
    write_skill(
        project / ".agents" / "skills",
        "answer-skill",
        description="Answer work.",
        body="Answer skill body.\n",
    )
    captured: dict[str, str] = {}

    def fake_chat_completion_messages(
        messages: list[dict[str, Any]],
        **kwargs: object,
    ) -> dict[str, Any]:
        del kwargs
        captured["user"] = str(messages[1]["content"])
        return {"content": "answered"}

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(project)
    monkeypatch.setattr(turn_routes, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_agent, "model_endpoint_open", lambda: True)
    monkeypatch.setattr(
        zeta_agent,
        "chat_completion_messages",
        fake_chat_completion_messages,
    )

    code = answers_runner.run_tool_answer(
        "system",
        "@answer-skill: do answer work",
        input_text="@answer-skill: do answer work",
    )

    assert code == 0
    assert '<skill name="answer-skill"' in captured["user"]
    assert "Answer skill body." in captured["user"]
    assert "do answer work" in captured["user"]


def test_zeta_answer_route_uses_active_session_model(
    tmp_path: Path,
    monkeypatch,
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
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SIGIL_SESSION_ID", "answer-model")
    zeta_models.set_active_model_profile("fast")
    captured: dict[str, Any] = {}

    def fake_ensure_server(**kwargs: object) -> bool:
        captured["server"] = kwargs
        return True

    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, kwargs
        captured["transcript"] = transcript
        captured["config"] = config
        return zeta_agent.AgentTurnResult(final_text="answered")

    monkeypatch.setattr(turn_routes, "ensure_server", fake_ensure_server)
    monkeypatch.setattr(answers_runner, "run_agent_turn", fake_run_agent_turn)

    code = answers_runner.run_tool_answer("system", "prompt")

    assert code == 0
    assert captured["server"] == {
        "selected_url": "http://127.0.0.1:8081/v1/chat/completions",
        "selected_model": "fast-model",
    }
    config = cast(zeta_agent.AgentConfig, captured["config"])
    assert config.model_profile == "fast"
    assert config.model_name == "fast-model"
    assert config.model_url == "http://127.0.0.1:8081/v1/chat/completions"
    transcript = cast(list[dict[str, Any]], captured["transcript"])
    assert transcript == []
    assert zeta.current_timeline()[-1]["model"] == {
        "profile": "fast",
        "model": "fast-model",
        "url": "http://127.0.0.1:8081/v1/chat/completions",
    }


def test_append_shell_result_appends_tool_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "zeta-test")
    zeta.record_event(
        {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "name": "bash",
            "result": {
                "ok": True,
                "handoff": {
                    "type": SHELL_PROMPT_HANDOFF_TYPE,
                    "command": "uv run pytest",
                    "reason": "Run tests.",
                },
            },
        }
    )
    record_turn("uv run pytest", 1, "/repo", stderr_snippet="test failed")

    event = sigil_handoff.append_shell_result()

    assert event["type"] == "tool_result"
    assert event["tool_call_id"] == "call-1"
    assert event["name"] == "bash"
    assert event["result"]["ok"] is True
    assert event["result"]["schema"] == SHELL_HANDOFF_RESULT_SCHEMA
    assert event["result"]["type"] == SHELL_HANDOFF_RESULT_TYPE
    assert event["result"]["outcome"] == SHELL_HANDOFF_OUTCOME_EXECUTED
    assert event["result"]["command"] == "uv run pytest"
    assert event["result"]["expected_command"] == "uv run pytest"
    assert event["result"]["executed_command"] == "uv run pytest"
    assert event["result"]["status"] == 1
    assert event["result"]["shell_turns"][0]["command"] == "uv run pytest"
    assert "uv run pytest (exit 1)" in event["result"]["content"][0]["text"]


def test_resolved_shell_handoff_context_keeps_tool_call_with_shell_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "zeta-test")
    zeta.record_event(
        {
            "type": "assistant_message",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "bash",
                        "arguments": '{"command":"uv run pytest"}',
                    },
                }
            ],
        }
    )
    zeta.record_event(
        {
            "type": "tool_call",
            "id": "call-1",
            "tool_call_id": "call-1",
            "name": "bash",
            "input": {"command": "uv run pytest"},
        }
    )
    zeta.record_event(
        {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "name": "bash",
            "result": {
                "ok": True,
                "handoff": {
                    "type": SHELL_PROMPT_HANDOFF_TYPE,
                    "command": "uv run pytest",
                    "reason": "Run tests.",
                },
            },
        }
    )
    record_turn("uv run pytest", 1, "/repo", stderr_snippet="test failed")

    sigil_handoff.append_shell_result()
    messages = zeta_timeline.chat_messages(zeta.current_timeline())

    assert messages[0]["role"] == "assistant"
    assert messages[0]["tool_calls"][0]["id"] == "call-1"
    tool_messages = [message for message in messages if message["role"] == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "call-1"
    tool_content = json.loads(tool_messages[0]["content"])
    assert tool_content["type"] == SHELL_HANDOFF_RESULT_TYPE
    assert tool_content["executed_command"] == "uv run pytest"


def test_sigil_transcript_shell_result_reports_extended_handoff_as_edited(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "zeta-test")
    zeta.record_event(
        {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "name": "bash",
            "result": {
                "ok": True,
                "handoff": {
                    "type": SHELL_PROMPT_HANDOFF_TYPE,
                    "command": "uv run pytest",
                    "reason": "Run tests.",
                },
            },
        }
    )
    record_turn("uv run pytest -q", 1, "/repo", stderr_snippet="test failed")

    event = sigil_handoff.append_shell_result()

    assert event["type"] == "tool_result"
    assert event["tool_call_id"] == "call-1"
    assert event["result"]["ok"] is True
    assert event["result"]["outcome"] == SHELL_HANDOFF_OUTCOME_EXECUTED
    assert event["result"]["edited"] is True
    assert event["result"]["expected_command"] == "uv run pytest"
    assert event["result"]["executed_command"] == "uv run pytest -q"
    assert "edited" in event["result"]["content"][0]["text"]


def test_sigil_transcript_shell_result_matches_despite_whitespace_edits(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "zeta-test")
    zeta.record_event(
        {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "name": "bash",
            "result": {
                "ok": True,
                "handoff": {
                    "type": SHELL_PROMPT_HANDOFF_TYPE,
                    "command": "uv run pytest",
                    "reason": "Run tests.",
                },
            },
        }
    )
    record_turn("uv  run   pytest ", 0, "/repo", stdout_snippet="191 passed")

    event = sigil_handoff.append_shell_result()

    assert event["result"]["outcome"] == SHELL_HANDOFF_OUTCOME_EXECUTED
    assert event["result"]["edited"] is False
    assert event["result"]["executed_command"] == "uv  run   pytest "


def test_sigil_transcript_shell_result_cancels_unrelated_command(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "zeta-test")
    zeta.record_event(
        {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "name": "bash",
            "result": {
                "ok": True,
                "handoff": {
                    "type": SHELL_PROMPT_HANDOFF_TYPE,
                    "command": "uv run pytest",
                    "reason": "Run tests.",
                },
            },
        }
    )
    record_turn("git status --short", 0, "/repo")

    event = sigil_handoff.append_shell_result()

    assert event["result"]["ok"] is False
    assert event["result"]["schema"] == SHELL_HANDOFF_RESULT_SCHEMA
    assert event["result"]["type"] == SHELL_HANDOFF_RESULT_TYPE
    assert event["result"]["outcome"] == SHELL_HANDOFF_OUTCOME_CANCELLED
    assert (
        event["result"]["cancellation_reason"]
        == SHELL_HANDOFF_CANCEL_EXPECTED_NOT_EXECUTED
    )
    assert event["result"]["expected_command"] == "uv run pytest"
    assert event["result"]["actual_command"] == "git status --short"
    assert event["result"]["shell_turns"][0]["command"] == "git status --short"


def test_sigil_transcript_shell_result_includes_intervening_shell_turns(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "zeta-test")
    zeta.record_event(
        {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "name": "bash",
            "result": {
                "ok": True,
                "handoff": {
                    "type": SHELL_PROMPT_HANDOFF_TYPE,
                    "command": "uv run pytest",
                    "reason": "Run tests.",
                },
            },
        }
    )
    record_turn("git status --short", 0, "/repo", stdout_snippet=" M README.md")
    record_turn("uv run pytest", 0, "/repo", stdout_snippet="191 passed")

    event = sigil_handoff.append_shell_result()

    assert event["result"]["outcome"] == SHELL_HANDOFF_OUTCOME_EXECUTED
    assert event["result"]["executed_command"] == "uv run pytest"
    assert [turn["command"] for turn in event["result"]["shell_turns"]] == [
        "git status --short",
        "uv run pytest",
    ]
    assert "1 user shell turn" in event["result"]["content"][0]["text"]


def test_sigil_transcript_shell_result_does_not_reuse_resolved_handoff(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "zeta-test")
    zeta.record_event(
        {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "name": "bash",
            "result": {
                "ok": True,
                "handoff": {
                    "type": SHELL_PROMPT_HANDOFF_TYPE,
                    "command": "uv run pytest",
                    "reason": "Run tests.",
                },
            },
        }
    )
    record_turn("uv run pytest", 0, "/repo", stdout_snippet="191 passed")

    first = sigil_handoff.append_shell_result()
    second = sigil_handoff.append_shell_result()

    assert first["type"] == "tool_result"
    assert first["result"]["outcome"] == SHELL_HANDOFF_OUTCOME_EXECUTED
    assert second["type"] == "shell_resume"
    assert second["result"]["outcome"] == SHELL_HANDOFF_OUTCOME_NO_PENDING
    assert second["result"]["shell_turns"][0]["command"] == "uv run pytest"


def test_zeta_question_loop_feeds_current_tool_result_to_next_step(
    monkeypatch,
    capsys,
) -> None:
    transcripts: list[list[dict[str, Any]]] = []

    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, config, kwargs
        transcripts.append(transcript)
        return zeta_agent.AgentTurnResult(
            final_text="It contains project metadata.",
            events=[
                {
                    "type": "assistant_message",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "read",
                                "arguments": '{"path":"pyproject.toml"}',
                            },
                        }
                    ],
                },
                {
                    "type": "tool_call",
                    "id": "call-1",
                    "tool_call_id": "call-1",
                    "name": "read",
                    "input": {"path": "pyproject.toml"},
                },
                {
                    "type": "tool_result",
                    "tool_call_id": "call-1",
                    "name": "read",
                    "result": {
                        "ok": True,
                        "content": [
                            {"type": "text", "text": "[project]\nname = 'sigil'\n"}
                        ],
                    },
                },
                {
                    "type": "assistant_message",
                    "content": "It contains project metadata.",
                },
            ],
        )

    monkeypatch.setattr(turn_routes, "ensure_server", lambda: True)
    monkeypatch.setattr(answers_runner, "run_agent_turn", fake_run_agent_turn)

    code = answers_runner.run_tool_answer(
        "question system",
        "What does pyproject.toml contain?",
    )

    assert code == 0
    output = capsys.readouterr().out
    assert "❯ read   pyproject.toml" in output
    assert "\n\nIt contains project metadata.\n" in output
    assert "project metadata" in output
    assert len(transcripts) == 1


def test_zeta_answer_route_prints_context_usage_and_records_telemetry(
    monkeypatch,
    capsys,
) -> None:
    telemetry = {
        "usage": {
            "prompt_tokens": 18_432,
            "completion_tokens": 391,
            "total_tokens": 18_823,
        },
        "model_context_tokens": 262_144,
    }

    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config, kwargs
        return zeta_agent.AgentTurnResult(
            final_text="It contains project metadata.",
            model_telemetry=telemetry,
        )

    monkeypatch.setattr(turn_routes, "ensure_server", lambda: True)
    monkeypatch.setattr(answers_runner, "run_agent_turn", fake_run_agent_turn)

    code = answers_runner.run_tool_answer(
        "question system",
        "What does pyproject.toml contain?",
    )

    assert code == 0
    output = capsys.readouterr().out
    assert "It contains project metadata." in output
    assert "context  [█░░░░░░░░░░░░░░░░░░░] 7%" in output
    assert "18,823 / 262,144 tokens" not in output
    assert output.index("It contains project metadata.") < output.index("context  [")
    answer_event = read_event_log()[-1]
    assert answer_event["usage"] == telemetry["usage"]
    assert answer_event["model_context_tokens"] == 262_144
    assert read_jsonl("last-tools.jsonl") == []


def test_zeta_answer_route_json_includes_context_telemetry(
    monkeypatch,
    capsys,
) -> None:
    telemetry = {
        "usage": {
            "prompt_tokens": 123,
            "completion_tokens": 4,
            "total_tokens": 127,
        },
        "model_context_tokens": 262_144,
    }

    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config, kwargs
        return zeta_agent.AgentTurnResult(
            final_text="buffered answer",
            model_telemetry=telemetry,
        )

    monkeypatch.setattr(turn_routes, "ensure_server", lambda: True)
    monkeypatch.setattr(answers_runner, "run_agent_turn", fake_run_agent_turn)

    code = answers_runner.run_tool_answer(
        "question system",
        "Question?",
        json_output=True,
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["answer"] == "buffered answer"
    assert payload["usage"] == telemetry["usage"]
    assert payload["model_context_tokens"] == 262_144
    assert payload["tools"] == []


def test_zeta_answer_route_streams_final_text_without_duplicate(
    monkeypatch,
    capsys,
) -> None:
    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config
        stream_sink = required_stream_sink(kwargs)
        assert isinstance(stream_sink, sigil_display.TraceAwareStreamRenderer)
        assert isinstance(stream_sink.renderer, sigil_display.TerminalStreamRenderer)
        stream_sink.content_delta("streamed answer")
        return zeta_agent.AgentTurnResult(
            final_text="streamed answer",
            events=[{"type": "assistant_message", "content": "streamed answer"}],
            final_text_streamed=True,
        )

    monkeypatch.setattr(turn_routes, "ensure_server", lambda: True)
    monkeypatch.setattr(answers_runner, "run_agent_turn", fake_run_agent_turn)

    code = answers_runner.run_tool_answer("question system", "Question?")

    assert code == 0
    assert capsys.readouterr().out == "\nstreamed answer\n\n"


def test_zeta_answer_route_streams_markdown_with_rich_for_tty(
    monkeypatch,
) -> None:
    output = TtyBuffer()

    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config
        stream_sink = required_stream_sink(kwargs)
        assert isinstance(stream_sink, sigil_display.TraceAwareStreamRenderer)
        assert isinstance(stream_sink.renderer, sigil_display.RichStreamRenderer)
        stream_sink.content_delta("**streamed** answer")
        return zeta_agent.AgentTurnResult(
            final_text="streamed answer",
            events=[{"type": "assistant_message", "content": "streamed answer"}],
            final_text_streamed=True,
        )

    monkeypatch.setattr(answers_runner.sys, "stdout", output)
    monkeypatch.setattr(turn_routes, "ensure_server", lambda: True)
    monkeypatch.setattr(answers_runner, "run_agent_turn", fake_run_agent_turn)

    code = answers_runner.run_tool_answer(
        "question system",
        "Question?",
        input_text="Question?",
    )

    assert code == 0
    assert "streamed answer" in visible_terminal_text(output.getvalue())
    turns = answers_runner.discussion_turns()
    assert [turn["content"] for turn in turns] == ["streamed answer"]


def test_zeta_answer_route_streams_text_before_tool_trace(
    monkeypatch,
    capsys,
) -> None:
    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config
        stream_sink = required_stream_sink(kwargs)
        stream_sink.content_delta("I'll inspect README.")
        event_sink = cast("Callable[[dict[str, Any]], None]", kwargs.get("event_sink"))
        tool_call = {
            "type": "tool_call",
            "id": "call-1",
            "tool_call_id": "call-1",
            "name": "read",
            "input": {"path": "README.md"},
        }
        tool_result = {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "name": "read",
            "result": {
                "ok": True,
                "content": [{"type": "text", "text": "README"}],
            },
        }
        event_sink(tool_call)
        event_sink(tool_result)
        stream_sink.content_delta("It is a README.")
        return zeta_agent.AgentTurnResult(
            final_text="It is a README.",
            events=[tool_call, tool_result],
            final_text_streamed=True,
        )

    monkeypatch.setattr(turn_routes, "ensure_server", lambda: True)
    monkeypatch.setattr(answers_runner, "run_agent_turn", fake_run_agent_turn)

    code = answers_runner.run_tool_answer("question system", "Question?")

    assert code == 0
    output = capsys.readouterr().out
    assert output == (
        "\nI'll inspect README.\n\n❯ read   README.md  (1 lines)\n\nIt is a README.\n\n"
    )
    assert '{"path"' not in output


def test_zeta_answer_route_renders_context_usage_at_bottom_after_tools(
    monkeypatch,
    capsys,
) -> None:
    telemetry = {
        "usage": {
            "prompt_tokens": 18_432,
            "completion_tokens": 391,
            "total_tokens": 18_823,
        },
        "model_context_tokens": 262_144,
    }

    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config
        event_sink = cast("Callable[[dict[str, Any]], None]", kwargs.get("event_sink"))
        first_call = {
            "type": "tool_call",
            "id": "call-1",
            "tool_call_id": "call-1",
            "name": "read",
            "input": {"path": "a.md"},
        }
        first_result = {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "name": "read",
            "result": {
                "ok": True,
                "content": [{"type": "text", "text": "A\n"}],
            },
        }
        second_call = {
            "type": "tool_call",
            "id": "call-2",
            "tool_call_id": "call-2",
            "name": "read",
            "input": {"path": "b.md"},
        }
        second_result = {
            "type": "tool_result",
            "tool_call_id": "call-2",
            "name": "read",
            "result": {
                "ok": True,
                "content": [{"type": "text", "text": "B\n"}],
            },
            "model_telemetry": telemetry,
        }
        events = [first_call, first_result, second_call, second_result]
        for event in events:
            event_sink(event)
        return zeta_agent.AgentTurnResult(
            final_text="It is a README.",
            events=events,
            model_telemetry=telemetry,
        )

    monkeypatch.setattr(turn_routes, "ensure_server", lambda: True)
    monkeypatch.setattr(answers_runner, "run_agent_turn", fake_run_agent_turn)

    code = answers_runner.run_tool_answer("question system", "Question?")

    assert code == 0
    output = capsys.readouterr().out
    assert ("❯ read   a.md  (1 lines)\n❯ read   b.md  (1 lines)") in output
    assert output.count("context  [") == 1
    assert output.index("It is a README.") < output.index(
        "context  [█░░░░░░░░░░░░░░░░░░░] 7%"
    )
    tools = read_jsonl("last-tools.jsonl")
    assert [(tool["type"], tool["tool"]) for tool in tools] == [
        ("tool_start", "read"),
        ("tool_end", "read"),
        ("tool_start", "read"),
        ("tool_end", "read"),
    ]


def test_zeta_answer_route_json_output_disables_live_streaming(
    monkeypatch,
    capsys,
) -> None:
    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config
        assert kwargs.get("stream_sink") is None
        return zeta_agent.AgentTurnResult(final_text="buffered answer")

    monkeypatch.setattr(turn_routes, "ensure_server", lambda: True)
    monkeypatch.setattr(answers_runner, "run_agent_turn", fake_run_agent_turn)

    code = answers_runner.run_tool_answer(
        "question system",
        "Question?",
        json_output=True,
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["answer"] == "buffered answer"


def test_zeta_question_loop_prints_tool_start_while_agent_runs(
    monkeypatch,
    capsys,
) -> None:
    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config
        event_sink = cast("Callable[[dict[str, Any]], None]", kwargs.get("event_sink"))
        assert callable(event_sink)
        tool_call = {
            "type": "tool_call",
            "id": "call-1",
            "tool_call_id": "call-1",
            "name": "read",
            "input": {"path": "README.md"},
        }
        event_sink(tool_call)
        assert "❯ read   README.md" in capsys.readouterr().out
        tool_result = {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "name": "read",
            "result": {
                "ok": True,
                "content": [{"type": "text", "text": "README"}],
            },
        }
        event_sink(tool_result)
        return zeta_agent.AgentTurnResult(
            final_text="It is a README.",
            events=[tool_call, tool_result],
        )

    monkeypatch.setattr(turn_routes, "ensure_server", lambda: True)
    monkeypatch.setattr(answers_runner, "run_agent_turn", fake_run_agent_turn)

    code = answers_runner.run_tool_answer(
        "question system",
        "What does README.md contain?",
    )

    assert code == 0
    assert "\nIt is a README.\n" in capsys.readouterr().out


def test_zeta_question_loop_passes_follow_up_history_as_turns(
    monkeypatch,
) -> None:
    transcripts: list[list[dict[str, Any]]] = []
    captured: dict[str, object] = {}

    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, config
        transcripts.append(transcript)
        captured["context"] = kwargs.get("context")
        return zeta_agent.AgentTurnResult(
            final_text="follow-up answer",
            events=[{"type": "assistant_message", "content": "follow-up answer"}],
        )

    monkeypatch.setattr(turn_routes, "ensure_server", lambda: True)
    monkeypatch.setattr(answers_runner, "run_agent_turn", fake_run_agent_turn)
    monkeypatch.setattr(answers_runner.runtime, "load_project_context", lambda: "ctx")

    code = answers_runner.run_tool_answer(
        "question system",
        "and why?",
        history=[
            {"role": "user", "content": "summarize README"},
            {"role": "assistant", "content": "It is a Sigil README."},
        ],
    )

    assert code == 0
    assert transcripts[0][:2] == [
        {"role": "user", "content": "summarize README"},
        {"role": "assistant", "content": "It is a Sigil README."},
    ]
    assert not any(turn.get("content") == "and why?" for turn in transcripts[0][:2])
    assert transcripts[0][-1] == {
        "type": "assistant_message",
        "content": "follow-up answer",
    }
    assert captured["context"] == "ctx"


def test_zeta_question_loop_falls_back_instead_of_budget_message(
    monkeypatch,
    capsys,
) -> None:
    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config, kwargs
        return zeta_agent.AgentTurnResult(
            events=[
                {
                    "type": "tool_call",
                    "id": "call-1",
                    "tool_call_id": "call-1",
                    "name": "read",
                    "input": {"path": "README.md"},
                },
                {
                    "type": "tool_result",
                    "tool_call_id": "call-1",
                    "name": "read",
                    "result": {
                        "ok": True,
                        "content": [{"type": "text", "text": "Sigil docs"}],
                    },
                },
            ]
        )

    monkeypatch.setattr(turn_routes, "ensure_server", lambda: True)
    monkeypatch.setattr(answers_runner, "run_agent_turn", fake_run_agent_turn)

    def fake_chat_text(
        system: str,
        prompt: str,
        *,
        max_tokens: int,
        stream_sink: object | None = None,
        telemetry_sink: object | None = None,
    ) -> str:
        del system, prompt, max_tokens, stream_sink, telemetry_sink
        return "It contains Sigil docs."

    monkeypatch.setattr(answers_runner, "chat_text", fake_chat_text)

    code = answers_runner.run_tool_answer(
        "question system",
        "What does README.md contain?",
        max_steps=1,
    )

    output = capsys.readouterr().out
    assert code == 0
    assert "\n\nIt contains Sigil docs.\n" in output
    assert "It contains Sigil docs." in output
    assert "question tool budget" not in output


def test_zeta_answer_fallback_formats_evidence_instead_of_raw_json(
    monkeypatch,
) -> None:
    captured: dict[str, str] = {}

    def fake_chat_text(
        system: str,
        prompt: str,
        *,
        max_tokens: int,
        stream_sink: object | None = None,
        telemetry_sink: object | None = None,
    ) -> str:
        del system, max_tokens, stream_sink, telemetry_sink
        captured["prompt"] = prompt
        return "Use a clearer decision index."

    monkeypatch.setattr(answers_runner, "chat_text", fake_chat_text)

    answer = answers_runner.fallback_answer(
        "question system",
        "How would you improve it?",
        [
            {"role": "user", "content": "What is this vault about?"},
            {"role": "assistant", "content": "It is a CEO vault."},
            {
                "type": "tool_result",
                "tool_call_id": "call-1",
                "name": "read",
                "result": {
                    "ok": True,
                    "content": [{"type": "text", "text": "Decision log"}],
                    "metadata": {"path": "/vault/DECISIONS.md"},
                },
            },
        ],
    )

    assert answer == "Use a clearer decision index."
    prompt = captured["prompt"]
    assert "Current question:\nHow would you improve it?" in prompt
    assert "Prior conversation:\nuser: What is this vault about?" in prompt
    assert "assistant: It is a CEO vault." in prompt
    assert "Tool result (read /vault/DECISIONS.md):\nDecision log" in prompt
    assert "Current turn transcript JSON" not in prompt


def test_zeta_answer_fallback_uses_active_session_model(
    tmp_path: Path,
    monkeypatch,
    capsys,
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
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SIGIL_SESSION_ID", "fallback-model")
    zeta_models.set_active_model_profile("fast")
    captured: dict[str, Any] = {}

    def fake_run_agent_turn(
        objective: str,
        transcript: list[dict[str, Any]],
        config: zeta_agent.AgentConfig,
        **kwargs: object,
    ) -> zeta_agent.AgentTurnResult:
        del objective, transcript, config, kwargs
        return zeta_agent.AgentTurnResult(events=[])

    def fake_chat_text(
        system: str,
        prompt: str,
        *,
        max_tokens: int,
        selected_model: str | None = None,
        selected_url: str | None = None,
        stream_sink: object | None = None,
        telemetry_sink: object | None = None,
    ) -> str:
        del system, prompt, max_tokens, stream_sink, telemetry_sink
        captured["selected_model"] = selected_model
        captured["selected_url"] = selected_url
        return "Fallback answer."

    monkeypatch.setattr(turn_routes, "ensure_server", lambda **kwargs: True)
    monkeypatch.setattr(answers_runner, "run_agent_turn", fake_run_agent_turn)
    monkeypatch.setattr(answers_runner, "chat_text", fake_chat_text)

    code = answers_runner.run_tool_answer("question system", "Question?", max_steps=1)

    output = capsys.readouterr().out
    assert code == 0
    assert "\nFallback answer.\n" in output
    assert captured["selected_model"] == "fast-model"
    assert captured["selected_url"] == "http://127.0.0.1:8081/v1/chat/completions"


def test_zeta_answer_model_failure_records_turn_abort(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "zeta-test")
    monkeypatch.setattr(turn_routes, "ensure_server", lambda: True)

    def failing_run_agent_turn(*args: object, **kwargs: object) -> None:
        raise RuntimeError("model stream failed: stream ended before [DONE]")

    monkeypatch.setattr(answers_runner, "run_agent_turn", failing_run_agent_turn)

    with pytest.raises(RuntimeError):
        answers_runner.run_tool_answer("system", "question")

    timeline = zeta.current_timeline()
    assert timeline[-1]["type"] == "turn_aborted"
    assert "model stream failed" in timeline[-1]["error"]
    assert timeline[-2]["type"] == "user_message"
    messages = zeta_timeline.chat_messages(timeline)
    assert messages[-1]["role"] == "assistant"
    assert "turn aborted" in messages[-1]["content"]
    history = read_jsonl("last-answer.jsonl")
    assert history[-1]["role"] == "assistant"
    assert history[-1]["aborted"] is True
    assert "model stream failed" in history[-1]["content"]


def test_zeta_step_model_failure_records_turn_abort(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "zeta-test")
    monkeypatch.setattr(turn_routes, "ensure_server", lambda: True)

    def failing_run_agent_turn(*args: object, **kwargs: object) -> None:
        raise RuntimeError("model request failed: connection reset")

    monkeypatch.setattr(zeta_runner, "run_agent_turn", failing_run_agent_turn)

    with pytest.raises(RuntimeError):
        zeta_runner.run_agent_step("do the thing", glyph=",,")

    timeline = zeta.current_timeline()
    assert timeline[-1]["type"] == "turn_aborted"
    assert timeline[-1]["glyph"] == ",,"
    assert "model request failed" in timeline[-1]["error"]
    assert timeline[-2]["type"] == "user_message"


def test_session_clear_removes_zeta_continuity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "zeta-test")
    zeta.record_event({"type": "user_message", "content": "hello"})
    record_turn("ls", 0, "/repo")
    session_root = tmp_path / "sessions" / "zeta-test"
    assert zeta.current_timeline() != []
    assert session_root.exists()

    result = CliRunner().invoke(sigil_cli, ["session", "clear"])

    assert result.exit_code == 0
    assert "zeta-trace.sqlite3" in result.output
    assert not session_root.exists()
    assert zeta.current_timeline() == []
