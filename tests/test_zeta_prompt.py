"""Prompt components, budget, compaction, context, and skills tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from _zeta_helpers import (
    BatchSpyStore,
    assert_task_state_graph,
    big_transcript_components,
    linked_ids_by_kind,
    linked_kinds,
    task_state_fixture,
    tool_call_fixture,
    tool_result_event,
    tool_result_transcript,
    write_skill,
)

from sigil.tools import ensure_builtin_tools_registered
from sigil.zeta import context as zeta_context
from sigil.zeta import prompt as zeta_prompt
from sigil.zeta import skills as zeta_skills
from sigil.zeta import trace as zeta_trace
from sigil.zeta.models import chat_completions as zeta_model
from sigil.zeta.prompt.system import model_tool_descriptors

ensure_builtin_tools_registered()


def test_zeta_prompt_builder_noop_transform_matches_chat_messages() -> None:
    store = zeta_trace.InMemoryStore()
    tools = model_tool_descriptors(())
    transcript = [{"role": "user", "content": "prior"}]
    current_events = [{"type": "model", "content": "current"}]

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
            {"type": "model", "tool_calls": tool_call_fixture("call-1")},
            {"type": "tool_result", "tool_call_id": "call-1", "result": {"ok": True}},
        ],
        tools=model_tool_descriptors(("read",)),
    )

    assert prepared.prompt_object_id is not None
    prompt = store.get_object(prepared.prompt_object_id)
    assert prompt is not None
    kinds = linked_kinds(store, prompt)
    assert "system_prompt" in kinds
    assert "user_message" in kinds
    assert "assistant_message" in kinds
    assert "project_context" in kinds
    assert "tool_descriptor_set" in kinds
    assert "tool_result" in kinds


def test_zeta_prompt_request_reconstructs_and_verifies() -> None:
    store = zeta_trace.InMemoryStore()
    tools = model_tool_descriptors(("read",))
    prepared = zeta_prompt.PromptBuilder(store=store).build(
        "inspect",
        [{"role": "user", "content": "prior"}],
        allowed_tools=("read",),
        context="Project context",
        tools=tools,
        selected_model="unit-model",
    )
    assert prepared.prompt_object_id is not None

    reconstructed = zeta_prompt.reconstructed_prompt_request(
        store, prepared.prompt_object_id
    )

    assert reconstructed is not None
    assert reconstructed.messages == prepared.messages
    assert reconstructed.tools == prepared.tools
    assert reconstructed.selected_model == "unit-model"
    assert reconstructed.payload_verified


def test_zeta_prompt_request_reconstructs_a_no_thinking_prompt() -> None:
    store = zeta_trace.InMemoryStore()
    tools = model_tool_descriptors(("read",))
    prepared = zeta_prompt.PromptBuilder(store=store).build(
        "inspect",
        [],
        allowed_tools=("read",),
        tools=tools,
        selected_model="unit-model",
        thinking="none",
    )
    assert prepared.prompt_object_id is not None
    assert prepared.payload["chat_template_kwargs"] == {"enable_thinking": False}

    reconstructed = zeta_prompt.reconstructed_prompt_request(
        store, prepared.prompt_object_id
    )

    assert reconstructed is not None
    assert reconstructed.thinking == "none"
    assert reconstructed.payload_verified


def test_zeta_prompt_reconstruction_treats_legacy_prompts_as_no_thinking() -> None:
    store = zeta_trace.InMemoryStore()
    message = {"role": "user", "content": "objective"}
    component_id = store.put_object(
        zeta_trace.Object(
            kind="user_message",
            schema="zeta.prompt_component.v1",
            data={"message": message},
        )
    )
    legacy_payload = zeta_model.chat_completion_request_body(
        [message],
        max_tokens=zeta_model.DEFAULT_MAX_COMPLETION_TOKENS,
        thinking="none",
    )
    prompt_id = store.put_object(
        zeta_trace.Object(
            kind="prompt",
            schema="zeta.prompt.v1",
            data={"payload_sha256": zeta_prompt.payload_sha256(legacy_payload)},
            links=(component_id,),
        )
    )
    store.record_derivation(
        zeta_trace.Derivation(
            producer="PromptBuilder",
            output_id=prompt_id,
            input_ids=(component_id,),
            params={
                "max_tokens": zeta_model.DEFAULT_MAX_COMPLETION_TOKENS,
                "selected_model": None,
            },
        )
    )

    reconstructed = zeta_prompt.reconstructed_prompt_request(store, prompt_id)

    assert reconstructed is not None
    assert reconstructed.thinking == "none"
    assert reconstructed.payload_verified


def test_zeta_prompt_request_reconstruction_flags_a_changed_component() -> None:
    store = zeta_trace.InMemoryStore()
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
            data={"payload_sha256": "sha256:not-the-payload"},
            links=(component_id,),
        )
    )

    reconstructed = zeta_prompt.reconstructed_prompt_request(store, prompt_id)

    assert reconstructed is not None
    assert reconstructed.messages == [{"role": "user", "content": "objective"}]
    assert not reconstructed.payload_verified


def test_zeta_prompt_request_reconstruction_requires_a_prompt() -> None:
    store = zeta_trace.InMemoryStore()
    other_id = store.put_object(
        zeta_trace.Object(
            kind="assistant_message",
            schema="zeta.assistant_output.v1",
            data={"message": {"role": "assistant", "content": "hi"}},
        )
    )

    assert zeta_prompt.reconstructed_prompt_request(store, other_id) is None
    assert zeta_prompt.reconstructed_prompt_request(store, "sha256:missing") is None


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
    assert transform.max_tokens == 7
    assert isinstance(transform.transform, zeta_prompt.StructuralTrimPromptTransform)
    assert isinstance(
        zeta_prompt.prompt_transform_from_env({}), zeta_prompt.NoOpPromptTransform
    )


def test_zeta_render_stub_contract() -> None:
    component = zeta_prompt.PromptComponent(
        kind="tool_result",
        message={"role": "tool", "content": "abcd"},
        object_id="sha256:abc",
    )

    assert (
        zeta_prompt.render_stub(component)
        == "[elided tool_result 1~tok id=sha256:abc — re-run the original tool call to recover this content]"
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
        current_events=[{"type": "model", "content": "current"}],
        tools=model_tool_descriptors(("read",)),
    )

    assert [component.kind for component in components[:4]] == [
        "system_prompt",
        "tool_descriptor_set",
        "project_context",
        "user_message",
    ]
    assert components[3].data.get("historical") is True


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
                if component.data.get("historical")
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
                if not component.data.get("historical"):
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
            {"role": "assistant", "content": "Working on it"},
            {"role": "user", "content": "Status?"},
            {"role": "assistant", "content": "Tests pass"},
            {"role": "user", "content": "Keep going"},
        ],
        allowed_tools=(),
        current_events=[{"type": "model", "content": "Fresh evidence"}],
        tools=[],
    )

    assert len(extractor.components) == 3
    assert all(component.data.get("historical") for component in extractor.components)
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
        [
            {"role": "user", "content": "keep raw transcript"},
            {"role": "assistant", "content": "noted"},
            {"role": "user", "content": "two"},
            {"role": "assistant", "content": "three"},
            {"role": "user", "content": "four"},
            {"role": "assistant", "content": "five"},
        ],
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
        {"type": "model", "tool_calls": tool_call_fixture()},
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
    assert tool_component.kind == "tool_result"
    assert tool_component.data.get("historical") is True
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
    assert stub.startswith("[elided tool_result ")
    assert " re-run the original tool call to recover this content]" in stub
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
    assert "tool_result" in linked_kinds(store, prompt)
    assert "compacted_context" not in linked_kinds(store, prompt)


def test_zeta_structural_trim_default_is_late_safety_valve() -> None:
    transform = zeta_prompt.StructuralTrimPromptTransform()
    below = zeta_prompt.PromptComponent(
        kind="tool_result",
        data={
            "historical": True,
            "source_event": {
                "type": "tool_result",
                "tool_call_id": "call-below",
                "tool_name": "read",
            },
        },
        message={
            "role": "tool",
            "tool_call_id": "call-below",
            "content": "x" * 119_999,
        },
        object_id="sha256:below",
    )
    above = zeta_prompt.PromptComponent(
        kind="tool_result",
        data={
            "historical": True,
            "source_event": {
                "type": "tool_result",
                "tool_call_id": "call-above",
                "tool_name": "read",
            },
        },
        message={
            "role": "tool",
            "tool_call_id": "call-above",
            "content": "x" * 120_001,
        },
        object_id="sha256:above",
    )

    trimmed = transform.apply([below, above])

    assert trimmed[0].kind == "tool_result"
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
                "type": "model",
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
        kind="tool_result",
        data={
            "historical": True,
            "source_event": {
                "type": "tool_result",
                "tool_call_id": "call-structured",
                "tool_name": "read",
                "result": {
                    "ok": True,
                    "content": [{"type": "text", "text": raw_text}],
                    "metadata": {"path": "structured.txt"},
                },
            },
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
        "[elided tool_result 145~tok id=sha256:source "
        "— re-run the original tool call to recover this content]"
    )


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

    context = zeta_context.load_project_context()

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

    context = zeta_context.load_project_context()

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

    context = zeta_context.load_project_context()

    assert "project instructions" in context


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


def test_system_prompt_advertises_enabled_skills_only_with_read(
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

    prompt = zeta_prompt.system_prompt(allowed_tools=("read", "ls"))
    no_read_prompt = zeta_prompt.system_prompt(allowed_tools=("ls",))

    assert "<available_skills>" in prompt
    assert "name: enabled-skill" in prompt
    assert "description: Do enabled work." in prompt
    assert f"location: {enabled}" in prompt
    assert "hidden-skill" not in prompt
    assert "<available_skills>" not in no_read_prompt


def test_system_prompt_is_product_neutral_and_dynamic() -> None:
    prompt = zeta_prompt.system_prompt(allowed_tools=("read", "ls"))
    grep_prompt = zeta_prompt.system_prompt(allowed_tools=("read", "grep", "ls"))

    assert "Sigil" not in prompt
    assert "You are Zeta" not in prompt
    assert "Preserve user changes." not in prompt
    assert "shell" not in prompt.lower()
    assert "handoff" not in prompt.lower()
    assert "Tool protocol:" in prompt
    assert "staged effect" in prompt
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


def test_system_prompt_states_todays_date() -> None:
    import time

    prompt = zeta_prompt.system_prompt(allowed_tools=("read",))
    custom = zeta_prompt.system_prompt("Custom base.", allowed_tools=("read",))

    today = time.strftime("%Y-%m-%d", time.localtime())
    assert f"Today is {today}" in prompt
    assert f"Today is {today}" in custom
    assert prompt == zeta_prompt.system_prompt(allowed_tools=("read",))


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

    assert message.startswith("@missing: inspect\n\ncwd:")


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

    assert message.startswith("@skill reviewer inspect\n\ncwd:")
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

    assert message.startswith("@reviewer inspect\n\ncwd:")
    assert '<skill name="reviewer"' not in message


def test_zeta_measure_counts_project_context_once() -> None:
    context = "x" * 4000
    components = zeta_prompt.prompt_components(
        "inspect",
        [],
        allowed_tools=("read",),
        context=context,
    )

    project = next(c for c in components if c.kind == "project_context")
    assert "content" not in project.data
    assert project.data["chars"] == 4000
    assert str(project.data["sha256"]).startswith("sha256:")
    usage = zeta_prompt.measure(components)
    project_usage = next(c for c in usage.components if c.kind == "project_context")
    assert project_usage.tokens < 50


def test_zeta_structural_trim_works_without_trace_ids() -> None:
    raw_text = "bulky read output " * 20
    component = zeta_prompt.PromptComponent(
        kind="tool_result",
        data={
            "historical": True,
            "source_event": {
                "type": "tool_result",
                "tool_call_id": "call-untraced",
                "tool_name": "read",
                "result": {
                    "ok": True,
                    "content": [{"type": "text", "text": raw_text}],
                    "metadata": {"path": "big.txt"},
                },
            },
        },
        message={
            "role": "tool",
            "tool_call_id": "call-untraced",
            "content": raw_text,
        },
    )

    trimmed = zeta_prompt.StructuralTrimPromptTransform(max_content_chars=20).apply(
        [component]
    )[0]

    assert trimmed.kind == "compacted_context"
    assert trimmed.links == ()
    assert trimmed.source_object_id is None
    assert "source_object_id" not in trimmed.data
    assert trimmed.message is not None
    assert "id=unknown" in str(trimmed.message["content"])


def test_zeta_structural_trim_embeds_trim_payload_in_component_data() -> None:
    raw_text = "line one\nline two\nbulky read output " * 10
    component = zeta_prompt.PromptComponent(
        kind="tool_result",
        data={
            "historical": True,
            "source_event": {
                "type": "tool_result",
                "tool_call_id": "call-read",
                "tool_name": "read",
                "result": {
                    "ok": True,
                    "content": [{"type": "text", "text": raw_text}],
                    "metadata": {"path": "big.txt"},
                },
            },
        },
        message={"role": "tool", "tool_call_id": "call-read", "content": raw_text},
        object_id="sha256:source",
    )

    trimmed = zeta_prompt.StructuralTrimPromptTransform(max_content_chars=20).apply(
        [component]
    )[0]

    trim = trimmed.data["trim"]
    assert trim["trimmed"] is True
    assert trim["trim_method"] == "structural"
    assert trim["source_object_id"] == "sha256:source"
    assert trim["tool_call_id"] == "call-read"
    assert trim["raw_content_chars"] == len(raw_text)
    assert str(trim["raw_content_sha256"]).startswith("sha256:")
    assert trim["tool_result"]["ok"] is True
    assert trim["tool_result"]["metadata"] == {"path": "big.txt"}
    assert trim["tool_result"]["content"][0]["text_chars"] == len(raw_text)


def test_zeta_task_state_transform_compacts_components_without_trace_ids() -> None:
    class FakeExtractor:
        def extract(
            self,
            components: list[zeta_prompt.PromptComponent],
        ) -> dict[str, Any]:
            del components
            return task_state_fixture(objective="continue without a store")

    components = zeta_prompt.prompt_components(
        "continue",
        [
            {"role": "user", "content": "Old objective"},
            {"role": "assistant", "content": "Old decision"},
            {"role": "user", "content": "newer one"},
            {"role": "assistant", "content": "newer two"},
            {"role": "user", "content": "newer three"},
            {"role": "assistant", "content": "newer four"},
        ],
        allowed_tools=(),
    )
    assert all(component.object_id is None for component in components)

    transform = zeta_prompt.TaskStateExtractionPromptTransform(
        extractor=FakeExtractor()
    )
    compacted = transform.apply(components)

    contents = [
        str(component.message.get("content") or "")
        for component in compacted
        if component.message is not None
    ]
    joined = "\n".join(contents)
    assert "Task state JSON:" in joined
    assert "continue without a store" in joined
    assert "Old decision" not in joined


def test_zeta_task_state_transform_keeps_newest_messages_verbatim() -> None:
    class FakeExtractor:
        def __init__(self) -> None:
            self.components: list[zeta_prompt.PromptComponent] = []

        def extract(
            self,
            components: list[zeta_prompt.PromptComponent],
        ) -> dict[str, Any]:
            self.components = components
            return task_state_fixture(objective="compact the old half")

    timeline = [
        {"role": "user", "content": "old message one"},
        {"role": "assistant", "content": "old message two"},
        {"role": "user", "content": "recent message three"},
        {"role": "assistant", "content": "recent message four"},
        {"role": "user", "content": "recent message five"},
        {"role": "assistant", "content": "recent message six"},
    ]
    components = zeta_prompt.prompt_components("continue", timeline, allowed_tools=())
    extractor = FakeExtractor()

    compacted = zeta_prompt.TaskStateExtractionPromptTransform(
        extractor=extractor
    ).apply(components)

    extracted_contents = [
        str(component.message.get("content") or "")
        for component in extractor.components
        if component.message is not None
    ]
    assert extracted_contents == ["old message one", "old message two"]
    joined = "\n".join(
        str(component.message.get("content") or "")
        for component in compacted
        if component.message is not None
    )
    assert "Task state JSON:" in joined
    assert "old message one" not in joined
    assert "recent message three" in joined
    assert "recent message six" in joined


def test_zeta_task_state_extraction_input_omits_duplicate_source_event() -> None:
    component = zeta_prompt.PromptComponent(
        kind="tool_result",
        data={
            "historical": True,
            "source_event_type": "tool_result",
            "source_event_role": "",
            "source_event": {
                "type": "tool_result",
                "tool_call_id": "call-1",
                "result": {"ok": True, "content": [{"type": "text", "text": "big"}]},
            },
        },
        message={"role": "tool", "tool_call_id": "call-1", "content": "big"},
    )

    messages = zeta_prompt.task_state_extraction_messages([component])

    payload = json.loads(
        str(messages[1]["content"]).removeprefix("Prior timeline components JSON:\n")
    )
    assert "source_event" not in payload[0]
    assert payload[0]["source_event_type"] == "tool_result"
    assert payload[0]["message"]["content"] == "big"


def test_zeta_task_state_extraction_is_cached_per_source_set() -> None:
    class CountingExtractor:
        def __init__(self) -> None:
            self.calls = 0

        def extract(
            self,
            components: list[zeta_prompt.PromptComponent],
        ) -> dict[str, Any]:
            del components
            self.calls += 1
            return task_state_fixture(objective="cached objective")

    timeline = [{"role": "user", "content": f"message {index}"} for index in range(6)]
    components = zeta_prompt.prompt_components("continue", timeline, allowed_tools=())
    extractor = CountingExtractor()
    transform = zeta_prompt.TaskStateExtractionPromptTransform(extractor=extractor)

    first = transform.apply(components)
    second = transform.apply(components)

    assert extractor.calls == 1
    assert [c.kind for c in first] == [c.kind for c in second]
    assert any(c.kind == "task_state" for c in second)


def test_zeta_budget_threshold_escalates_until_under_budget() -> None:
    class DropHalf:
        def __init__(self, label: str, calls: list[str]) -> None:
            self.label = label
            self.calls = calls

        def apply(
            self,
            components: list[zeta_prompt.PromptComponent],
        ) -> list[zeta_prompt.PromptComponent]:
            self.calls.append(self.label)
            transcript = [c for c in components if c.data.get("historical")]
            keep = {id(c) for c in transcript[len(transcript) // 2 :]}
            return [
                c for c in components if not c.data.get("historical") or id(c) in keep
            ]

    components = big_transcript_components(8)
    over_budget = zeta_prompt.measure(components).total_tokens
    target = over_budget - 600
    calls: list[str] = []
    gate = zeta_prompt.BudgetThresholdPromptTransform(
        DropHalf("first", calls),
        target,
        escalation=(DropHalf("second", calls), DropHalf("third", calls)),
    )

    output = gate.apply(components)

    assert calls == ["first", "second"]
    assert zeta_prompt.measure(output).total_tokens <= target


def test_zeta_budget_threshold_warns_when_still_over_budget(caplog) -> None:
    zeta_prompt.transforms.reset_over_budget_warning()
    components = big_transcript_components(4)
    gate = zeta_prompt.BudgetThresholdPromptTransform(
        zeta_prompt.NoOpPromptTransform(),
        1,
    )

    with caplog.at_level("WARNING", logger="sigil.zeta.prompt"):
        output = gate.apply(components)

    assert output
    assert any("over budget" in record.getMessage() for record in caplog.records)


def test_zeta_drop_oldest_removes_historical_messages_until_budget() -> None:
    components = big_transcript_components(6)
    total = zeta_prompt.measure(components).total_tokens
    target = total - 150

    output = zeta_prompt.DropOldestPromptTransform(max_tokens=target).apply(components)

    assert zeta_prompt.measure(output).total_tokens <= target
    contents = [
        str(c.message.get("content") or "") for c in output if c.message is not None
    ]
    joined = "\n".join(contents)
    assert "message 0" not in joined
    assert "message 5" in joined
    assert any(c.kind == "system_prompt" for c in output)
    assert any(c.kind == "user_message" for c in output)


def test_zeta_drop_oldest_drops_tool_results_with_their_call() -> None:
    timeline = [
        {
            "type": "model",
            "tool_calls": tool_call_fixture("call-old"),
        },
        tool_result_event("call-old", "old result " + "x" * 400, metadata={}),
        {"role": "user", "content": "newer message"},
    ]
    components = zeta_prompt.prompt_components("continue", timeline, allowed_tools=())
    total = zeta_prompt.measure(components).total_tokens

    output = zeta_prompt.DropOldestPromptTransform(max_tokens=total - 50).apply(
        components
    )

    roles = [c.message.get("role") for c in output if c.message is not None]
    assert "tool" not in roles
    assert not any(
        c.message is not None and c.message.get("tool_calls") for c in output
    )
    joined = "\n".join(
        str(c.message.get("content") or "") for c in output if c.message is not None
    )
    assert "newer message" in joined


def test_zeta_trim_env_modes_build_escalation_ladders() -> None:
    structural = zeta_prompt.prompt_transform_from_env(
        {"ZETA_TRIM": "structural", "ZETA_TRIM_THRESHOLD_TOKENS": "7"}
    )
    assert isinstance(structural, zeta_prompt.BudgetThresholdPromptTransform)
    assert [type(t).__name__ for t in structural.escalation] == [
        "TaskStateExtractionPromptTransform",
        "DropOldestPromptTransform",
    ]

    task_state = zeta_prompt.prompt_transform_from_env(
        {"ZETA_TRIM": "task_state", "ZETA_TRIM_THRESHOLD_TOKENS": "7"}
    )
    assert isinstance(task_state, zeta_prompt.BudgetThresholdPromptTransform)
    assert [type(t).__name__ for t in task_state.escalation] == [
        "DropOldestPromptTransform",
    ]


def test_zeta_trim_unknown_mode_warns_loudly(caplog) -> None:
    with caplog.at_level("WARNING", logger="sigil.zeta.prompt"):
        transform = zeta_prompt.prompt_transform_from_env({"ZETA_TRIM": "structurall"})

    assert isinstance(transform, zeta_prompt.NoOpPromptTransform)
    assert any("ZETA_TRIM" in record.getMessage() for record in caplog.records)


def test_zeta_prompt_components_start_prior_timeline_at_message_boundary() -> None:
    timeline = [
        {
            "type": "tool_result",
            "tool_call_id": "call-cut-off",
            "name": "read",
            "result": {"ok": True, "content": [{"type": "text", "text": "orphan"}]},
        },
        {"type": "model", "content": "the answer"},
    ]

    components = zeta_prompt.prompt_components("continue", timeline, allowed_tools=())

    contents = [
        str(component.message.get("content") or "")
        for component in components
        if component.message is not None
    ]
    assert not any(content.startswith("Tool result JSON:") for content in contents)
    assert any("the answer" in content for content in contents)


def test_zeta_project_context_caps_oversized_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "repo"
    project.mkdir()
    (project / "AGENTS.md").write_text(
        "start marker\n" + "x" * 60_000, encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(project)

    context = zeta_context.load_project_context()

    assert "start marker" in context
    assert "... truncated ..." in context
    assert len(context) <= zeta_context.MAX_CONTEXT_FILE_CHARS + 200


def test_zeta_project_context_total_cap_drops_broadest_first(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    zeta_home = home / ".zeta"
    zeta_home.mkdir(parents=True)
    (zeta_home / "AGENTS.md").write_text(
        "global rules\n" + "g" * 20_000, encoding="utf-8"
    )
    parent = tmp_path / "repo"
    project = parent / "pkg"
    project.mkdir(parents=True)
    (parent / "AGENTS.md").write_text("parent rules\n" + "p" * 20_000, encoding="utf-8")
    (project / "AGENTS.md").write_text("local rules\n" + "l" * 20_000, encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(project)

    context = zeta_context.load_project_context()

    assert len(context) <= zeta_context.MAX_CONTEXT_TOTAL_CHARS + 200
    assert "global rules" not in context
    assert "parent rules" in context
    assert "local rules" in context
    assert context.index("parent rules") < context.index("local rules")


def test_zeta_prompt_build_writes_in_a_single_batch() -> None:
    store = BatchSpyStore()

    zeta_prompt.PromptBuilder(store=store).build(
        "question",
        [{"role": "user", "content": "prior"}],
        allowed_tools=(),
        tools=[],
    )

    assert store.batches == 1


def test_zeta_prompt_object_stores_payload_hash_not_payload() -> None:
    store = zeta_trace.InMemoryStore()

    prepared = zeta_prompt.PromptBuilder(store=store).build(
        "question",
        [{"role": "user", "content": "prior"}],
        allowed_tools=(),
        tools=[],
    )

    assert prepared.prompt_object_id is not None
    prompt = store.get_object(prepared.prompt_object_id)
    assert prompt is not None
    assert "payload" not in prompt.data
    assert prompt.data["payload_sha256"] == zeta_prompt.builder.payload_sha256(
        prepared.payload
    )
    linked = [store.get_object(component_id) for component_id in prompt.links]
    linked_messages = [
        obj.data["message"]
        for obj in linked
        if obj is not None and "message" in obj.data
    ]
    assert linked_messages == prepared.messages


def test_zeta_prompt_builder_discovers_skills_once_per_turn(monkeypatch) -> None:
    calls = 0

    def fake_available_skills() -> list[zeta_skills.Skill]:
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(
        "sigil.zeta.prompt.components.available_skills", fake_available_skills
    )
    monkeypatch.setattr(
        "sigil.zeta.prompt.builder.available_skills", fake_available_skills
    )
    builder = zeta_prompt.PromptBuilder(store=zeta_trace.InMemoryStore())

    builder.build("question", [], allowed_tools=("read",), tools=[])
    builder.build("question", [], allowed_tools=("read",), tools=[])

    assert calls == 1
