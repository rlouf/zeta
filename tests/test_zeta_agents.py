"""Authored agent spec tests."""

import asyncio
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from zeta.agents.events import EventRegistry
from zeta.agents.manifest import (
    AgentPlugin,
    EgressBinding,
    IngressBinding,
    Manifest,
    ManifestError,
    PluginManifestSection,
    egress_bindings,
    ingress_bindings,
)
from zeta.agents.prompts import TemplateError, render_prompt, validate_prompt
from zeta.agents.resources import (
    ResourceError,
    load_agent_project,
    load_event_registry,
    load_skill_registry,
    validate_agent_project,
)
from zeta.agents.returns import derive_returns_schema
from zeta.agents.spec import (
    AgentSpec,
    ScheduleEntry,
    SpecError,
    load_spec,
    load_specs,
    matches,
    scheduled_event_type,
)
from zeta.capabilities.execution import (
    InProcessCapabilityExecutor,
)
from zeta.capabilities.registry import CapabilityRegistry, RegisteredCapability
from zeta.capabilities.types import (
    Capability,
    CapabilityId,
)
from zeta.events import DraftEvent, Event
from zeta.orchestration import dispatch as zeta_dispatch
from zeta.orchestration import queue as zeta_queue
from zeta.orchestration import scheduling as zeta_scheduling
from zeta.orchestration import worker as zeta_worker
from zeta.orchestration.agents import (
    AgentDefinition,
    EventPattern,
    agent_session_id,
    compile_agent_definition,
    compile_agent_definitions,
)
from zeta.orchestration.projections import runtime_event_projection
from zeta.records.stores import Filter, SqliteEventStore
from zeta.run.config import AgentConfig
from zeta.run.runtime import AgentRunResult


def runtime_sqlite_event_store(path: Path) -> SqliteEventStore:
    return SqliteEventStore(path, projections=(runtime_event_projection(),))


zeta_agents = SimpleNamespace(
    EgressBinding=EgressBinding,
    EventRegistry=EventRegistry,
    IngressBinding=IngressBinding,
    AgentPlugin=AgentPlugin,
    Manifest=Manifest,
    ManifestError=ManifestError,
    PluginManifestSection=PluginManifestSection,
    ResourceError=ResourceError,
    ScheduleEntry=ScheduleEntry,
    SpecError=SpecError,
    TemplateError=TemplateError,
    compile_agent_definition=compile_agent_definition,
    compile_agent_definitions=compile_agent_definitions,
    derive_returns_schema=derive_returns_schema,
    load_agent_project=load_agent_project,
    load_event_registry=load_event_registry,
    load_spec=load_spec,
    load_specs=load_specs,
    load_skill_registry=load_skill_registry,
    matches=matches,
    egress_bindings=egress_bindings,
    ingress_bindings=ingress_bindings,
    render_prompt=render_prompt,
    scheduled_event_type=scheduled_event_type,
    validate_agent_project=validate_agent_project,
    validate_prompt=validate_prompt,
)
zeta_events = SimpleNamespace(
    DraftEvent=DraftEvent,
    Filter=Filter,
    SqliteEventStore=runtime_sqlite_event_store,
)


