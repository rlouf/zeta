"""Model-specific tool profile tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from zeta.capabilities.execution import CapabilityExecutionContext, handle_tool_call
from zeta.capabilities.executors import (
    InProcessCapabilityExecutor,
    local_tool_executor,
)
from zeta.capabilities.paths import reset_base_dir, set_base_dir
from zeta.capabilities.profiles import ToolPresentation, ToolProfile
from zeta.capabilities.registry import CapabilityRegistry, RegisteredCapability
from zeta.capabilities.types import Capability, CapabilityId
from zeta.cli.main import cli as zeta_cli
from zeta.context.builder import PromptBuilder, reconstructed_prompt_request
from zeta.effects import DeliverySemantics
from zeta.loop.config import AgentConfig
from zeta.loop.runtime import run_agent_loop
from zeta.models import profiles as model_profiles
from zeta.models.types import ModelInput, ModelOutput, ModelRequest
from zeta.substrate import InMemoryStore
from zeta.tools import edit as edit_tool
from zeta.tools import history as zeta_history
from zeta.tools import register_builtin_tools
from zeta_test_support import event_by_type


def registered_capability(
    capability_id: str,
    *,
    description: str = "Use the test capability.",
    schema: dict[str, Any] | None = None,
    executor: Any = None,
    delivery_semantics: DeliverySemantics | None = None,
) -> RegisteredCapability:
    provider, name = capability_id.split(".", 1)
    return RegisteredCapability(
        Capability(
            CapabilityId(provider, name),
            description,
            schema or {"type": "object"},
            delivery_semantics=delivery_semantics,
        ),
        InProcessCapabilityExecutor(
            executor or (lambda params: {"ok": True, "metadata": params})
        ),
    )


def run_patch_in(base_dir: Path, patch: str) -> dict[str, Any]:
    token = set_base_dir(base_dir)
    try:
        return edit_tool.run_patch({"patch": patch})
    finally:
        reset_base_dir(token)


def test_zeta_native_tool_profile_preserves_capability_presentation() -> None:
    registry = CapabilityRegistry()
    schema = {
        "type": "object",
        "required": ["command"],
        "properties": {"command": {"type": "string"}},
    }
    registry.register(
        registered_capability(
            "zeta.bash",
            description="Execute a shell command.",
            schema=schema,
        )
    )

    tool_schema = registry.model_tool_schema(("zeta.bash",), tool_profile="native")

    assert tool_schema.descriptors == [
        {
            "type": "function",
            "function": {
                "name": "bash",
                "description": "Execute a shell command.",
                "parameters": schema,
            },
        }
    ]
    route = tool_schema.routes["bash"]
    assert route.capability_id == "zeta.bash"
    assert route.input_schema == schema
    assert route.adapt_arguments({"command": "pytest"}) == {"command": "pytest"}


def test_zeta_codex_tool_profile_projects_known_capabilities() -> None:
    registry = CapabilityRegistry()
    register_builtin_tools(registry)

    tool_schema = registry.model_tool_schema(
        ("zeta.bash", "zeta.patch", "zeta.edit"),
        tool_profile="codex",
    )

    assert [
        descriptor["function"]["name"] for descriptor in tool_schema.descriptors
    ] == ["exec_command", "apply_patch", "edit"]
    assert tool_schema.descriptors[0]["function"] == {
        "name": "exec_command",
        "description": "Run a shell command.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["cmd"],
            "properties": {"cmd": {"type": "string"}},
        },
    }
    assert tool_schema.descriptors[1]["function"] == {
        "name": "apply_patch",
        "description": "Apply a patch to files.",
        "parameters": edit_tool.PATCH_SCHEMA,
    }
    assert tool_schema.routes["exec_command"].adapt_arguments(
        {"cmd": "uv run pytest"}
    ) == {"command": "uv run pytest"}
    assert tool_schema.routes["apply_patch"].capability_id == "zeta.patch"
    assert tool_schema.routes["edit"].capability_id == "zeta.edit"


def test_zeta_tool_profile_rejects_ambiguous_model_names() -> None:
    registry = CapabilityRegistry()
    registry.register(registered_capability("test.first"))
    registry.register(registered_capability("test.second"))
    profile = ToolProfile(
        "collision",
        {
            "test.first": ToolPresentation(
                "same",
                "Run the first capability.",
                {"type": "object"},
            ),
            "test.second": ToolPresentation(
                "same",
                "Run the second capability.",
                {"type": "object"},
            ),
        },
    )

    with pytest.raises(ValueError, match="ambiguous capability name 'same'"):
        registry.model_tool_schema(
            ("test.first", "test.second"),
            tool_profile=profile,
        )


def test_zeta_tool_profile_rejects_an_unknown_profile() -> None:
    registry = CapabilityRegistry()
    registry.register(registered_capability("test.read"))

    with pytest.raises(ValueError, match="unknown tool profile: missing"):
        registry.model_tool_schema(("test.read",), tool_profile="missing")


def test_zeta_tool_profile_adapts_arguments_before_execution() -> None:
    received: list[dict[str, Any]] = []

    def execute(params: dict[str, Any]) -> dict[str, Any]:
        received.append(params)
        return {"ok": True}

    registry = CapabilityRegistry()
    registry.register(
        registered_capability(
            "test.bash",
            schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["command"],
                "properties": {"command": {"type": "string"}},
            },
            executor=execute,
            delivery_semantics="unsafe_to_retry",
        )
    )
    profile = ToolProfile(
        "test",
        {
            "test.bash": ToolPresentation(
                "exec_command",
                "Run a shell command.",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["cmd"],
                    "properties": {"cmd": {"type": "string"}},
                },
                lambda params: {"command": params["cmd"]},
            )
        },
    )
    tool_schema = registry.model_tool_schema(
        ("test.bash",),
        tool_profile=profile,
    )
    ctx = CapabilityExecutionContext(
        event_sink=None,
        trace_store=None,
        tool_registry=registry,
        tool_executor=local_tool_executor(registry),
        effect_scope="qi_test",
    )

    result = asyncio.run(
        handle_tool_call(
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": "uv run pytest"}),
                },
            },
            allowed_capabilities=("test.bash",),
            tool_schema=tool_schema,
            index=0,
            ctx=ctx,
        )
    )

    assert received == [{"command": "uv run pytest"}]
    tool_call = event_by_type(result.events, "tool_call")
    assert tool_call["input"] == {"cmd": "uv run pytest"}
    effect = next(
        event for event in result.events if event.event_type == "runtime.effect.planned"
    )
    assert effect.payload["params"] == {"command": "uv run pytest"}


def test_zeta_query_log_uses_the_injected_runtime_reader() -> None:
    class RejectingExecutor:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def call(
            self,
            capability_id: str,
            params: dict[str, Any],
            *,
            base_dir: Path | None,
            effect_key: str | None,
        ) -> dict[str, Any]:
            del params, base_dir, effect_key
            self.calls.append(capability_id)
            raise AssertionError("query_log must not reach the tool executor")

        async def aclose(self) -> None:
            return None

    registry = CapabilityRegistry()
    register_builtin_tools(registry)
    executor = RejectingExecutor()
    history_tools = zeta_history.bind_history_tools(
        lambda params: {
            "ok": True,
            "content": [{"type": "text", "text": "prior run"}],
            "metadata": {"params": params},
        }
    )
    ctx = CapabilityExecutionContext(
        event_sink=None,
        trace_store=None,
        tool_registry=registry,
        tool_executor=executor,
        internal_tool_executor=lambda capability_id, params: history_tools[
            capability_id
        ](params),
    )

    result = asyncio.run(
        handle_tool_call(
            {
                "id": "call-query-log",
                "type": "function",
                "function": {"name": "query_log", "arguments": "{}"},
            },
            allowed_capabilities=("zeta.query_log",),
            tool_schema=registry.model_tool_schema(("zeta.query_log",)),
            index=0,
            ctx=ctx,
        )
    )

    tool_result = event_by_type(result.events, "tool_result")
    assert tool_result["result"] == {
        "ok": True,
        "content": [{"type": "text", "text": "prior run"}],
        "metadata": {"params": {}},
    }
    assert executor.calls == []


def test_zeta_query_log_without_a_runtime_reader_is_unavailable() -> None:
    class RejectingExecutor:
        async def call(
            self,
            capability_id: str,
            params: dict[str, Any],
            *,
            base_dir: Path | None,
            effect_key: str | None,
        ) -> dict[str, Any]:
            del capability_id, params, base_dir, effect_key
            raise AssertionError("query_log must not reach the tool executor")

        async def aclose(self) -> None:
            return None

    registry = CapabilityRegistry()
    register_builtin_tools(registry)
    history_tools = zeta_history.bind_history_tools(None)
    ctx = CapabilityExecutionContext(
        event_sink=None,
        trace_store=None,
        tool_registry=registry,
        tool_executor=RejectingExecutor(),
        internal_tool_executor=lambda capability_id, params: history_tools[
            capability_id
        ](params),
    )

    result = asyncio.run(
        handle_tool_call(
            {
                "id": "call-query-log",
                "type": "function",
                "function": {"name": "query_log", "arguments": "{}"},
            },
            allowed_capabilities=("zeta.query_log",),
            tool_schema=registry.model_tool_schema(("zeta.query_log",)),
            index=0,
            ctx=ctx,
        )
    )

    tool_result = event_by_type(result.events, "tool_result")
    assert tool_result["result"] == {
        "ok": False,
        "error": {
            "code": "query-log-unavailable",
            "message": "query_log is unavailable outside a durable runtime session",
        },
    }


def test_zeta_tool_profile_validates_model_arguments_before_adaptation() -> None:
    received: list[dict[str, Any]] = []
    registry = CapabilityRegistry()
    registry.register(
        registered_capability(
            "test.bash",
            schema={
                "type": "object",
                "required": ["command"],
                "properties": {"command": {"type": "string"}},
            },
            executor=lambda params: received.append(params) or {"ok": True},
        )
    )
    profile = ToolProfile(
        "test",
        {
            "test.bash": ToolPresentation(
                "exec_command",
                "Run a shell command.",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["cmd"],
                    "properties": {"cmd": {"type": "string"}},
                },
                lambda params: {"command": params["cmd"]},
            )
        },
    )
    tool_schema = registry.model_tool_schema(
        ("test.bash",),
        tool_profile=profile,
    )
    ctx = CapabilityExecutionContext(
        event_sink=None,
        trace_store=None,
        tool_registry=registry,
        tool_executor=local_tool_executor(registry),
    )

    result = asyncio.run(
        handle_tool_call(
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "exec_command",
                    "arguments": json.dumps({"command": "pytest"}),
                },
            },
            allowed_capabilities=("test.bash",),
            tool_schema=tool_schema,
            index=0,
            ctx=ctx,
        )
    )

    tool_result = event_by_type(result.events, "tool_result")
    assert received == []
    assert tool_result["result"]["error"]["code"] == "invalid-tool-args"
    assert "model arguments" in tool_result["result"]["error"]["message"]


def test_zeta_tool_profile_validates_adapted_canonical_arguments() -> None:
    received: list[dict[str, Any]] = []
    registry = CapabilityRegistry()
    registry.register(
        registered_capability(
            "test.bash",
            schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["command"],
                "properties": {"command": {"type": "string"}},
            },
            executor=lambda params: received.append(params) or {"ok": True},
        )
    )
    profile = ToolProfile(
        "test",
        {
            "test.bash": ToolPresentation(
                "exec_command",
                "Run a shell command.",
                {
                    "type": "object",
                    "required": ["cmd"],
                    "properties": {"cmd": {"type": "string"}},
                },
                lambda params: {"other": params["cmd"]},
            )
        },
    )
    tool_schema = registry.model_tool_schema(
        ("test.bash",),
        tool_profile=profile,
    )
    ctx = CapabilityExecutionContext(
        event_sink=None,
        trace_store=None,
        tool_registry=registry,
        tool_executor=local_tool_executor(registry),
    )

    result = asyncio.run(
        handle_tool_call(
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": "pytest"}),
                },
            },
            allowed_capabilities=("test.bash",),
            tool_schema=tool_schema,
            index=0,
            ctx=ctx,
        )
    )

    tool_result = event_by_type(result.events, "tool_result")
    assert received == []
    assert tool_result["result"]["error"]["code"] == "invalid-tool-args"
    assert "canonical arguments" in tool_result["result"]["error"]["message"]


def test_zeta_model_profile_defaults_to_native_tool_profile(tmp_path: Path) -> None:
    config = tmp_path / "models.toml"
    config.write_text(
        '[[models]]\nname = "local"\nmodel = "local-model"\n',
        encoding="utf-8",
    )

    selection = model_profiles.resolve_model_profile(
        "local",
        catalog=model_profiles.load_model_profiles(config),
    )

    assert selection is not None
    assert selection.tool_profile == "native"


def test_zeta_model_profile_selects_named_tool_profile(tmp_path: Path) -> None:
    config = tmp_path / "models.toml"
    config.write_text(
        "\n".join(
            [
                "[[models]]",
                'name = "codex-custom"',
                'model = "gpt-test"',
                'api = "codex-responses"',
                'tool_profile = "codex"',
            ]
        ),
        encoding="utf-8",
    )

    selection = model_profiles.resolve_model_profile(
        "codex-custom",
        catalog=model_profiles.load_model_profiles(config),
    )

    assert selection is not None
    assert selection.tool_profile == "codex"


def test_zeta_model_profile_rejects_unknown_tool_profile(tmp_path: Path) -> None:
    config = tmp_path / "models.toml"
    config.write_text(
        "\n".join(
            [
                "[[models]]",
                'name = "unknown-tools"',
                'model = "test-model"',
                'tool_profile = "missing"',
            ]
        ),
        encoding="utf-8",
    )

    catalog = model_profiles.load_model_profiles(config)

    assert catalog.profiles == {}
    assert len(catalog.diagnostics) == 1
    assert "tool_profile must be one of codex, native" in catalog.diagnostics[0].message


def test_zeta_builtin_codex_model_selects_codex_tool_profile() -> None:
    assert model_profiles.default_model_selection().tool_profile == "codex"


def test_zeta_patch_applies_add_update_and_delete_operations(tmp_path: Path) -> None:
    (tmp_path / "update.txt").write_text("alpha\nomega\n", encoding="utf-8")
    (tmp_path / "delete.txt").write_text("remove me\n", encoding="utf-8")
    patch = """*** Begin Patch
