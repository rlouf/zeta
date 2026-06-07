from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

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
from sigil.routes import ask as answers_runner
from sigil.routes import zeta_step as zeta_runner
from sigil import handoff as sigil_handoff
from sigil.session import recent_turns, record_turn
from sigil import display as sigil_display
from sigil.zeta import agent as zeta_agent
from sigil.zeta import runtime as zeta
from sigil.zeta import model as zeta_model
from sigil.zeta import models as zeta_models
from sigil.zeta.tools import grep as grep_tool
from sigil.zeta.tools import validate_tool_args
from sigil.zeta.cli import cli as zeta_cli


def write_models_config(home: Path, text: str) -> Path:
    config_dir = home / ".zeta"
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "models.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_zeta_model_config_uses_zeta_env(monkeypatch) -> None:
    monkeypatch.delenv("ZETA_MODEL_URL", raising=False)
    monkeypatch.delenv("ZETA_MODEL_NAME", raising=False)

    assert zeta_model.model_url() == zeta_model.DEFAULT_MODEL_URL
    assert zeta_model.model_name() == zeta_model.DEFAULT_MODEL_NAME

    monkeypatch.setenv("ZETA_MODEL_URL", "http://zeta.invalid/v1/chat/completions")
    monkeypatch.setenv("ZETA_MODEL_NAME", "zeta-model")

    assert zeta_model.model_url() == "http://zeta.invalid/v1/chat/completions"
    assert zeta_model.model_name() == "zeta-model"


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
    assert "response_format" not in body


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
    assert result.events == [{"type": "assistant_message", "content": "done"}]
    kwargs = cast(dict[str, Any], captured["kwargs"])
    assert kwargs["tools"][0]["function"]["name"] == "read"


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
    assert tool_result["result"]["metadata"]["stdout"] == "direct-bash"


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

    catalog = zeta.discover_skills()

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

    catalog = zeta.discover_skills()

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

    catalog = zeta.discover_skills()

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
    result = CliRunner().invoke(zeta_cli, ["tools", "list", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    names = {tool["name"] for tool in data["tools"]}
    assert {"read", "grep", "ls", "bash", "edit", "write"} <= names
    assert data["tools"][0]["origin"] == "builtin"


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


def test_zeta_cli_plugin_tool_flows_through_registry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    script = tmp_path / "plugin.py"
    write_cli_plugin(script)
    write_tools_config(home, [sys.executable, str(script)])
    monkeypatch.setenv("HOME", str(home))

    listed = CliRunner().invoke(zeta_cli, ["tools", "list", "--json"])
    assert listed.exit_code == 0
    tools = json.loads(listed.output)["tools"]
    plugin = next(tool for tool in tools if tool["name"] == "docs_search")
    assert plugin["origin"] == "plugin"
    assert plugin["plugin"] == sys.executable
    assert plugin["command"] == ["zeta", "tool", "docs_search"]

    descriptors = zeta.model_tool_descriptors(("docs_search",))
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

    analysis = zeta.analyze_tool("docs_search", {"query": "install"})
    assert analysis["valid"] is True
    assert analysis["effects"][0]["target"] == "install"

    data = zeta.run_tool("docs_search", {"query": "install"})
    assert data["ok"] is True
    assert data["content"][0]["text"] == "docs:install"

    cli_run = CliRunner().invoke(
        zeta_cli,
        ["tool", "docs_search"],
        input=json.dumps({"query": "cli"}),
    )
    assert cli_run.exit_code == 0
    assert json.loads(cli_run.output)["content"][0]["text"] == "docs:cli"


def test_zeta_cli_plugin_name_collision_is_ignored(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    script = tmp_path / "plugin.py"
    write_cli_plugin(script, name="read")
    write_tools_config(home, [sys.executable, str(script)])
    monkeypatch.setenv("HOME", str(home))

    data = zeta.tools_list()
    read_tools = [tool for tool in data["tools"] if tool["name"] == "read"]
    assert len(read_tools) == 1
    assert read_tools[0]["origin"] == "builtin"
    assert data["diagnostics"][0]["code"] == "plugin-name-collision"


def test_zeta_cli_plugin_invalid_metadata_reports_diagnostic(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    script = tmp_path / "plugin.py"
    write_cli_plugin(script, invalid_metadata=True)
    write_tools_config(home, [sys.executable, str(script)])
    monkeypatch.setenv("HOME", str(home))

    data = zeta.tools_list()
    assert "docs_search" not in {tool["name"] for tool in data["tools"]}
    assert data["diagnostics"][0]["code"] == "plugin-metadata-invalid-json"


def test_zeta_cli_plugin_missing_command_reports_diagnostic(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    write_tools_config(home, [str(tmp_path / "missing-tool")])
    monkeypatch.setenv("HOME", str(home))

    data = zeta.tools_list()
    assert data["diagnostics"][0]["code"] == "plugin-metadata-failed"


def test_zeta_cli_plugin_metadata_timeout_reports_diagnostic(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    script = tmp_path / "plugin.py"
    write_cli_plugin(script, sleep_metadata=True)
    write_tools_config(home, [sys.executable, str(script)], timeout_ms=10)
    monkeypatch.setenv("HOME", str(home))

    data = zeta.tools_list()
    assert data["diagnostics"][0]["code"] == "plugin-metadata-timeout"


def test_zeta_cli_plugin_nonzero_execution_returns_tool_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    script = tmp_path / "plugin.py"
    write_cli_plugin(script, fail_run=True)
    write_tools_config(home, [sys.executable, str(script)])
    monkeypatch.setenv("HOME", str(home))

    data = zeta.run_tool("docs_search", {"query": "install"})
    assert data["ok"] is False
    assert data["error"]["code"] == "plugin-run-failed"
    assert "status 7" in data["error"]["message"]


def test_zeta_help_frames_cli_as_bundled_runtime_service() -> None:
    result = CliRunner().invoke(zeta_cli, ["--help"])

    assert result.exit_code == 0
    assert "Bundled runtime service commands used by Sigil." in result.output


def test_zeta_tool_read_schema_and_run(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("hello zeta\n", encoding="utf-8")

    schema = CliRunner().invoke(zeta_cli, ["tool", "read", "--schema"])
    assert schema.exit_code == 0
    assert json.loads(schema.output)["required"] == ["path"]

    result = CliRunner().invoke(
        zeta_cli,
        ["tool", "read"],
        input=json.dumps({"path": str(target)}),
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["content"][0]["text"] == "hello zeta\n"


def test_zeta_tool_read_offset_and_limit_select_lines(tmp_path: Path) -> None:
    target = tmp_path / "lines.txt"
    target.write_text("one\ntwo\nthree\nfour\nfive\n", encoding="utf-8")

    data = zeta.run_tool("read", {"path": str(target), "offset": 1, "limit": 2})

    assert data["ok"] is True
    assert data["content"][0]["text"] == "two\nthree\n"
    assert data["metadata"]["offset"] == 1
    assert data["metadata"]["limit"] == 2


def test_zeta_tool_read_limit_past_end_returns_remaining_lines(tmp_path: Path) -> None:
    target = tmp_path / "short.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")

    data = zeta.run_tool("read", {"path": str(target), "offset": 1, "limit": 10})

    assert data["content"][0]["text"] == "beta\n"


def test_zeta_tool_grep_reports_total_limited_metadata(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("needle one\nneedle two\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("needle three\n", encoding="utf-8")

    data = zeta.run_tool(
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

    data = zeta.run_tool("grep", {"path": str(target), "pattern": "needle"})

    assert data["ok"] is True
    assert len(data["content"][0]["text"]) == 20
    assert data["metadata"]["matches"] == 1
    assert data["metadata"]["files"] == 1
    assert data["metadata"]["truncated"] is True
    assert data["metadata"]["match_limit_reached"] is False
    assert data["metadata"]["content_truncated"] is True


def test_zeta_tool_bash_returns_handoff() -> None:
    result = CliRunner().invoke(
        zeta_cli,
        ["tool", "bash"],
        input=json.dumps({"command": "uv run pytest", "reason": "Run tests."}),
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["handoff"]["command"] == "uv run pytest"
    assert data["handoff"]["reason"] == "Run tests."


def test_zeta_tool_bash_direct_executes_command() -> None:
    data = zeta.run_tool(
        "bash",
        {"command": "printf direct-bash"},
        execution_mode="direct",
    )

    assert data["ok"] is True
    assert data["metadata"]["mode"] == "direct"
    assert data["metadata"]["status"] == 0
    assert data["metadata"]["stdout"] == "direct-bash"
    assert "direct-bash" in data["content"][0]["text"]


def test_zeta_tool_write_direct_writes_file(tmp_path: Path) -> None:
    target = tmp_path / "direct.txt"

    data = zeta.run_tool(
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

    result = CliRunner().invoke(
        zeta_cli,
        ["tool", "ls"],
        input=json.dumps({"path": str(tmp_path)}),
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
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

    result = CliRunner().invoke(
        zeta_cli,
        ["tool", "ls"],
        input=json.dumps(
            {
                "path": str(tmp_path),
                "recursive": True,
                "min_size_bytes": 10,
                "exclude": [".git"],
            }
        ),
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["content"][0]["text"].splitlines() == ["12\tfile\tsrc/large.bin"]
    assert data["metadata"]["entries"] == 1
    assert data["metadata"]["exclude"] == [".git"]


def test_zeta_tool_edit_writes_patch_artifact(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("old\n", encoding="utf-8")

    result = CliRunner().invoke(
        zeta_cli,
        ["tool", "edit"],
        input=json.dumps({"location": str(target), "old": "old\n", "new": "new\n"}),
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
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

    result = CliRunner().invoke(
        zeta_cli,
        ["tool", "edit"],
        input=json.dumps(payload),
    )

    assert validate_tool_args("edit", payload) == []
    assert result.exit_code == 0
    data = json.loads(result.output)
    artifact = Path(data["handoff"]["artifact"])
    patch = artifact.read_text(encoding="utf-8")
    assert data["handoff"]["command"].startswith("git apply ")
    assert data["handoff"]["reason"] == "Replace one line."
    assert "-old\n" in patch
    assert "+new\n" in patch


def test_zeta_tool_edit_direct_replace_writes_file(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("hello\nold\nbye\n", encoding="utf-8")

    data = zeta.run_tool(
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


def test_zeta_tool_edit_rejects_ambiguous_exact_replacement(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("old\nold\n", encoding="utf-8")

    result = CliRunner().invoke(
        zeta_cli,
        ["tool", "edit"],
        input=json.dumps({"location": str(target), "old": "old\n", "new": "new\n"}),
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"]["code"] == "old-text-not-unique"


def test_zeta_tool_edit_marks_no_newline_exact_replacement(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("old", encoding="utf-8")

    result = CliRunner().invoke(
        zeta_cli,
        ["tool", "edit"],
        input=json.dumps({"location": str(target), "old": "old", "new": "new"}),
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    artifact = Path(data["handoff"]["artifact"])
    patch = artifact.read_text(encoding="utf-8")
    assert "-old\n\\ No newline at end of file\n" in patch
    assert "+new\n\\ No newline at end of file\n" in patch


def test_zeta_transcript_append_and_tail(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "zeta-test")

    runner = CliRunner()
    appended = runner.invoke(
        zeta_cli,
        ["transcript", "append"],
        input=json.dumps({"type": "tool_call", "name": "read"}),
    )
    assert appended.exit_code == 0

    tail = runner.invoke(zeta_cli, ["transcript", "tail", "--limit", "1"])
    assert tail.exit_code == 0
    data = json.loads(tail.output)
    assert data["events"][0]["type"] == "tool_call"
    assert data["events"][0]["name"] == "read"


def test_zeta_transcript_does_not_expose_shell_handoff_verbs() -> None:
    result = CliRunner().invoke(zeta_cli, ["transcript", "shell-result"])

    assert result.exit_code != 0


def test_sigil_zeta_step_writes_handoff_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    handoff_file = tmp_path / "handoff.json"

    monkeypatch.setattr(zeta_runner, "ensure_server", lambda: True)
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
    monkeypatch.setattr(zeta_runner, "ensure_server", lambda: True)
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
    assert result.stdout == "\nsummary\n"
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

    monkeypatch.setattr(zeta_runner, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)
    monkeypatch.setattr(zeta_runner.runtime, "load_project_context", lambda: "ctx")

    code = zeta_runner.run_agent_step("answer me", glyph=",,")

    assert code == 0
    output = capsys.readouterr()
    assert output.out == "\nThe answer.\n"
    assert "❯ read" in output.err
    assert captured["context"] == "ctx"


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

    monkeypatch.setattr(zeta_runner, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)

    code = zeta_runner.run_agent_step("answer me", glyph=",,")

    assert code == 0
    assert cast(list[dict[str, Any]], captured["transcript"]) == []
    assert zeta.transcript_tail()[-1]["type"] == "user_message"
    assert capsys.readouterr().out == "\ndone\n"


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

    monkeypatch.setattr(zeta_runner, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)

    code = zeta_runner.run_agent_step("review", glyph=",,")

    assert code == 0
    config = cast(zeta_agent.AgentConfig, captured["config"])
    assert config.edit_mode == "review_patch"
    assert config.execution_mode == "handoff"


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

    monkeypatch.setattr(zeta_runner, "ensure_server", lambda: True)
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

    monkeypatch.setattr(zeta_runner, "ensure_server", lambda: True)
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)

    for glyph in (",,", ",,,"):
        code = zeta_runner.run_agent_step("inspect", glyph=glyph)

        assert code == 0
        assert "\nIt is a README.\n" in capsys.readouterr().out


def test_zeta_agent_step_prints_final_answer_after_direct_edit(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(zeta_runner, "ensure_server", lambda: True)
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
    assert output.out == "\nedited and verified\n"
    assert "❯ edit   a.txt  (applied · a.txt)" in output.err


def test_sigil_transcript_shell_turn_records_recent_turn(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "zeta-test")

    result = CliRunner().invoke(
        sigil_cli,
        ["transcript", "shell-turn"],
        input=json.dumps(
            {
                "command": "uv run pytest",
                "status": 1,
                "cwd": "/repo",
                "stderr_snippet": "test failed",
            }
        ),
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
    assert turns[0]["stderr_snippet"] == "test failed"


def test_zeta_edit_analysis_reports_location() -> None:
    data = zeta.analyze_tool(
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

    assert "Sigil" not in prompt
    assert "Preserve user changes." in prompt
    assert "Do not commit unless asked." in prompt
    assert "more local instructions\noverride earlier ones" in prompt
    assert "Available tools:" in prompt
    assert "- read(path, offset?, limit?): Read a UTF-8 text file." in prompt
    assert "- ls(path?, limit?, recursive?, min_size_bytes?, exclude?):" in prompt
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

    message = zeta.zeta_context_message("@reviewer: inspect the patch")

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

    message = zeta.zeta_context_message("@missing: inspect")

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

    message = zeta.zeta_context_message("@skill reviewer inspect")

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

    message = zeta.zeta_context_message("@reviewer inspect")

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
    monkeypatch.setattr(zeta_runner, "ensure_server", lambda: True)
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

    monkeypatch.setattr(zeta_runner, "ensure_server", fake_ensure_server)
    monkeypatch.setattr(zeta_runner, "run_agent_turn", fake_run_agent_turn)

    code = zeta_runner.run_agent_step("do work", glyph=",,")

    assert code == 0
    assert capsys.readouterr().out == "\ndone\n"
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
    monkeypatch.setattr(answers_runner, "ensure_server", lambda: True)
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

    monkeypatch.setattr(answers_runner, "ensure_server", fake_ensure_server)
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
    assert zeta.transcript_tail()[-1]["model"] == {
        "profile": "fast",
        "model": "fast-model",
        "url": "http://127.0.0.1:8081/v1/chat/completions",
    }


def test_sigil_transcript_shell_result_appends_tool_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "zeta-test")
    zeta.append_transcript(
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

    result = CliRunner().invoke(sigil_cli, ["transcript", "shell-result"])

    assert result.exit_code == 0
    event = json.loads(result.output)
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
    zeta.append_transcript(
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
    zeta.append_transcript(
        {
            "type": "tool_call",
            "id": "call-1",
            "tool_call_id": "call-1",
            "name": "bash",
            "input": {"command": "uv run pytest"},
        }
    )
    zeta.append_transcript(
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
    messages = zeta.transcript_chat_messages(zeta.transcript_tail())

    assert messages[0]["role"] == "assistant"
    assert messages[0]["tool_calls"][0]["id"] == "call-1"
    tool_messages = [message for message in messages if message["role"] == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "call-1"
    tool_content = json.loads(tool_messages[0]["content"])
    assert tool_content["type"] == SHELL_HANDOFF_RESULT_TYPE
    assert tool_content["executed_command"] == "uv run pytest"


def test_sigil_transcript_shell_result_cancels_modified_handoff(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "zeta-test")
    zeta.append_transcript(
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
    assert event["result"]["ok"] is False
    assert event["result"]["schema"] == SHELL_HANDOFF_RESULT_SCHEMA
    assert event["result"]["type"] == SHELL_HANDOFF_RESULT_TYPE
    assert event["result"]["outcome"] == SHELL_HANDOFF_OUTCOME_CANCELLED
    assert (
        event["result"]["cancellation_reason"]
        == SHELL_HANDOFF_CANCEL_EXPECTED_NOT_EXECUTED
    )
    assert event["result"]["expected_command"] == "uv run pytest"
    assert event["result"]["actual_command"] == "uv run pytest -q"
    assert event["result"]["shell_turns"][0]["command"] == "uv run pytest -q"


def test_sigil_transcript_shell_result_includes_intervening_shell_turns(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIGIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGIL_SESSION_ID", "zeta-test")
    zeta.append_transcript(
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
    zeta.append_transcript(
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

    monkeypatch.setattr(answers_runner, "ensure_server", lambda: True)
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

    monkeypatch.setattr(answers_runner, "ensure_server", lambda: True)
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

    monkeypatch.setattr(answers_runner, "ensure_server", lambda: True)
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

    monkeypatch.setattr(answers_runner, "ensure_server", lambda: True)
    monkeypatch.setattr(answers_runner, "run_agent_turn", fake_run_agent_turn)
    monkeypatch.setattr(
        answers_runner,
        "chat_text",
        lambda system, prompt, max_tokens: "It contains Sigil docs.",
    )

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
    ) -> str:
        del system, prompt, max_tokens
        captured["selected_model"] = selected_model
        captured["selected_url"] = selected_url
        return "Fallback answer."

    monkeypatch.setattr(answers_runner, "ensure_server", lambda **kwargs: True)
    monkeypatch.setattr(answers_runner, "run_agent_turn", fake_run_agent_turn)
    monkeypatch.setattr(answers_runner, "chat_text", fake_chat_text)

    code = answers_runner.run_tool_answer("question system", "Question?", max_steps=1)

    output = capsys.readouterr().out
    assert code == 0
    assert "\nFallback answer.\n" in output
    assert captured["selected_model"] == "fast-model"
    assert captured["selected_url"] == "http://127.0.0.1:8081/v1/chat/completions"
