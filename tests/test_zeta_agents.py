"""Authored agent spec tests."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from zeta import dispatch as zeta_dispatch
from zeta.agents.capabilities import AgentConfig
from zeta.agents.events import EventEnvelope, EventRegistry
from zeta.agents.loader import load_spec
from zeta.agents.manifest import Manifest, ManifestError
from zeta.agents.prompts import TemplateError, render_prompt, validate_prompt
from zeta.agents.returns import derive_returns_schema
from zeta.agents.runtime import compile_agent_definition
from zeta.agents.spec import ScheduleEntry, matches
from zeta.capabilities.base import (
    Capability,
    CapabilityId,
    CapabilityPolicy,
    CapabilitySpec,
    InProcessCapabilityExecutor,
)
from zeta.capabilities.registry import CapabilityRegistry
from zeta.events import DraftEvent
from zeta.loop import AgentTurnResult
from zeta.store.events import SqliteEventStore

zeta_agents = SimpleNamespace(
    EventEnvelope=EventEnvelope,
    EventRegistry=EventRegistry,
    Manifest=Manifest,
    ManifestError=ManifestError,
    ScheduleEntry=ScheduleEntry,
    TemplateError=TemplateError,
    compile_agent_definition=compile_agent_definition,
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


def _read_capability() -> Capability:
    return Capability(
        CapabilitySpec(
            CapabilityId("host", "read"),
            "Read a file.",
            {"type": "object"},
            effects=("read",),
            aliases=("Read",),
        ),
        CapabilityPolicy(
            supports_staging=False,
            supports_direct=True,
            trust="host",
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
  - Read
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
    assert spec.tools == ("Read",)
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
  - Read
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
        zeta_agents.EventEnvelope(
            event_type="slack.dm.received",
            payload={"text": "why is this slow?"},
        ),
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

    def run_turn(
        objective: str,
        timeline: list[dict[str, Any]],
        config: AgentConfig,
        **kwargs: Any,
    ) -> AgentTurnResult:
        calls.append(
            {
                "objective": objective,
                "timeline": timeline,
                "config": config,
                "kwargs": kwargs,
            }
        )
        return AgentTurnResult(final_text="done")

    compiled = zeta_agents.compile_agent_definition(spec, run_turn=run_turn)
    store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    dispatcher = zeta_dispatch.EventDispatcher(store, agents=[compiled])

    outcome = dispatcher.dispatch(
        zeta_events.DraftEvent(
            "slack.dm.received",
            "test",
            {"text": "hello"},
            session_id="s1",
        )
    )

    assert compiled.agent_id == "slack-qa"
    assert len(calls) == 1
    assert calls[0]["objective"] == "User asked: hello"
    assert calls[0]["timeline"] == []
    assert calls[0]["config"].system_prompt == "Answers workspace questions in Slack."
    assert tuple(calls[0]["config"].allowed_capabilities or ()) == ("Read",)
    assert calls[0]["kwargs"]["caused_by"] == outcome.event.id
    assert outcome.agent_results == [{"final_text": "done", "final_event_cursor": "4"}]