*** Update File: update.txt
@@
-alpha
+beta
 omega
*** Add File: added.txt
+new file
*** Delete File: delete.txt
*** End Patch"""

    data = run_patch_in(tmp_path, patch)

    assert data["ok"] is True
    assert (tmp_path / "update.txt").read_text(encoding="utf-8") == "beta\nomega\n"
    assert (tmp_path / "added.txt").read_text(encoding="utf-8") == "new file\n"
    assert not (tmp_path / "delete.txt").exists()
    assert data["metadata"]["files"] == [
        "update.txt",
        "added.txt",
        "delete.txt",
    ]


def test_zeta_patch_does_not_write_when_a_hunk_does_not_match(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.txt"
    target.write_text("current\n", encoding="utf-8")
    patch = """*** Begin Patch
*** Update File: target.txt
@@
-missing
+replacement
*** Add File: added.txt
+must not exist
*** End Patch"""

    data = run_patch_in(tmp_path, patch)

    assert data["ok"] is False
    assert data["error"]["code"] == "patch-context-mismatch"
    assert target.read_text(encoding="utf-8") == "current\n"
    assert not (tmp_path / "added.txt").exists()


def test_zeta_patch_moves_and_updates_a_file(tmp_path: Path) -> None:
    (tmp_path / "before.txt").write_text("old\n", encoding="utf-8")
    patch = """*** Begin Patch
