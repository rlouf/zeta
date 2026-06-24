"""Authored agent spec tests."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from zeta.agents.events import EventRegistry
from zeta.agents.loader import load_spec
from zeta.agents.manifest import Manifest, ManifestError
from zeta.agents.prompts import TemplateError, render_prompt, validate_prompt
from zeta.agents.returns import derive_returns_schema
from zeta.agents.spec import ScheduleEntry, matches
from zeta.capabilities.execution import (
    InProcessCapabilityExecutor,
)
from zeta.capabilities.registry import CapabilityRegistry, RegisteredCapability
from zeta.capabilities.types import (
    Capability,
    CapabilityId,
)
from zeta.orchestration import dispatch as zeta_dispatch
from zeta.orchestration import queue as zeta_queue
from zeta.orchestration.agents import (
    compile_agent_definition,
    compile_agent_definitions,
)
from zeta.records.events import DraftEvent
from zeta.records.stores import SqliteEventStore
from zeta.run.config import AgentConfig
from zeta.run.runtime import AgentRunResult

zeta_agents = SimpleNamespace(
    EventRegistry=EventRegistry,
    Manifest=Manifest,
    ManifestError=ManifestError,
    ScheduleEntry=ScheduleEntry,
    TemplateError=TemplateError,
    compile_agent_definition=compile_agent_definition,
    compile_agent_definitions=compile_agent_definitions,
    derive_returns_schema=derive_returns_schema,
    load_spec=load_spec,
    matches=matches,
    render_prompt=render_prompt,
    validate_prompt=validate_prompt,
)
zeta_events = SimpleNamespace(DraftEvent=DraftEvent, SqliteEventStore=SqliteEventStore)


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


def test_zeta_agent_spec_loads_frontmatter_body_and_extensions(tmp_path: Path) -> None:
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
    event: slack.dm.received
    payload:
      text: scheduled
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
    assert spec.accepts == ("slack.dm.received",)
    assert spec.returns == ("message.delivery.requested",)
    assert spec.tools == ("read",)
    assert spec.schedules == (
        zeta_agents.ScheduleEntry(
            cron="* * * * *",
            event="slack.dm.received",
            payload={"text": "scheduled"},
            timezone=None,
        ),
    )
    assert spec.extensions == {"writes": {"paths": ["docs/**.md"]}}
    assert spec.instructions == "User asked: {{ event.payload.text }}\n"
    assert len(spec.sha256) == 64


def test_zeta_agent_spec_defaults_schedule_event_to_runtime_trigger(
    tmp_path: Path,
) -> None:
    spec = zeta_agents.load_spec(
        _write_spec(
            tmp_path / "scheduled.md",
            """---
name: Scheduled
description: Runs on a schedule.
accepts:
  - runtime.schedule.triggered
schedules:
  - cron: "* * * * *"
---
Summarize the repo.
""",
        )
    )

    assert spec.schedules == (
        zeta_agents.ScheduleEntry(
            cron="* * * * *",
            event="runtime.schedule.triggered",
            payload={},
            timezone=None,
        ),
    )


@pytest.mark.parametrize(
    ("frontmatter", "message"),
    [
        ("name: 1\ndescription: Worker\n", "name"),
        ("name: Worker\ndescription: 1\n", "description"),
        ("name: Worker\ndescription: Worker\nenabled: maybe\n", "enabled"),
        ("name: Worker\ndescription: Worker\nresumable: later\n", "resumable"),
        ("name: Worker\ndescription: Worker\naccepts: github.issue.opened\n", "accepts"),
        ("name: Worker\ndescription: Worker\nreturns:\n  - 1\n", "returns"),
        ("name: Worker\ndescription: Worker\ntools:\n  - read\n  - 2\n", "tools"),
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
returns:
  - message.delivery.requested
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
    assert compiled.definition.returns == ("message.delivery.requested",)
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