def _write_spec(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def _read_capability() -> RegisteredCapability:
    return RegisteredCapability(
        Capability(
            CapabilityId("host", "read"),
            "Read a file.",
            {"type": "object"},
        ),
        InProcessCapabilityExecutor(lambda params: {"ok": True}),
    )


def _slack_plugin(
    *,
    message_schema: dict[str, Any] | None = None,
    ingress_poller: Callable[..., Any] | None = None,
    egress_handler: Callable[..., Any] | None = None,
) -> AgentPlugin:
    ingress_pollers = (
        {"slack.dm.received": ingress_poller} if ingress_poller is not None else {}
    )
    egress_handlers = (
        {"slack.message.send.requested": egress_handler}
        if egress_handler is not None
        else {}
    )
    ingress_filter_schema = {
        "type": "object",
        "required": ["channel_ids"],
        "properties": {
            "channel_ids": {
                "type": "array",
                "items": {"type": "string"},
            }
        },
        "additionalProperties": False,
    }
    egress_filter_schema = {
        "type": "object",
        "properties": {
            "channel_ids": {
                "type": "array",
                "items": {"type": "string"},
            }
        },
        "additionalProperties": False,
    }
    return zeta_agents.AgentPlugin(
        name="slack",
        events={
            "slack.dm.received": message_schema
            or {
                "type": "object",
                "required": ["text"],
                "properties": {"text": {"type": "string"}},
                "additionalProperties": False,
            },
            "slack.message.send.requested": {
                "type": "object",
                "required": ["channel_id", "text"],
                "properties": {
                    "channel_id": {"type": "string"},
                    "text": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        manifest_sections={
            "ingress": zeta_agents.PluginManifestSection(
                "ingress",
                schema={
                    "type": "object",
                    "required": ["source"],
                    "properties": {
                        "source": {"const": "slack"},
                        "event": {"type": "string"},
                        "filter": {"type": "object"},
                        "idempotency_key": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
                events={"slack.dm.received": ingress_filter_schema},
            ),
            "egress": zeta_agents.PluginManifestSection(
                "egress",
                schema={
                    "type": "object",
                    "required": ["sink"],
                    "properties": {
                        "sink": {"const": "slack"},
                        "event": {"type": "string"},
                        "filter": {"type": "object"},
                        "idempotency_key": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
                events={"slack.message.send.requested": egress_filter_schema},
            ),
        },
        ingress_pollers=ingress_pollers,
        egress_handlers=egress_handlers,
    )


def _audit_plugin() -> AgentPlugin:
    return zeta_agents.AgentPlugin(
        name="audit",
        manifest_sections={
            "audit": zeta_agents.PluginManifestSection(
                "audit",
                schema={
                    "type": "object",
                    "required": ["level"],
                    "properties": {"level": {"enum": ["info", "strict"]}},
                    "additionalProperties": False,
                },
            )
        },
    )


def _approval_plugin(name: str) -> AgentPlugin:
    return zeta_agents.AgentPlugin(
        name=name,
        manifest_sections={
            "approval": zeta_agents.PluginManifestSection(
                "approval",
                schema={
                    "type": "object",
                    "properties": {"required": {"type": "boolean"}},
                    "additionalProperties": False,
                },
            )
        },
    )


class PluginMap:
    def __init__(self, *plugins: AgentPlugin) -> None:
        self.plugins = {plugin.name: plugin for plugin in plugins}

    def resolve(self, name: str) -> AgentPlugin | None:
        return self.plugins.get(name)

    def names_for_section(self, key: str, value: Any) -> tuple[str, ...]:
        if key == "ingress" and isinstance(value, list | tuple):
            return tuple(
                item["source"]
                for item in value
                if isinstance(item, dict) and isinstance(item.get("source"), str)
            )
        if key == "egress" and isinstance(value, list | tuple):
            return tuple(
                item["sink"]
                for item in value
                if isinstance(item, dict) and isinstance(item.get("sink"), str)
            )
        return tuple(
            plugin.name
            for plugin in self.plugins.values()
            if key in plugin.manifest_sections
        )


def _slack_return_agent_spec(tmp_path: Path) -> AgentSpec:
    return zeta_agents.load_spec(
        _write_spec(
            tmp_path / "slack-qa.md",
            """---
name: Slack Q&A
description: Answers workspace questions in Slack.
accepts:
  - slack.dm.received
returns:
  - message.delivery.requested
tools:
  - read
---
User asked: {{ event.payload.text }}
""",
        )
    )


def _slack_return_event_registry() -> EventRegistry:
    return zeta_agents.EventRegistry(
        {
            "slack.dm.received": {
                "type": "object",
                "required": ["text"],
                "properties": {"text": {"type": "string"}},
                "additionalProperties": False,
            },
            "message.delivery.requested": {
                "type": "object",
                "required": ["channel_id", "text"],
                "properties": {
                    "channel_id": {"type": "string"},
                    "text": {"type": "string"},
                },
                "additionalProperties": False,
            },
        }
    )


def _recording_return_run(
    calls: list[dict[str, Any]],
) -> Callable[..., Any]:
    async def run_turn(
        objective: str,
        timeline: list[dict[str, Any]],
        config: AgentConfig,
        **kwargs: Any,
    ) -> AgentRunResult:
        calls.append(
            {
                "objective": objective,
                "timeline": timeline,
                "config": config,
                "kwargs": kwargs,
            }
        )
        return AgentRunResult(final_answer="Send a reply to C1.")

    return run_turn


def _recording_structured_return(
    calls: list[dict[str, Any]],
) -> Callable[..., Any]:
    def structured_output(
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        calls.append({"messages": messages, "kwargs": kwargs})
        return {
            "type": "message.delivery.requested",
            "payload": {"channel_id": "C1", "text": "hello"},
        }

    return structured_output


def _assert_return_run_called(calls: list[dict[str, Any]]) -> None:
    assert len(calls) == 1
    assert calls[0]["objective"] == "User asked: hello"


def _assert_structured_return_called(
    calls: list[dict[str, Any]],
    spec: AgentSpec,
    events: EventRegistry,
) -> None:
    assert len(calls) == 1
    assert calls[0]["kwargs"]["response_name"] == "zeta_agent_return"
    assert calls[0]["kwargs"]["schema"] == zeta_agents.derive_returns_schema(
        spec, events
    )
    assert calls[0]["kwargs"]["selected_model"] is None
    assert calls[0]["kwargs"]["api"] is None
    assert "Send a reply to C1." in calls[0]["messages"][1]["content"]


def _assert_message_delivery_event(events: list[Any]) -> None:
    assert len(events) == 1
    assert events[0].source == "agent:slack-qa"
    assert events[0].payload["channel_id"] == "C1"
    assert events[0].payload["text"] == "hello"


def _assert_terminal_return_event(terminal: dict[str, Any] | None) -> None:
    assert terminal is not None
    assert terminal["final_answer"] == "Send a reply to C1."
    assert terminal["returned_events"][0]["type"] == "message.delivery.requested"


def test_zeta_agent_spec_loads_frontmatter_body_and_manifest(tmp_path: Path) -> None:
    spec_path = _write_spec(
        tmp_path / "slack-qa.md",
        """---
name: Slack Q&A
description: Answers workspace questions in Slack.
enabled: true
resumable: true
accepts:
  - slack.dm.received
returns:
  - message.delivery.requested
tools:
  - read
schedules:
  - cron: "* * * * *"
writes:
  paths:
    - docs/**.md
---
User asked: {{ event.payload.text }}
""",
    )

    spec = zeta_agents.load_spec(spec_path)

    assert spec.slug == "slack-qa"
    assert spec.name == "Slack Q&A"
    assert spec.description == "Answers workspace questions in Slack."
    assert spec.enabled is True
    assert spec.resumable is True
    assert spec.accepts == ("slack.dm.received", "agent.slack-qa.scheduled")
    assert spec.returns == ("message.delivery.requested",)
    assert spec.tools == ("read",)
    assert spec.skills == ()
    assert spec.schedules == (
        zeta_agents.ScheduleEntry(
            cron="* * * * *",
            timezone=None,
        ),
    )
    assert spec.manifest == {"writes": {"paths": ["docs/**.md"]}}
    assert spec.instructions == "User asked: {{ event.payload.text }}\n"
    assert len(spec.sha256) == 64


def test_zeta_agent_spec_keeps_ingress_and_egress_as_manifest_sections(
    tmp_path: Path,
) -> None:
    spec = zeta_agents.load_spec(
        _write_spec(
            tmp_path / "support.md",
            """---
name: Support
description: Replies to Slack support messages.
accepts:
  - slack.dm.received
returns:
  - slack.message.send.requested
ingress:
  - source: slack
    event: slack.dm.received
    filter:
      channel_ids: ["C123"]
    idempotency_key: "slack:message:{team_id}:{channel_id}:{message_ts}"
egress:
  - sink: slack
    event: slack.message.send.requested
    filter:
      channel_ids: ["C123"]
---
Reply.
""",
        )
    )

    assert spec.manifest == {
        "ingress": [
            {
                "source": "slack",
                "event": "slack.dm.received",
                "filter": {"channel_ids": ["C123"]},
                "idempotency_key": "slack:message:{team_id}:{channel_id}:{message_ts}",
            }
        ],
        "egress": [
            {
                "sink": "slack",
                "event": "slack.message.send.requested",
                "filter": {"channel_ids": ["C123"]},
            }
        ],
    }
    assert zeta_agents.ingress_bindings(spec) == (
        zeta_agents.IngressBinding(
            source="slack",
            event="slack.dm.received",
            filter={"channel_ids": ["C123"]},
            idempotency_key="slack:message:{team_id}:{channel_id}:{message_ts}",
        ),
    )
    assert zeta_agents.egress_bindings(spec) == (
        zeta_agents.EgressBinding(
            sink="slack",
            event="slack.message.send.requested",
            filter={"channel_ids": ["C123"]},
            idempotency_key=None,
        ),
    )


def test_zeta_agent_spec_loads_skills_as_core_metadata(tmp_path: Path) -> None:
    spec = zeta_agents.load_spec(
        _write_spec(
            tmp_path / "reviewer.md",
            """---
name: Reviewer
description: Reviews changes.
skills:
  - code-review
  - release-notes
mode: strict
---
Review the change.
""",
        )
    )

    assert spec.skills == ("code-review", "release-notes")
    assert spec.manifest == {"mode": "strict"}


def test_zeta_agent_specs_load_only_flat_agent_files(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_spec(
        agents_dir / "top-level.md",
        """---
name: Top Level
description: Loads as an agent.
---
Run.
""",
    )
    for directory in ("skills", "events", "tools", "nested-agent"):
        nested = agents_dir / directory
        nested.mkdir()
        _write_spec(
            nested / "ignored.md",
            """---
name: Ignored
description: Should not load.
---
Ignore.
""",
        )

    specs = zeta_agents.load_specs(agents_dir)

    assert [spec.slug for spec in specs] == ["top-level"]


def test_zeta_agent_spec_adds_synthetic_schedule_event(
    tmp_path: Path,
) -> None:
    spec = zeta_agents.load_spec(
        _write_spec(
            tmp_path / "scheduled.md",
            """---
name: Scheduled
description: Runs on a schedule.
schedules:
  - cron: "* * * * *"
---
Summarize the repo.
""",
        )
    )

    assert spec.accepts == ("agent.scheduled.scheduled",)
    assert spec.schedules == (
        zeta_agents.ScheduleEntry(
            cron="* * * * *",
            timezone=None,
        ),
    )
    assert zeta_agents.scheduled_event_type("scheduled") == "agent.scheduled.scheduled"


@pytest.mark.parametrize("field", ["event", "payload"])
def test_zeta_agent_spec_rejects_schedule_event_source_fields(
    tmp_path: Path,
    field: str,
) -> None:
    extra = (
        "    event: repo.digest.requested\n"
        if field == "event"
        else "    payload:\n      reason: scheduled\n"
    )

    with pytest.raises(zeta_agents.SpecError, match=f"{field} is not supported"):
        zeta_agents.load_spec(
            _write_spec(
                tmp_path / "scheduled.md",
                f"""---
name: Scheduled
description: Runs on a schedule.
schedules:
  - cron: "* * * * *"
{extra}---
Summarize the repo.
""",
            )
        )


@pytest.mark.parametrize(
    ("frontmatter", "message"),
    [
        ("name: 1\ndescription: Worker\n", "name"),
        ("name: Worker\ndescription: 1\n", "description"),
        ("name: Worker\ndescription: Worker\nenabled: maybe\n", "enabled"),
        ("name: Worker\ndescription: Worker\nresumable: later\n", "resumable"),
        (
            "name: Worker\ndescription: Worker\naccepts: github.issue.opened\n",
            "accepts",
        ),
        ("name: Worker\ndescription: Worker\nreturns:\n  - 1\n", "returns"),
        ("name: Worker\ndescription: Worker\ntools:\n  - read\n  - 2\n", "tools"),
        ("name: Worker\ndescription: Worker\nskills:\n  - review\n  - 2\n", "skills"),
        ("name: Worker\ndescription: Worker\nschedules: hourly\n", "schedules"),
        (
            "name: Worker\ndescription: Worker\naccepts:\n"
            "  - runtime.schedule.triggered\nschedules:\n  - soon\n",
            "schedules",
        ),
        (
            "name: Worker\ndescription: Worker\naccepts:\n"
            "  - runtime.schedule.triggered\nschedules:\n"
            "  - cron: '* * * * *'\n    payload: scheduled\n",
            "payload",
        ),
        (
            "name: Worker\ndescription: Worker\naccepts:\n"
            "  - runtime.schedule.triggered\nschedules:\n"
            "  - cron: '* * * * *'\n    timezone: 1\n",
            "timezone",
        ),
    ],
)
def test_zeta_agent_spec_rejects_invalid_frontmatter_values(
    tmp_path: Path,
    frontmatter: str,
    message: str,
) -> None:
    spec_path = _write_spec(
        tmp_path / "worker.md",
        f"""---
{frontmatter}---
Do work.
""",
    )

    with pytest.raises(ValueError, match=message):
        zeta_agents.load_spec(spec_path)


@pytest.mark.parametrize(
    "frontmatter",
    [
        "name: Worker\ndescription: Worker\ningress: slack\n",
        "name: Worker\ndescription: Worker\ningress:\n  - source: 1\n",
        (
            "name: Worker\ndescription: Worker\ningress:\n"
            "  - source: slack\n    extra: nope\n"
        ),
        (
            "name: Worker\ndescription: Worker\ningress:\n"
            "  - source: slack\n    filter: C123\n"
        ),
        "name: Worker\ndescription: Worker\negress: slack\n",
        "name: Worker\ndescription: Worker\negress:\n  - sink: 1\n",
        (
            "name: Worker\ndescription: Worker\negress:\n"
            "  - sink: slack\n    accepts: 1\n"
        ),
    ],
)
def test_zeta_agent_spec_defers_plugin_section_validation(
    tmp_path: Path,
    frontmatter: str,
) -> None:
    spec = zeta_agents.load_spec(
        _write_spec(
            tmp_path / "worker.md",
            f"""---
{frontmatter}---
Run.
""",
        )
    )

    assert spec.manifest


def test_zeta_agent_spec_validates_renders_matches_and_derives_schema(
    tmp_path: Path,
) -> None:
    spec = zeta_agents.load_spec(
        _write_spec(
            tmp_path / "slack-qa.md",
            """---
name: Slack Q&A
description: Answers workspace questions in Slack.
accepts:
  - slack.dm.received
returns:
  - message.delivery.requested
tools:
  - read
---
User asked: {{ event.payload.text }}
""",
        )
    )
    tools = CapabilityRegistry()
    tools.register(_read_capability())
    events = zeta_agents.EventRegistry(
        {
            "slack.dm.received": {
                "type": "object",
                "required": ["text"],
                "properties": {"text": {"type": "string"}},
            },
            "message.delivery.requested": {
                "type": "object",
                "required": ["channel_id", "text"],
                "properties": {
                    "channel_id": {"type": "string"},
                    "text": {"type": "string"},
                },
            },
        }
    )

    zeta_agents.Manifest(tools=tools, events=events).validate(spec)
    rendered = zeta_agents.render_prompt(
        spec,
        {"event_type": "slack.dm.received", "payload": {"text": "why is this slow?"}},
    )
    returns_schema = zeta_agents.derive_returns_schema(spec, events)

    assert zeta_agents.matches(spec, "slack.dm.received")
    assert not zeta_agents.matches(spec, "slack.dm.sent")
    assert rendered == "User asked: why is this slow?"
    assert returns_schema == {
        "type": "object",
        "anyOf": [
            {
                "type": "object",
                "required": ["type", "payload"],
                "properties": {
                    "type": {"const": "message.delivery.requested"},
                    "payload": {
                        "type": "object",
                        "required": ["channel_id", "text"],
                        "properties": {
                            "channel_id": {"type": "string"},
                            "text": {"type": "string"},
                        },
                    },
                },
                "additionalProperties": False,
            }
        ],
    }


def test_zeta_agent_manifest_rejects_unknown_tool(tmp_path: Path) -> None:
    spec = zeta_agents.load_spec(
        _write_spec(
            tmp_path / "worker.md",
            """---
name: Worker
description: Does work.
tools:
  - Missing
---
Use a tool.
""",
        )
    )

    with pytest.raises(zeta_agents.ManifestError, match="unknown tool 'Missing'"):
        zeta_agents.Manifest(tools=CapabilityRegistry()).validate(spec)


def test_zeta_agent_manifest_rejects_unknown_skill(tmp_path: Path) -> None:
    spec = zeta_agents.load_spec(
        _write_spec(
            tmp_path / "worker.md",
            """---
name: Worker
description: Does work.
skills:
  - missing
---
Use a skill.
""",
        )
    )

    with pytest.raises(zeta_agents.ManifestError, match="unknown skill 'missing'"):
        zeta_agents.Manifest(skills={}).validate(spec)


def test_zeta_agent_resource_loaders_read_flat_skills_and_events(
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    skills_dir = agents_dir / "skills"
    events_dir = agents_dir / "events"
    skills_dir.mkdir(parents=True)
    events_dir.mkdir()
    _write_spec(skills_dir / "code-review.md", "Review for correctness.\n")
    (events_dir / "github.pr.opened.json").write_text(
        """{
  "schema": {
    "type": "object",
    "required": ["title"],
    "properties": {
      "title": {
        "type": "string"
      }
    }
  }
}
""",
        encoding="utf-8",
    )
    (events_dir / "release.ready.json").write_text(
        """{
  "type": "object",
  "properties": {
    "version": {
      "type": "string"
    }
  }
}
""",
        encoding="utf-8",
    )

    skills = zeta_agents.load_skill_registry(agents_dir)
    events = zeta_agents.load_event_registry(agents_dir)

    assert skills.knows("code-review")
    assert events.knows("github.pr.opened")
    assert events.schema("github.pr.opened") == {
        "type": "object",
        "required": ["title"],
        "properties": {"title": {"type": "string"}},
    }
    assert events.schema("release.ready") == {
        "type": "object",
        "properties": {"version": {"type": "string"}},
    }


def test_zeta_agent_project_merges_plugin_event_schemas(
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_spec(
        agents_dir / "support.md",
        """---
name: Support
description: Replies to Slack support messages.
accepts:
  - slack.dm.received
returns:
  - slack.message.send.requested
ingress:
  - source: slack
    filter:
      channel_ids: ["C123"]
    idempotency_key: "slack:message:{team_id}:{channel_id}:{message_ts}"
egress:
  - sink: slack
    filter:
      channel_ids: ["C123"]
---
Reply.
""",
    )

    project = zeta_agents.load_agent_project(
        agents_dir,
        plugin_resolver=PluginMap(_slack_plugin()),
    )
    zeta_agents.validate_agent_project(project)

    assert project.events.knows("slack.dm.received")
    assert project.events.knows("slack.message.send.requested")
    assert zeta_agents.ingress_bindings(project.specs[0])[0].event is None
    assert zeta_agents.egress_bindings(project.specs[0])[0].event is None


def test_zeta_agent_project_rejects_conflicting_plugin_event_schema(
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    events_dir = agents_dir / "events"
    events_dir.mkdir(parents=True)
    _write_spec(
        agents_dir / "support.md",
        """---
name: Support
description: Replies to Slack support messages.
accepts:
  - slack.dm.received
ingress:
  - source: slack
    filter:
      channel_ids: ["C123"]
    idempotency_key: "slack:message:{team_id}:{channel_id}:{message_ts}"
---
Reply.
""",
    )
    (events_dir / "slack.dm.received.json").write_text(
        '{"type":"object","required":["body"],"properties":{"body":{"type":"string"}}}',
        encoding="utf-8",
    )

    with pytest.raises(zeta_agents.ResourceError, match="conflicts"):
        zeta_agents.load_agent_project(
            agents_dir,
            plugin_resolver=PluginMap(_slack_plugin()),
        )


def test_zeta_agent_project_accepts_identical_local_plugin_event_schema(
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    events_dir = agents_dir / "events"
    events_dir.mkdir(parents=True)
    schema = {
        "type": "object",
        "required": ["text"],
        "properties": {"text": {"type": "string"}},
        "additionalProperties": False,
    }
    _write_spec(
        agents_dir / "support.md",
        """---
name: Support
description: Replies to Slack support messages.
accepts:
  - slack.dm.received
ingress:
  - source: slack
    filter:
      channel_ids: ["C123"]
    idempotency_key: "slack:message:{team_id}:{channel_id}:{message_ts}"
---
Reply.
""",
    )
    (events_dir / "slack.dm.received.json").write_text(
        '{"type":"object","required":["text"],"properties":{"text":{"type":"string"}},"additionalProperties":false}',
        encoding="utf-8",
    )

    project = zeta_agents.load_agent_project(
        agents_dir,
        plugin_resolver=PluginMap(_slack_plugin(message_schema=schema)),
    )

    assert project.events.schema("slack.dm.received") == schema


def test_zeta_agent_project_validates_generic_plugin_manifest_section(
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_spec(
        agents_dir / "audited.md",
        """---
name: Audited
description: Uses an audit plugin manifest section.
audit:
  level: strict
---
Run.
""",
    )

    project = zeta_agents.load_agent_project(
        agents_dir,
        plugin_resolver=PluginMap(_audit_plugin()),
    )

    zeta_agents.validate_agent_project(project)


def test_zeta_agent_project_rejects_unknown_manifest_section(
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_spec(
        agents_dir / "worker.md",
        """---
name: Worker
description: Uses an unknown plugin manifest section.
mode: strict
---
Run.
""",
    )
    project = zeta_agents.load_agent_project(agents_dir)

    with pytest.raises(zeta_agents.ManifestError, match="unknown manifest section"):
        zeta_agents.validate_agent_project(project)


def test_zeta_agent_project_rejects_conflicting_manifest_section_claims(
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_spec(
        agents_dir / "worker.md",
        """---
name: Worker
description: Uses a contested plugin manifest section.
approval:
  required: true
---
Run.
""",
    )
    project = zeta_agents.load_agent_project(
        agents_dir,
        plugin_resolver=PluginMap(
            _approval_plugin("linear"),
            _approval_plugin("github"),
        ),
    )

    with pytest.raises(zeta_agents.ManifestError, match="multiple plugins"):
        zeta_agents.validate_agent_project(project)


def test_zeta_agent_project_rejects_invalid_generic_plugin_manifest_section(
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_spec(
        agents_dir / "audited.md",
        """---
name: Audited
description: Uses an audit plugin manifest section.
audit:
  level: loud
---
Run.
""",
    )
    project = zeta_agents.load_agent_project(
        agents_dir,
        plugin_resolver=PluginMap(_audit_plugin()),
    )

    with pytest.raises(zeta_agents.ManifestError, match="invalid manifest section"):
        zeta_agents.validate_agent_project(project)


@pytest.mark.parametrize(
    ("frontmatter", "message"),
    [
        (
            """accepts:
  - slack.dm.received
ingress:
  - source: missing
    idempotency_key: "k"
""",
            "unknown ingress source 'missing'",
        ),
        (
            """accepts:
  - slack.dm.received
ingress:
  - source: slack
    event: slack.channel.joined
    idempotency_key: "k"
""",
            "does not support event 'slack.channel.joined'",
        ),
        (
            """accepts:
  - other.event
ingress:
  - source: slack
    event: slack.dm.received
    idempotency_key: "k"
""",
            "not listed in accepts",
        ),
        (
            """accepts:
  - slack.dm.received
ingress:
  - source: slack
""",
            "requires idempotency_key",
        ),
        (
            """accepts:
  - slack.dm.received
ingress:
  - source: slack
    filter:
      channel_ids: [1]
    idempotency_key: "k"
""",
            "invalid ingress filter",
        ),
        (
            """returns:
  - slack.message.send.requested
egress:
  - sink: missing
    event: slack.message.send.requested
""",
            "unknown egress sink 'missing'",
        ),
        (
            """returns:
  - slack.message.send.requested
egress:
  - sink: slack
    event: slack.message.delete.requested
""",
            "does not support event 'slack.message.delete.requested'",
        ),
        (
            """returns:
  - other.event
egress:
  - sink: slack
    event: slack.message.send.requested
""",
            "not listed in returns",
        ),
    ],
)
def test_zeta_agent_project_validates_plugin_bindings(
    tmp_path: Path,
    frontmatter: str,
    message: str,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_spec(
        agents_dir / "support.md",
        f"""---
name: Support
description: Replies to Slack support messages.
{frontmatter}---
Reply.
""",
    )
    project = zeta_agents.load_agent_project(
        agents_dir,
        plugin_resolver=PluginMap(_slack_plugin()),
    )

    with pytest.raises(zeta_agents.ManifestError, match=message):
        zeta_agents.validate_agent_project(project)


def test_zeta_ingress_once_appends_plugin_events(
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_spec(
        agents_dir / "support.md",
        """---
name: Support
description: Replies to Slack support messages.
accepts:
  - slack.dm.received
ingress:
  - source: slack
    filter:
      channel_ids: ["C123"]
    idempotency_key: "slack:message:{team_id}:{channel_id}:{message_ts}"
---
Reply.
""",
    )

    def poll_slack(binding: IngressBinding) -> list[DraftEvent]:
        assert binding.filter == {"channel_ids": ["C123"]}
        return [
            zeta_events.DraftEvent(
                "slack.dm.received",
                "slack",
                {
                    "team_id": "T1",
                    "channel_id": "C123",
                    "message_ts": "42",
                    "text": "hello",
                },
            )
        ]

    plugin = _slack_plugin(
        message_schema={
            "type": "object",
            "required": ["team_id", "channel_id", "message_ts", "text"],
            "properties": {
                "team_id": {"type": "string"},
                "channel_id": {"type": "string"},
                "message_ts": {"type": "string"},
                "text": {"type": "string"},
            },
            "additionalProperties": False,
        },
        ingress_poller=poll_slack,
    )
    runtime = zeta_worker.WorkerServices(
        project_root=tmp_path,
        state_dir=tmp_path / ".zeta",
        events=zeta_events.SqliteEventStore(tmp_path / "events.sqlite3"),
        plugin_resolver=PluginMap(plugin),
    )

    try:
        inserted = asyncio.run(zeta_worker.run_ingress_once(runtime))
        events = runtime.events.list_events(
            zeta_events.Filter(event_type="slack.dm.received")
        )
    finally:
        runtime.close()

    assert inserted == 1
    assert len(events) == 1
    assert events[0].source == "slack"
    assert events[0].payload["text"] == "hello"
    assert events[0].idempotency_key == "slack:message:T1:C123:42"


def test_zeta_ingress_forever_continues_after_plugin_failure(
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_spec(
        agents_dir / "support.md",
        """---
name: Support
description: Replies to Slack support messages.
accepts:
  - slack.dm.received
ingress:
  - source: slack
    filter:
      channel_ids: ["C123"]
    idempotency_key: "slack:message:{team_id}:{channel_id}:{message_ts}"
---
Reply.
""",
    )
    stop_event = asyncio.Event()
    calls = 0

    def poll_slack(_binding: IngressBinding) -> list[DraftEvent]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("slack unavailable")
        stop_event.set()
        return [
            zeta_events.DraftEvent(
                "slack.dm.received",
                "slack",
                {
                    "team_id": "T1",
                    "channel_id": "C123",
                    "message_ts": "42",
                    "text": "hello",
                },
            )
        ]

    plugin = _slack_plugin(
        message_schema={
            "type": "object",
            "required": ["team_id", "channel_id", "message_ts", "text"],
            "properties": {
                "team_id": {"type": "string"},
                "channel_id": {"type": "string"},
                "message_ts": {"type": "string"},
                "text": {"type": "string"},
            },
            "additionalProperties": False,
        },
        ingress_poller=poll_slack,
    )
    runtime = zeta_worker.WorkerServices(
        project_root=tmp_path,
        state_dir=tmp_path / ".zeta",
        events=zeta_events.SqliteEventStore(tmp_path / "events.sqlite3"),
        plugin_resolver=PluginMap(plugin),
    )

    try:
        asyncio.run(
            zeta_worker.run_ingress_forever(
                runtime,
                poll_interval_seconds=0,
                stop_event=stop_event,
            )
        )
        events = runtime.events.list_events(
            zeta_events.Filter(event_type="slack.dm.received")
        )
    finally:
        runtime.close()

    assert calls == 2
    assert [event.payload["text"] for event in events] == ["hello"]


def test_zeta_egress_binding_handles_returned_event(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str]] = []
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_spec(
        agents_dir / "support.md",
        """---
name: Support
description: Sends Slack support messages.
returns:
  - slack.message.send.requested
egress:
  - sink: slack
    filter:
      channel_ids: ["C123"]
---
Send.
""",
    )

    async def send_slack(
        event: Event,
        binding: EgressBinding,
        idempotency_key: str,
    ) -> dict[str, str]:
        assert binding.filter == {"channel_ids": ["C123"]}
        calls.append((event.payload["text"], idempotency_key))
        return {"provider_message_id": "m1"}

    runtime = zeta_worker.WorkerServices(
        project_root=tmp_path,
        state_dir=tmp_path / ".zeta",
        events=zeta_events.SqliteEventStore(tmp_path / "events.sqlite3"),
        plugin_resolver=PluginMap(_slack_plugin(egress_handler=send_slack)),
    )
    runtime.events.accept(
        zeta_events.DraftEvent(
            "slack.message.send.requested",
            "agent:support",
            {"channel_id": "C123", "text": "hello"},
        )
    )

    try:
        message = asyncio.run(zeta_worker.run_once(runtime))
        egress_events = runtime.events.list_events(
            zeta_events.Filter(event_type_prefix="runtime.egress.")
        )
        queue_items = zeta_queue.project_queue_items(
            runtime.events.list_events(zeta_events.Filter())
        )
    finally:
        runtime.close()

    assert message.startswith("ran qi_")
    assert calls == [("hello", f"slack:{egress_events[0].caused_by}")]
    assert [event.event_type for event in egress_events] == [
        "runtime.egress.started",
        "runtime.egress.completed",
    ]
    assert egress_events[1].payload["result"] == {"provider_message_id": "m1"}
    assert [item.status for item in queue_items] == ["completed"]


def test_zeta_egress_binding_records_failure_without_failing_queue_item(
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_spec(
        agents_dir / "support.md",
        """---
name: Support
description: Sends Slack support messages.
returns:
  - slack.message.send.requested
egress:
  - sink: slack
    filter:
      channel_ids: ["C123"]
---
Send.
""",
    )

    async def send_slack(
        _event: Event,
        _binding: EgressBinding,
        _idempotency_key: str,
    ) -> None:
        raise RuntimeError("slack unavailable")

    runtime = zeta_worker.WorkerServices(
        project_root=tmp_path,
        state_dir=tmp_path / ".zeta",
        events=zeta_events.SqliteEventStore(tmp_path / "events.sqlite3"),
        plugin_resolver=PluginMap(_slack_plugin(egress_handler=send_slack)),
    )
    runtime.events.accept(
        zeta_events.DraftEvent(
            "slack.message.send.requested",
            "agent:support",
            {"channel_id": "C123", "text": "hello"},
        )
    )

    try:
        message = asyncio.run(zeta_worker.run_once(runtime))
        egress_events = runtime.events.list_events(
            zeta_events.Filter(event_type_prefix="runtime.egress.")
        )
        queue_items = zeta_queue.project_queue_items(
            runtime.events.list_events(zeta_events.Filter())
        )
    finally:
        runtime.close()

    assert message.startswith("ran qi_")
    assert [event.event_type for event in egress_events] == [
        "runtime.egress.started",
        "runtime.egress.failed",
    ]
    assert egress_events[1].payload["error"] == "slack unavailable"
    assert [item.status for item in queue_items] == ["completed"]


def test_zeta_scheduler_loads_project_with_plugin_bindings(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_spec(
        agents_dir / "scheduled.md",
        """---
name: Scheduled
description: Sends scheduled Slack updates.
returns:
  - slack.message.send.requested
schedules:
  - cron: "* * * * *"
egress:
  - sink: slack
    event: slack.message.send.requested
    filter:
      channel_ids: ["C123"]
---
Send a scheduled update.
""",
    )
    runtime = zeta_scheduling.build_scheduler_services(
        project_root=tmp_path,
        plugin_resolver=PluginMap(_slack_plugin()),
    )

    try:
        events = zeta_scheduling.request_due_project_schedules(runtime)
    finally:
        runtime.close()

    assert [event.event_type for event in events] == ["agent.scheduled.scheduled"]


def test_zeta_agent_manifest_allows_unvalidated_runtime_vocabularies(
    tmp_path: Path,
) -> None:
    spec = zeta_agents.load_spec(
        _write_spec(
            tmp_path / "worker.md",
            """---
name: Worker
description: Does work.
accepts:
  - repo.requested
returns:
  - repo.completed
tools:
  - read
---
Handle {{ event.payload.title }}.
""",
        )
    )

    zeta_agents.Manifest().validate(spec)


def test_zeta_agent_prompt_validation_rejects_unknown_root(tmp_path: Path) -> None:
    spec = zeta_agents.load_spec(
        _write_spec(
            tmp_path / "worker.md",
            """---
name: Worker
description: Does work.
---
{{ payload.text }}
""",
        )
    )

    with pytest.raises(zeta_agents.TemplateError, match="unknown variable 'payload'"):
        zeta_agents.validate_prompt(spec)


def test_zeta_agent_spec_compiles_to_event_dispatch_agent(tmp_path: Path) -> None:
    spec = zeta_agents.load_spec(
        _write_spec(
            tmp_path / "slack-qa.md",
            """---
name: Slack Q&A
description: Answers workspace questions in Slack.
accepts:
  - slack.dm.received
tools:
  - Read
---
User asked: {{ event.payload.text }}
""",
        )
    )
    calls: list[dict[str, Any]] = []

    async def run_turn(
        objective: str,
        timeline: list[dict[str, Any]],
        config: AgentConfig,
        **kwargs: Any,
    ) -> AgentRunResult:
        calls.append(
            {
                "objective": objective,
                "timeline": timeline,
                "config": config,
                "kwargs": kwargs,
            }
        )
        return AgentRunResult(final_answer="done")

    compiled = zeta_agents.compile_agent_definition(spec, run_turn=run_turn)
    store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    dispatcher = zeta_dispatch.EventDispatcher(store, executors=[compiled])

    outcome = asyncio.run(
        dispatcher.publish_and_run(
            zeta_events.DraftEvent(
                "slack.dm.received",
                "test",
                {"text": "hello"},
                session_id="s1",
            )
        )
    )

    assert compiled.definition.agent_id == "slack-qa"
    assert compiled.definition.returns == ()
    assert len(calls) == 1
    assert calls[0]["objective"] == "User asked: hello"
    assert calls[0]["timeline"] == []
    assert calls[0]["config"].system_prompt == "Answers workspace questions in Slack."
    assert tuple(calls[0]["config"].allowed_capabilities or ()) == ("Read",)
    assert calls[0]["kwargs"]["caused_by"] == outcome.event.id
    assert zeta_queue.terminal_queue_item_result(
        outcome.lifecycle_events,
        event_id=outcome.event.id,
        target_agent="slack-qa",
    ) == {
        "final_answer": "done",
        "final_event_cursor": "6",
    }


def test_zeta_agent_with_returns_requires_event_registry(tmp_path: Path) -> None:
    spec = _slack_return_agent_spec(tmp_path)

    with pytest.raises(ValueError, match="returns require an event registry"):
        zeta_agents.compile_agent_definition(spec)


def test_zeta_agent_with_returns_publishes_structured_return_event(
    tmp_path: Path,
) -> None:
    spec = _slack_return_agent_spec(tmp_path)
    events = _slack_return_event_registry()
    run_calls: list[dict[str, Any]] = []
    structured_calls: list[dict[str, Any]] = []

    compiled = zeta_agents.compile_agent_definition(
        spec,
        event_registry=events,
        run_turn=_recording_return_run(run_calls),
        structured_output=_recording_structured_return(structured_calls),
    )
    store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    dispatcher = zeta_dispatch.EventDispatcher(store, executors=[compiled])

    outcome = asyncio.run(
        dispatcher.publish_and_run(
            zeta_events.DraftEvent(
                "slack.dm.received",
                "test",
                {"text": "hello"},
                session_id="s1",
            )
        )
    )
    returned = store.list_events(
        zeta_events.Filter(event_type="message.delivery.requested")
    )
    terminal = zeta_queue.terminal_queue_item_result(
        outcome.lifecycle_events,
        event_id=outcome.event.id,
        target_agent="slack-qa",
    )

    _assert_return_run_called(run_calls)
    _assert_structured_return_called(structured_calls, spec, events)
    _assert_message_delivery_event(returned)
    _assert_terminal_return_event(terminal)


def test_zeta_resumable_agent_uses_stable_session_id() -> None:
    definition = AgentDefinition(
        "slack-qa",
        (EventPattern("slack.dm.received"),),
        dispatch_mode="session_scoped",
    )
    first = Event(
        id="evt_first",
        event_type="slack.dm.received",
        source="test",
        payload={},
        idempotency_key=None,
        caused_by=None,
        session_id=None,
        run_id=None,
        turn_id=None,
        timestamp_ms=1,
        cursor=1,
    )
    second = Event(
        id="evt_second",
        event_type="slack.dm.received",
        source="test",
        payload={},
        idempotency_key=None,
        caused_by=None,
        session_id=None,
        run_id=None,
        turn_id=None,
        timestamp_ms=2,
        cursor=2,
    )

    assert agent_session_id(definition, first) == "agent/slack-qa"
    assert agent_session_id(definition, second) == "agent/slack-qa"


def test_zeta_one_shot_agent_uses_trigger_event_session_id() -> None:
    definition = AgentDefinition(
        "slack-qa",
        (EventPattern("slack.dm.received"),),
        dispatch_mode="one_shot",
    )
    event = Event(
        id="evt_first",
        event_type="slack.dm.received",
        source="test",
        payload={},
        idempotency_key=None,
        caused_by=None,
        session_id=None,
        run_id=None,
        turn_id=None,
        timestamp_ms=1,
        cursor=1,
    )

    assert agent_session_id(definition, event) == "agent/slack-qa/evt_first"


def test_zeta_disabled_agent_spec_does_not_compile_for_runtime(
    tmp_path: Path,
) -> None:
    spec = zeta_agents.load_spec(
        _write_spec(
            tmp_path / "disabled.md",
            """---
name: Disabled
description: Disabled agent.
enabled: false
accepts:
  - slack.dm.received
---
Ignore this.
""",
        )
    )

    assert zeta_agents.compile_agent_definitions(spec) == []
    with pytest.raises(ValueError, match="requires an enabled agent"):
        zeta_agents.compile_agent_definition(spec)


def test_zeta_agent_spec_compiles_declared_runtime_locks(tmp_path: Path) -> None:
    spec = zeta_agents.load_spec(
        _write_spec(
            tmp_path / "triage.md",
            """---
name: Triage
description: Triage issues.
accepts:
  - github.issue.opened
locks:
  - context:repo
  - branch:main
---
Triage the issue.
""",
        )
    )

    compiled = zeta_agents.compile_agent_definition(spec)

    assert compiled.definition.lock_keys == ("context:repo", "branch:main")