*** Update File: before.txt
*** Move to: after.txt
@@
-old
+new
*** End Patch"""

    data = run_patch_in(tmp_path, patch)

    assert data["ok"] is True
    assert not (tmp_path / "before.txt").exists()
    assert (tmp_path / "after.txt").read_text(encoding="utf-8") == "new\n"
    assert data["metadata"]["changes"][0]["move_to"] == "after.txt"


def test_zeta_patch_rejects_ambiguous_context(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("same\nsame\n", encoding="utf-8")
    patch = """*** Begin Patch
*** Update File: target.txt
@@
-same
+new
*** End Patch"""

    data = run_patch_in(tmp_path, patch)

    assert data["ok"] is False
    assert data["error"]["code"] == "patch-context-ambiguous"
    assert target.read_text(encoding="utf-8") == "same\nsame\n"


def test_zeta_patch_rejects_paths_outside_the_base_directory(tmp_path: Path) -> None:
    patch = """*** Begin Patch
*** Add File: ../outside.txt
+blocked
*** End Patch"""

    data = run_patch_in(tmp_path, patch)

    assert data["ok"] is False
    assert data["error"]["code"] == "invalid-patch-path"
    assert not (tmp_path.parent / "outside.txt").exists()


def test_zeta_prompt_trace_keeps_ordered_profile_descriptors() -> None:
    registry = CapabilityRegistry()
    register_builtin_tools(registry)
    descriptors = registry.model_tool_schema(
        ("zeta.patch", "zeta.bash"),
        tool_profile="codex",
    ).descriptors
    store = InMemoryStore()
    builder = PromptBuilder(store=store)
    plan = builder.plan_prompt(
        "Update the project.",
        [],
        allowed_capabilities=("zeta.patch", "zeta.bash"),
        tools=descriptors,
        selected_model="gpt-test",
    )
    stored = asyncio.run(builder.commit_prompt_plan(plan))
    assert stored.prompt_object_id is not None

    reconstructed = reconstructed_prompt_request(store, stored.prompt_object_id)

    assert reconstructed is not None
    assert reconstructed.tools == descriptors
    assert [tool["function"]["name"] for tool in reconstructed.tools] == [
        "apply_patch",
        "exec_command",
    ]
    assert reconstructed.payload_verified


def test_zeta_agent_run_uses_the_selected_tool_profile() -> None:
    captured: dict[str, Any] = {}

    class Gateway:
        def available(self, request: ModelRequest) -> bool:
            captured["request"] = request
            return True

        async def generate(
            self,
            model_input: ModelInput,
            request: ModelRequest,
            **_kwargs: Any,
        ) -> ModelOutput:
            captured["tools"] = model_input.tools
            return ModelOutput(message={"role": "assistant", "content": "done"})

    registry = CapabilityRegistry()
    register_builtin_tools(registry)

    asyncio.run(
        run_agent_loop(
            "Run the tests.",
            [],
            AgentConfig(
                allowed_capabilities=("zeta.bash", "zeta.patch"),
                model_name="gpt-test",
                model_url="https://example.invalid",
                tool_profile="codex",
                max_turns=1,
            ),
            tool_registry=registry,
            tool_executor=local_tool_executor(registry),
            model_gateway=Gateway(),
        )
    )

    assert [tool["function"]["name"] for tool in captured["tools"]] == [
        "exec_command",
        "apply_patch",
    ]


def test_zeta_trace_replay_uses_stored_profile_descriptors(monkeypatch) -> None:
    registry = CapabilityRegistry()
    register_builtin_tools(registry)
    descriptors = registry.model_tool_schema(
        ("zeta.patch", "zeta.bash"),
        tool_profile="codex",
    ).descriptors
    store = InMemoryStore()
    builder = PromptBuilder(store=store)
    plan = builder.plan_prompt(
        "Update the project.",
        [],
        allowed_capabilities=("zeta.patch", "zeta.bash"),
        tools=descriptors,
        selected_model="gpt-test",
    )
    stored = asyncio.run(builder.commit_prompt_plan(plan))
    assert stored.prompt_object_id is not None
    captured: dict[str, Any] = {}

    async def fake_chat(
        messages: list[dict[str, Any]],
        request: ModelRequest,
        **kwargs: Any,
    ) -> dict[str, Any]:
        captured["tools"] = kwargs["tools"]
        return {"role": "assistant", "content": "done"}

    monkeypatch.setattr("zeta.cli.traces.scoped_store", lambda *_args, **_kwargs: store)
    monkeypatch.setattr("zeta.cli.traces.chat_completion_messages", fake_chat)
    monkeypatch.setattr(
        "zeta.cli.traces.replay_model_selection",
        lambda *_args, **_kwargs: model_profiles.ModelSelection(
            profile="local",
            model="local-model",
            url="https://example.invalid",
            tool_profile="native",
        ),
    )

    result = CliRunner().invoke(
        zeta_cli,
        ["traces", "replay", stored.prompt_object_id],
    )

    assert result.exit_code == 0, result.output
    assert captured["tools"] == descriptors
