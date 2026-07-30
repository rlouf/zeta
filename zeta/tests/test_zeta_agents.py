"""Authored agent spec tests."""

import asyncio
import hashlib
import hmac
import json
import time
import tomllib
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from connectors import (
    EgressBinding,
    EventConnector,
    EventConnectorRegistry,
    InboundRequest,
    InboundResponse,
    IngressBinding,
)
from connectors.slack import (
    SLACK_MESSAGE_POST,
    SLACK_MESSAGE_RECEIVED,
    slack_event_connector,
)
from zeta.authoring.manifest import (
    Manifest,
    ManifestError,
    egress_bindings,
    ingress_bindings,
)
from zeta.authoring.prompts import TemplateError, render_prompt, validate_prompt
from zeta.authoring.resources import (
    ResourceError,
    enabled_event_connector_ids,
    event_connector_entry_points,
    load_agent_project,
    load_connector_registry,
    load_event_registry,
    load_skill_registry,
    validate_agent_project,
)
from zeta.authoring.returns import derive_returns_schema
from zeta.authoring.schemas import EventRegistry
from zeta.authoring.spec import (
    AgentSpec,
    ExecutorSpec,
    ModelSpec,
    ScheduleEntry,
    SpecError,
    load_spec,
    load_specs,
    matches,
    scheduled_event_type,
)
from zeta.capabilities.executors import (
    InProcessCapabilityExecutor,
    ToolExecutorProviderRegistry,
)
from zeta.capabilities.registry import CapabilityRegistry, RegisteredCapability
from zeta.capabilities.types import (
    Capability,
    CapabilityId,
)
from zeta.effects import DeliverySemantics
from zeta.events import DraftEvent, Event
from zeta.harness import connector_bridge as harness_connector_bridge
from zeta.harness import dispatch as harness_dispatch
from zeta.harness import queue as harness_queue
from zeta.harness import worker as harness_worker
from zeta.harness.routing import (
    AgentDefinition,
    EventPattern,
    compile_agent_definition,
    compile_agent_definitions,
    config_for_spec,
)
from zeta.harness.sessions import agent_session_id
from zeta.harness.store import RuntimeEventStore
from zeta.journal.store import Filter
from zeta.loop.runtime import AgentRunResult


def runtime_sqlite_event_store(path: Path) -> RuntimeEventStore:
    return RuntimeEventStore.open(path)


async def dispatch_and_drain(
    dispatcher: harness_dispatch.QueueingDispatcher,
    draft: DraftEvent,
) -> SimpleNamespace:
    outcome = await dispatcher.publish_event(draft)
    return SimpleNamespace(
        event=outcome.event,
        inserted=outcome.inserted,
        lifecycle_events=await dispatcher.drain(),
    )


zeta_agents = SimpleNamespace(
    EgressBinding=EgressBinding,
    EventConnector=EventConnector,
    EventConnectorRegistry=EventConnectorRegistry,
    EventRegistry=EventRegistry,
    InboundRequest=InboundRequest,
    InboundResponse=InboundResponse,
    IngressBinding=IngressBinding,
    Manifest=Manifest,
    ManifestError=ManifestError,
    ModelSpec=ModelSpec,
    ResourceError=ResourceError,
    SLACK_MESSAGE_POST=SLACK_MESSAGE_POST,
    SLACK_MESSAGE_RECEIVED=SLACK_MESSAGE_RECEIVED,
    ScheduleEntry=ScheduleEntry,
    SpecError=SpecError,
    TemplateError=TemplateError,
    compile_agent_definition=compile_agent_definition,
    compile_agent_definitions=compile_agent_definitions,
    config_for_spec=config_for_spec,
    derive_returns_schema=derive_returns_schema,
    enabled_event_connector_ids=enabled_event_connector_ids,
    event_connector_entry_points=event_connector_entry_points,
    load_connector_registry=load_connector_registry,
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
    slack_event_connector=slack_event_connector,
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


def _slack_connector(
    *,
    message_schema: dict[str, Any] | None = None,
    ingress_poller: Callable[..., Any] | None = None,
    push_ingress: Callable[..., Any] | None = None,
    egress_handler: Callable[..., Any] | None = None,
    egress_semantics: DeliverySemantics = "connector_deduplicated",
) -> EventConnector:
    ingress = {"slack.message.received": ingress_poller} if ingress_poller else {}
    egress = {"slack.message.post": egress_handler} if egress_handler else {}
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
    return zeta_agents.EventConnector(
        id="slack",
        events={
            "slack.message.received": message_schema
            or {
                "type": "object",
                "required": ["text"],
                "properties": {"text": {"type": "string"}},
                "additionalProperties": False,
            },
            "slack.message.post": {
                "type": "object",
                "required": ["channel_id", "text"],
                "properties": {
                    "channel_id": {"type": "string"},
                    "text": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        filters={
            "slack.message.received": ingress_filter_schema,
            "slack.message.post": egress_filter_schema,
        },
        ingress=ingress,
        push_ingress=push_ingress,
        egress=egress,
        egress_semantics=({"slack.message.post": egress_semantics} if egress else {}),
    )


def connector_registry(*connectors: EventConnector) -> EventConnectorRegistry:
    registry = EventConnectorRegistry()
    for connector in connectors:
        registry.register(connector)
    return registry


class FakeEntryPoint:
    def __init__(self, name: str, connector: EventConnector) -> None:
        self.name = name
        self.group = "zeta.event_connectors"
        self.connector = connector

    def load(self) -> Callable[[], EventConnector]:
        return lambda: self.connector


class FakeSlackClient:
    def __init__(
        self,
        *,
        post_result: dict[str, Any] | None = None,
        post_error: Exception | None = None,
    ) -> None:
        self.post_result = post_result or {"channel": "C123", "ts": "123.456"}
        self.post_error = post_error
        self.post_calls: list[dict[str, Any]] = []

    async def post_message(
        self,
        channel_id: str,
        text: str,
        *,
        thread_ts: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if self.post_error is not None:
            raise self.post_error
        self.post_calls.append(
            {
                "channel_id": channel_id,
                "text": text,
                "thread_ts": thread_ts,
                "idempotency_key": idempotency_key,
            }
        )
        return self.post_result


def _slack_return_agent_spec(tmp_path: Path) -> AgentSpec:
    return zeta_agents.load_spec(
        _write_spec(
            tmp_path / "slack-qa.md",
            """---
name: Slack Q&A
description: Answers workspace questions in Slack.
accepts:
  - slack.message.received
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
            "slack.message.received": {
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
) -> Any:
    async def agent_loop(
        invocation: Any,
        objective: str,
        timeline: list[dict[str, Any]],
        context: str,
        config: Any,
        session_id: str,
        run_id: str,
    ) -> AgentRunResult:
        calls.append(
            {
                "invocation": invocation,
                "objective": objective,
                "timeline": timeline,
                "context": context,
                "config": config,
                "session_id": session_id,
                "run_id": run_id,
            }
        )
        return AgentRunResult(final_answer="Send a reply to C1.")

    return agent_loop


def _recording_structured_return(
    calls: list[dict[str, Any]],
) -> Callable[..., Any]:
    def structured_output(
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        calls.append({"messages": messages, "kwargs": kwargs})
        return {
            "events": [
                {
                    "type": "message.delivery.requested",
                    "payload": {"channel_id": "C1", "text": "hello"},
                }
            ]
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
session: shared
model:
  name: qwen3.6-27b-q8-local
  url: http://127.0.0.1:8080/v1/chat/completions
accepts:
  - slack.message.received
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
    assert spec.session == "shared"
    assert spec.model == zeta_agents.ModelSpec(
        name="qwen3.6-27b-q8-local",
        url="http://127.0.0.1:8080/v1/chat/completions",
    )
    assert spec.accepts == ("slack.message.received", "agent.slack-qa.scheduled")
    assert spec.returns == ("message.delivery.requested",)
    assert spec.tools == ("read",)
    assert spec.skills == ()
    assert spec.schedules == (
        zeta_agents.ScheduleEntry(
            cron="* * * * *",
            timezone=None,
            catchup=None,
        ),
    )
    assert spec.manifest == {"writes": {"paths": ["docs/**.md"]}}
    assert spec.instructions == "User asked: {{ event.payload.text }}\n"
    assert len(spec.sha256) == 64


def test_zeta_agent_spec_selects_tool_executor_from_frontmatter(
    tmp_path: Path,
) -> None:
    spec = load_spec(
        _write_spec(
            tmp_path / "triage.md",
            """---
name: Triage
description: Triage issues.
executor:
  provider: modal
  config:
    app: zeta-tools
    options:
      retries: 3
      timeout: 1.5
      enabled: true
      regions:
        - eu-west
        - us-east
      fallback: null
accepts:
  - github.issue.opened
---
Triage the issue.
""",
        )
    )

    assert spec.executor == ExecutorSpec(
        provider="modal",
        config={
            "app": "zeta-tools",
            "options": {
                "retries": 3,
                "timeout": 1.5,
                "enabled": True,
                "regions": ["eu-west", "us-east"],
                "fallback": None,
            },
        },
    )
    assert "executor" not in spec.manifest


def test_zeta_agent_project_rejects_unknown_tool_executor_provider(
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    events_dir = agents_dir / "events"
    events_dir.mkdir(parents=True)
    (events_dir / "github.issue.opened.json").write_text(
        '{"type": "object"}',
        encoding="utf-8",
    )
    _write_spec(
        agents_dir / "triage.md",
        """---
name: Triage
description: Triage issues.
executor:
  provider: remote
accepts:
  - github.issue.opened
---
Triage the issue.
""",
    )

    project = load_agent_project(agents_dir)

    with pytest.raises(ManifestError, match="unknown tool executor 'remote'"):
        validate_agent_project(
            project,
            tool_executors=ToolExecutorProviderRegistry(),
        )


def test_zeta_agent_spec_parses_base_dir_as_absolute_path(tmp_path: Path) -> None:
    spec = load_spec(
        _write_spec(
            tmp_path / "filer.md",
            """---
name: Filer
description: Files notes into the vault.
base_dir: ~/vaults/CEO
accepts:
  - file.created
tools:
  - read
---
{{ event.payload.path }}
""",
        )
    )

    assert spec.base_dir == Path.home() / "vaults" / "CEO"
    assert "base_dir" not in spec.manifest


def test_zeta_agent_spec_defaults_base_dir_to_none(tmp_path: Path) -> None:
    spec = load_spec(
        _write_spec(
            tmp_path / "plain.md",
            """---
name: Plain
description: No base dir.
---
body
""",
        )
    )

    assert spec.base_dir is None


def test_zeta_agent_spec_rejects_relative_base_dir(tmp_path: Path) -> None:
    with pytest.raises(SpecError):
        load_spec(
            _write_spec(
                tmp_path / "filer.md",
                """---
name: Filer
description: Files notes.
base_dir: notes/vault
---
body
""",
            )
        )


def test_zeta_authored_agent_config_executes_tools_directly(tmp_path: Path) -> None:
    spec = zeta_agents.load_spec(
        _write_spec(
            tmp_path / "worker.md",
            """---
name: Worker
description: Runs directly.
model:
  name: qwen3.6-27b-q8-local
  url: http://127.0.0.1:8080/v1/chat/completions
tools:
  - bash
---
Run.
""",
        )
    )

    config = zeta_agents.config_for_spec(spec, None)

    assert config.execution_mode == "direct"
    assert config.model_name == "qwen3.6-27b-q8-local"
    assert config.model_url == "http://127.0.0.1:8080/v1/chat/completions"
    assert config.system_prompt == "Runs directly."
    assert config.allowed_capabilities == ("bash",)


def test_zeta_agent_spec_loads_inline_connector_bindings(
    tmp_path: Path,
) -> None:
    spec = zeta_agents.load_spec(
        _write_spec(
            tmp_path / "support.md",
            """---
name: Support
description: Replies to Slack support messages.
accepts:
  - event: slack.message.received
    filter:
      channel_ids: ["C123"]
    idempotency_key: "slack:message:{team_id}:{channel_id}:{message_ts}"
returns:
  - event: slack.message.post
    with:
      channel_ids: ["C123"]
---
Reply.
""",
        )
    )

    assert spec.manifest == {}
    assert spec.accepts == ("slack.message.received",)
    assert spec.returns == ("slack.message.post",)
    assert zeta_agents.ingress_bindings(spec) == (
        zeta_agents.IngressBinding(
            event="slack.message.received",
            filter={"channel_ids": ["C123"]},
            idempotency_key="slack:message:{team_id}:{channel_id}:{message_ts}",
        ),
    )
    assert zeta_agents.egress_bindings(spec) == (
        zeta_agents.EgressBinding(
            event="slack.message.post",
            options={"channel_ids": ["C123"]},
            idempotency_key=None,
        ),
    )


@pytest.mark.parametrize(
    ("frontmatter", "message"),
    [
        (
            """accepts:
  - filter:
      channel_ids: ["C123"]
""",
            "event is required",
        ),
        (
            """returns:
  - with:
      channel_ids: ["C123"]
""",
            "event is required",
        ),
        (
            """returns:
  - event: slack.message.post
    filter:
      channel_ids: ["C123"]
""",
            "use 'with' for returned event options",
        ),
    ],
)
def test_zeta_agent_spec_rejects_invalid_event_entries(
    tmp_path: Path,
    frontmatter: str,
    message: str,
) -> None:
    with pytest.raises(zeta_agents.SpecError, match=message):
        zeta_agents.load_spec(
            _write_spec(
                tmp_path / "support.md",
                f"""---
name: Support
description: Replies to Slack support messages.
{frontmatter}---
Reply.
""",
            )
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
            catchup=None,
        ),
    )
    assert zeta_agents.scheduled_event_type("scheduled") == "agent.scheduled.scheduled"


def test_zeta_agent_spec_parses_latest_schedule_catchup(tmp_path: Path) -> None:
    spec = zeta_agents.load_spec(
        _write_spec(
            tmp_path / "scheduled.md",
            """---
name: Scheduled
description: Runs on a schedule.
schedules:
  - cron: "0 18 * * 0"
    timezone: Europe/Paris
    catchup: latest
---
Summarize the repo.
""",
        )
    )

    assert spec.schedules == (
        zeta_agents.ScheduleEntry(
            cron="0 18 * * 0",
            timezone="Europe/Paris",
            catchup="latest",
        ),
    )


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
        ("name: Worker\ndescription: Worker\nsession: later\n", "session"),
        (
            "name: Worker\ndescription: Worker\naccepts: github.issue.opened\n",
            "accepts",
        ),
        ("name: Worker\ndescription: Worker\nreturns:\n  - 1\n", "returns"),
        ("name: Worker\ndescription: Worker\ntools:\n  - read\n  - 2\n", "tools"),
        ("name: Worker\ndescription: Worker\nskills:\n  - review\n  - 2\n", "skills"),
        ("name: Worker\ndescription: Worker\nexecutor: local\n", "executor"),
        (
            "name: Worker\ndescription: Worker\nexecutor:\n"
            "  provider: local\n  config: not-a-mapping\n",
            "config",
        ),
        (
            "name: Worker\ndescription: Worker\nexecutor:\n"
            "  provider: remote\n  config:\n    1: value\n",
            "config",
        ),
        (
            "name: Worker\ndescription: Worker\nexecutor:\n"
            "  provider: remote\n  config:\n    created: 2026-07-29\n",
            "config",
        ),
        (
            "name: Worker\ndescription: Worker\nexecutor:\n"
            "  provider: remote\n  config:\n    threshold: .nan\n",
            "config",
        ),
        (
            "name: Worker\ndescription: Worker\nexecutor:\n"
            "  provider: remote\n  config: &config\n"
            "    nested: *config\n",
            "config",
        ),
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
        (
            "name: Worker\ndescription: Worker\nschedules:\n"
            "  - cron: '* * * * *'\n    catchup: all\n",
            "catchup",
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
  - slack.message.received
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
            "slack.message.received": {
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
        {
            "event_type": "slack.message.received",
            "payload": {"text": "why is this slow?"},
        },
    )
    returns_schema = zeta_agents.derive_returns_schema(spec, events)

    assert zeta_agents.matches(spec, "slack.message.received")
    assert not zeta_agents.matches(spec, "slack.dm.sent")
    assert rendered == "User asked: why is this slow?"
    assert returns_schema == {
        "type": "object",
        "required": ["events"],
        "properties": {
            "events": {
                "type": "array",
                "items": {
                    "anyOf": [
                        {
                            "type": "object",
                            "required": ["type", "payload"],
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "const": "message.delivery.requested",
                                },
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
                        },
                    ]
                },
                "maxItems": 100,
            }
        },
        "additionalProperties": False,
    }


def test_zeta_agent_return_schema_hoists_payload_local_definitions(
    tmp_path: Path,
) -> None:
    spec = zeta_agents.load_spec(
        _write_spec(
            tmp_path / "reply.md",
            """---
name: Reply
description: Replies with citations.
accepts:
  - message.received
returns:
  - message.replied
---
Reply.
""",
        )
    )
    events = zeta_agents.EventRegistry(
        {
            "message.replied": {
                "type": "object",
                "required": ["evidence"],
                "properties": {
                    "evidence": {
                        "type": "array",
                        "items": {"$ref": "#/$defs/citation"},
                    }
                },
                "$defs": {
                    "citation": {
                        "type": "object",
                        "required": ["ref"],
                        "properties": {"ref": {"type": "string"}},
                        "additionalProperties": False,
                    }
                },
                "additionalProperties": False,
            }
        }
    )

    schema = zeta_agents.derive_returns_schema(spec, events)

    assert schema is not None
    assert schema["properties"]["events"]["items"]["anyOf"][0]["properties"]["payload"][
        "properties"
    ]["evidence"]["items"] == {"$ref": "#/$defs/event_0_citation"}
    assert schema["$defs"]["event_0_citation"]["properties"] == {
        "ref": {"type": "string"}
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


def test_zeta_agent_project_reads_enabled_event_connector_ids(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "connectors.yaml").write_text(
        "event_connectors:\n  - slack\n  - github\n",
        encoding="utf-8",
    )

    assert zeta_agents.enabled_event_connector_ids(agents_dir) == ("slack", "github")


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("event_connectors: slack\n", "event_connectors"),
        ("event_connectors:\n  - slack\n  - 1\n", "event_connectors"),
        ("connectors:\n  - slack\n", "unsupported field 'connectors'"),
    ],
)
def test_zeta_agent_project_rejects_invalid_event_connector_config(
    tmp_path: Path,
    content: str,
    message: str,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "connectors.yaml").write_text(content, encoding="utf-8")

    with pytest.raises(zeta_agents.ResourceError, match=message):
        zeta_agents.enabled_event_connector_ids(agents_dir)


def test_zeta_event_connector_registry_registers_and_resolves_connectors() -> None:
    slack = _slack_connector()
    github = zeta_agents.EventConnector(
        id="github",
        events={"github.issue.opened": None},
    )
    registry = zeta_agents.EventConnectorRegistry()
    registry.register(slack)
    registry.register(github)

    assert registry.resolve("slack") == slack
    assert registry.resolve("github") == github
    assert registry.connector_for_event("slack.message.received") == slack
    assert registry.connector_for_event("github.issue.opened") == github
    assert registry.connector_for_event("missing.event") is None


def test_zeta_event_connector_registry_rejects_duplicate_ids() -> None:
    registry = zeta_agents.EventConnectorRegistry()
    registry.register(_slack_connector())

    with pytest.raises(ValueError, match="duplicate"):
        registry.register(_slack_connector())


def test_zeta_event_connector_requires_egress_delivery_semantics() -> None:
    with pytest.raises(ValueError, match="missing delivery semantics"):
        zeta_agents.EventConnector(
            id="unsafe",
            events={"message.post": None},
            egress={"message.post": lambda _event, _binding, _key: None},
        )


def test_zeta_event_connector_registry_lists_push_ingress_connectors() -> None:
    async def push(
        _request: InboundRequest,
    ) -> tuple[InboundResponse, tuple[DraftEvent, ...]]:
        return InboundResponse(status_code=202), ()

    slack = _slack_connector(push_ingress=push)
    github = zeta_agents.EventConnector(
        id="github",
        events={"github.issue.opened": None},
    )
    registry = connector_registry(slack, github)

    assert registry.push_ingress_connectors() == {"slack": slack}


def test_zeta_load_connector_registry_loads_only_enabled_entry_points(
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "connectors.yaml").write_text(
        "event_connectors:\n  - slack\n",
        encoding="utf-8",
    )
    slack = _slack_connector()
    github = zeta_agents.EventConnector(
        id="github",
        events={"github.issue.opened": None},
    )

    registry = zeta_agents.load_connector_registry(
        agents_dir,
        entry_points=(FakeEntryPoint("slack", slack), FakeEntryPoint("github", github)),
    )

    assert registry.resolve("slack") == slack
    assert registry.resolve("github") is None


def test_zeta_load_connector_registry_honors_process_allowlist(
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "connectors.yaml").write_text(
        "event_connectors:\n  - slack\n  - github\n",
        encoding="utf-8",
    )
    slack = _slack_connector()
    github = zeta_agents.EventConnector(
        id="github",
        events={"github.issue.opened": None},
    )

    registry = zeta_agents.load_connector_registry(
        agents_dir,
        entry_points=(FakeEntryPoint("slack", slack), FakeEntryPoint("github", github)),
        connector_names=("github",),
    )

    assert registry.resolve("slack") is None
    assert registry.resolve("github") == github


def test_zeta_slack_connector_is_discoverable_as_entry_point() -> None:
    metadata = tomllib.loads(Path("zeta/pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["project"]["entry-points"]["zeta.event_connectors"] == {
        "slack": "connectors.slack:slack_event_connector",
        "filesystem": "connectors.filesystem:filesystem_event_connector",
        "telegram": "connectors.telegram:telegram_event_connector",
    }


def test_zeta_slack_connector_maps_events_api_payload_to_received_events() -> None:
    connector = zeta_agents.slack_event_connector(FakeSlackClient())

    drafts = list(
        connector.ingress[zeta_agents.SLACK_MESSAGE_RECEIVED](
            zeta_agents.IngressBinding(
                event=zeta_agents.SLACK_MESSAGE_RECEIVED,
                filter={"channel_ids": ["C123"]},
            ),
            {
                "type": "event_callback",
                "event_id": "Ev1",
                "team_id": "T1",
                "event": {
                    "type": "message",
                    "channel": "C123",
                    "ts": "42.000",
                    "thread_ts": "40.000",
                    "user": "U1",
                    "text": "hello",
                },
            },
        )
    )

    assert len(drafts) == 1
    assert drafts[0].event_type == zeta_agents.SLACK_MESSAGE_RECEIVED
    assert drafts[0].source == "slack"
    assert drafts[0].session_id == "slack:T1:C123:40.000"
    assert drafts[0].idempotency_key == "slack:event:Ev1"
    assert drafts[0].payload == {
        "event_id": "Ev1",
        "team_id": "T1",
        "channel_id": "C123",
        "message_ts": "42.000",
        "thread_ts": "40.000",
        "user_id": "U1",
        "text": "hello",
    }


def test_zeta_slack_connector_posts_message_events() -> None:
    client = FakeSlackClient(post_result={"channel": "C123", "ts": "43.000"})
    connector = zeta_agents.slack_event_connector(client)
    event = zeta_events.DraftEvent(
        zeta_agents.SLACK_MESSAGE_POST,
        "agent:support",
        {"channel_id": "C123", "text": "hello", "thread_ts": "40.000"},
    )

    result = connector.egress[zeta_agents.SLACK_MESSAGE_POST](
        Event.from_draft(event),
        zeta_agents.EgressBinding(
            event=zeta_agents.SLACK_MESSAGE_POST,
            options={"channel_ids": ["C123"]},
        ),
        "idem-1",
    )
    assert asyncio.iscoroutine(result)
    result = asyncio.run(result)

    assert client.post_calls == [
        {
            "channel_id": "C123",
            "text": "hello",
            "thread_ts": "40.000",
            "idempotency_key": "idem-1",
        }
    ]
    assert result == {
        "channel_id": "C123",
        "message_ts": "43.000",
        "provider_message_id": "C123:43.000",
    }


def test_zeta_slack_connector_filters_channels() -> None:
    connector = zeta_agents.slack_event_connector(FakeSlackClient())

    drafts = list(
        connector.ingress[zeta_agents.SLACK_MESSAGE_RECEIVED](
            zeta_agents.IngressBinding(
                event=zeta_agents.SLACK_MESSAGE_RECEIVED,
                filter={"channel_ids": ["C123"]},
            ),
            {
                "type": "event_callback",
                "event_id": "Ev1",
                "team_id": "T1",
                "event": {
                    "type": "message",
                    "channel": "C999",
                    "ts": "42.000",
                    "user": "U1",
                    "text": "wrong channel",
                },
            },
        )
    )

    assert drafts == []
    with pytest.raises(ValueError, match="not allowed"):
        asyncio.run(
            connector.egress[zeta_agents.SLACK_MESSAGE_POST](
                Event.from_draft(
                    zeta_events.DraftEvent(
                        zeta_agents.SLACK_MESSAGE_POST,
                        "agent:support",
                        {"channel_id": "C999", "text": "hello"},
                    )
                ),
                zeta_agents.EgressBinding(
                    event=zeta_agents.SLACK_MESSAGE_POST,
                    options={"channel_ids": ["C123"]},
                ),
                "idem-1",
            ),
        )


def signed_slack_request(
    payload: dict[str, Any],
    *,
    secret: str = "secret",
    signature: str | None = None,
    timestamp: str | None = None,
) -> InboundRequest:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    timestamp = timestamp or str(int(time.time()))
    digest = hmac.new(
        secret.encode("utf-8"),
        f"v0:{timestamp}:".encode() + body,
        hashlib.sha256,
    ).hexdigest()
    return InboundRequest(
        method="POST",
        path="/connectors/slack",
        headers={
            "content-type": "application/json",
            "x-slack-request-timestamp": timestamp,
            "x-slack-signature": signature or f"v0={digest}",
        },
        query={},
        body=body,
    )


def test_zeta_slack_push_ingress_answers_url_verification() -> None:
    connector = zeta_agents.slack_event_connector(
        FakeSlackClient(),
        signing_secret="secret",
    )

    response, drafts = asyncio.run(
        connector.push_ingress(
            signed_slack_request(
                {
                    "type": "url_verification",
                    "challenge": "challenge-token",
                }
            )
        )
    )

    assert response.status_code == 200
    assert response.body == b"challenge-token"
    assert tuple(drafts) == ()


def test_zeta_slack_push_ingress_rejects_invalid_signature() -> None:
    connector = zeta_agents.slack_event_connector(
        FakeSlackClient(),
        signing_secret="secret",
    )

    response, drafts = asyncio.run(
        connector.push_ingress(
            signed_slack_request(
                {"type": "event_callback"},
                signature="v0=bad",
            )
        )
    )

    assert response.status_code == 401
    assert response.body == b"invalid signature"
    assert tuple(drafts) == ()


def test_zeta_slack_push_ingress_rejects_stale_timestamp() -> None:
    connector = zeta_agents.slack_event_connector(
        FakeSlackClient(),
        signing_secret="secret",
    )
    stale = str(int(time.time()) - 60 * 60)

    response, drafts = asyncio.run(
        connector.push_ingress(
            signed_slack_request(
                {"type": "event_callback"},
                timestamp=stale,
            )
        )
    )

    assert response.status_code == 401
    assert response.body == b"invalid signature"
    assert tuple(drafts) == ()


def test_zeta_slack_push_ingress_maps_callback_to_received_event() -> None:
    connector = zeta_agents.slack_event_connector(
        FakeSlackClient(),
        signing_secret="secret",
    )

    response, drafts = asyncio.run(
        connector.push_ingress(
            signed_slack_request(
                {
                    "type": "event_callback",
                    "event_id": "Ev1",
                    "team_id": "T1",
                    "event": {
                        "type": "app_mention",
                        "channel": "C123",
                        "ts": "42.000",
                        "thread_ts": "40.000",
                        "user": "U1",
                        "text": "hello",
                    },
                }
            )
        )
    )

    drafts = tuple(drafts)
    assert response.status_code == 202
    assert len(drafts) == 1
    assert drafts[0].event_type == zeta_agents.SLACK_MESSAGE_RECEIVED
    assert drafts[0].idempotency_key == "slack:event:Ev1"
    assert drafts[0].session_id == "slack:T1:C123:40.000"


def test_zeta_slack_push_ingress_ignores_unsupported_callbacks() -> None:
    connector = zeta_agents.slack_event_connector(
        FakeSlackClient(),
        signing_secret="secret",
    )

    response, drafts = asyncio.run(
        connector.push_ingress(
            signed_slack_request(
                {
                    "type": "event_callback",
                    "event_id": "Ev1",
                    "team_id": "T1",
                    "event": {"type": "reaction_added"},
                }
            )
        )
    )

    assert response.status_code == 202
    assert response.body == b"ignored"
    assert tuple(drafts) == ()


def test_zeta_agent_project_uses_enabled_event_connector_entry_points(
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "connectors.yaml").write_text(
        "event_connectors:\n  - slack\n",
        encoding="utf-8",
    )
    _write_spec(
        agents_dir / "support.md",
        """---
name: Support
description: Replies to Slack support messages.
accepts:
  - event: slack.message.received
    filter:
      channel_ids: ["C123"]
    idempotency_key: "slack:message:{team_id}:{channel_id}:{message_ts}"
---
Reply.
""",
    )

    registry = zeta_agents.load_connector_registry(
        agents_dir,
        entry_points=(FakeEntryPoint("slack", _slack_connector()),),
    )
    project = zeta_agents.load_agent_project(
        agents_dir,
        registry=registry,
    )

    zeta_agents.validate_agent_project(project)
    assert project.events.knows("slack.message.received")


def test_zeta_agent_project_merges_connector_event_schemas(
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
  - event: slack.message.received
    filter:
      channel_ids: ["C123"]
    idempotency_key: "slack:message:{team_id}:{channel_id}:{message_ts}"
returns:
  - event: slack.message.post
    with:
      channel_ids: ["C123"]
---
Reply.
""",
    )

    project = zeta_agents.load_agent_project(
        agents_dir,
        registry=connector_registry(_slack_connector()),
    )
    zeta_agents.validate_agent_project(project)

    assert project.events.knows("slack.message.received")
    assert project.events.knows("slack.message.post")
    assert zeta_agents.ingress_bindings(project.specs[0])[0].event == (
        "slack.message.received"
    )
    assert (
        zeta_agents.egress_bindings(project.specs[0])[0].event == "slack.message.post"
    )


def test_zeta_agent_project_rejects_conflicting_connector_event_schema(
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
  - event: slack.message.received
    filter:
      channel_ids: ["C123"]
    idempotency_key: "slack:message:{team_id}:{channel_id}:{message_ts}"
---
Reply.
""",
    )
    (events_dir / "slack.message.received.json").write_text(
        '{"type":"object","required":["body"],"properties":{"body":{"type":"string"}}}',
        encoding="utf-8",
    )

    with pytest.raises(zeta_agents.ResourceError, match="conflicts"):
        zeta_agents.load_agent_project(
            agents_dir,
            registry=connector_registry(_slack_connector()),
        )


def test_zeta_agent_project_accepts_identical_local_connector_event_schema(
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
  - event: slack.message.received
    filter:
      channel_ids: ["C123"]
    idempotency_key: "slack:message:{team_id}:{channel_id}:{message_ts}"
---
Reply.
""",
    )
    (events_dir / "slack.message.received.json").write_text(
        '{"type":"object","required":["text"],"properties":{"text":{"type":"string"}},"additionalProperties":false}',
        encoding="utf-8",
    )

    project = zeta_agents.load_agent_project(
        agents_dir,
        registry=connector_registry(_slack_connector(message_schema=schema)),
    )

    assert project.events.schema("slack.message.received") == schema


def test_zeta_agent_project_rejects_unknown_manifest_section(
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_spec(
        agents_dir / "worker.md",
        """---
name: Worker
description: Uses an unknown connector manifest section.
mode: strict
---
Run.
""",
    )
    project = zeta_agents.load_agent_project(agents_dir)

    with pytest.raises(zeta_agents.ManifestError, match="unknown manifest section"):
        zeta_agents.validate_agent_project(project)


@pytest.mark.parametrize(
    ("frontmatter", "message"),
    [
        (
            """accepts:
  - event: missing.event
    idempotency_key: "k"
""",
            "unknown ingress event 'missing.event'",
        ),
        (
            """accepts:
  - event: slack.channel.joined
    idempotency_key: "k"
""",
            "unknown ingress event 'slack.channel.joined'",
        ),
        (
            """accepts:
  - event: slack.message.received
    filter:
      channel_ids: ["C123"]
""",
            "requires idempotency_key",
        ),
        (
            """accepts:
  - event: slack.message.received
    filter:
      channel_ids: [1]
    idempotency_key: "k"
""",
            "invalid ingress filter",
        ),
        (
            """returns:
  - event: missing.event
""",
            "unknown egress event 'missing.event'",
        ),
        (
            """returns:
  - event: slack.message.delete
""",
            "unknown egress event 'slack.message.delete'",
        ),
        (
            """returns:
  - event: slack.message.post
    with:
      channel_ids: [1]
""",
            "invalid egress options",
        ),
    ],
)
def test_zeta_agent_project_validates_connector_bindings(
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
        registry=connector_registry(_slack_connector()),
    )

    with pytest.raises(zeta_agents.ManifestError, match=message):
        zeta_agents.validate_agent_project(project)


@pytest.mark.parametrize(
    "frontmatter",
    [
        """accepts:
  - slack.message.received
ingress:
  - event: slack.message.received
    idempotency_key: "k"
""",
        """returns:
  - slack.message.post
egress:
  - event: slack.message.post
""",
    ],
)
def test_zeta_agent_project_rejects_legacy_connector_sections(
    tmp_path: Path,
    frontmatter: str,
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
        registry=connector_registry(_slack_connector()),
    )

    with pytest.raises(zeta_agents.ManifestError, match="unknown manifest section"):
        zeta_agents.validate_agent_project(project)


def test_zeta_ingress_once_appends_connector_events(
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
  - event: slack.message.received
    filter:
      channel_ids: ["C123"]
    idempotency_key: "slack:message:{team_id}:{channel_id}:{message_ts}"
---
Reply.
""",
    )

    def poll_slack(
        binding: IngressBinding,
        _item: object | None = None,
    ) -> list[DraftEvent]:
        assert binding.filter == {"channel_ids": ["C123"]}
        return [
            zeta_events.DraftEvent(
                "slack.message.received",
                "slack",
                {
                    "team_id": "T1",
                    "channel_id": "C123",
                    "message_ts": "42",
                    "text": "hello",
                },
            )
        ]

    connector = _slack_connector(
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
    runtime = harness_worker.WorkerServices(
        project_root=tmp_path,
        state_dir=tmp_path / ".zeta",
        events=zeta_events.SqliteEventStore(tmp_path / "events.sqlite3"),
        registry=connector_registry(connector),
    )

    with asyncio.Runner() as runner:
        try:
            inserted = runner.run(harness_connector_bridge.run_ingress_once(runtime))
            events = runtime.events.list_events(
                zeta_events.Filter(event_type="slack.message.received")
            )
        finally:
            runner.run(runtime.aclose())

    assert inserted == 1
    assert len(events) == 1
    assert events[0].source == "slack"
    assert events[0].payload["text"] == "hello"
    assert events[0].idempotency_key == "slack:message:T1:C123:42"


def test_zeta_ingress_forever_continues_after_connector_failure(
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
  - event: slack.message.received
    filter:
      channel_ids: ["C123"]
    idempotency_key: "slack:message:{team_id}:{channel_id}:{message_ts}"
---
Reply.
""",
    )
    stop_event = asyncio.Event()
    calls = 0

    def poll_slack(
        _binding: IngressBinding,
        _item: object | None = None,
    ) -> list[DraftEvent]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("slack unavailable")
        stop_event.set()
        return [
            zeta_events.DraftEvent(
                "slack.message.received",
                "slack",
                {
                    "team_id": "T1",
                    "channel_id": "C123",
                    "message_ts": "42",
                    "text": "hello",
                },
            )
        ]

    connector = _slack_connector(
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
    runtime = harness_worker.WorkerServices(
        project_root=tmp_path,
        state_dir=tmp_path / ".zeta",
        events=zeta_events.SqliteEventStore(tmp_path / "events.sqlite3"),
        registry=connector_registry(connector),
    )

    with asyncio.Runner() as runner:
        try:
            runner.run(
                harness_worker.run_ingress_forever(
                    runtime,
                    poll_interval_seconds=0,
                    stop_event=stop_event,
                )
            )
            events = runtime.events.list_events(
                zeta_events.Filter(event_type="slack.message.received")
            )
        finally:
            runner.run(runtime.aclose())

    assert calls == 2
    assert [event.payload["text"] for event in events] == ["hello"]


def test_zeta_push_ingress_returns_404_for_unknown_connector(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    runtime = harness_worker.WorkerServices(
        project_root=tmp_path,
        state_dir=tmp_path / ".zeta",
        events=zeta_events.SqliteEventStore(tmp_path / "events.sqlite3"),
        registry=connector_registry(),
    )

    with asyncio.Runner() as runner:
        try:
            response = runner.run(
                harness_worker.handle_push_ingress_request(
                    runtime,
                    "missing",
                    InboundRequest("POST", "/connectors/missing", {}, {}, b"{}"),
                )
            )
            events = runtime.events.list_events(zeta_events.Filter())
        finally:
            runner.run(runtime.aclose())

    assert response.status_code == 404
    assert events == []


def test_zeta_push_ingress_returns_405_for_connector_without_push(
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    runtime = harness_worker.WorkerServices(
        project_root=tmp_path,
        state_dir=tmp_path / ".zeta",
        events=zeta_events.SqliteEventStore(tmp_path / "events.sqlite3"),
        registry=connector_registry(_slack_connector()),
    )

    with asyncio.Runner() as runner:
        try:
            response = runner.run(
                harness_worker.handle_push_ingress_request(
                    runtime,
                    "slack",
                    InboundRequest("POST", "/connectors/slack", {}, {}, b"{}"),
                )
            )
            events = runtime.events.list_events(zeta_events.Filter())
        finally:
            runner.run(runtime.aclose())

    assert response.status_code == 405
    assert events == []


def test_zeta_push_ingress_appends_returned_events(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()

    async def push(
        request: InboundRequest,
    ) -> tuple[InboundResponse, tuple[DraftEvent, ...]]:
        return (
            InboundResponse(status_code=202, body=b"accepted"),
            (
                zeta_events.DraftEvent(
                    "slack.message.received",
                    "slack",
                    {"text": request.path},
                    idempotency_key="push-1",
                ),
            ),
        )

    runtime = harness_worker.WorkerServices(
        project_root=tmp_path,
        state_dir=tmp_path / ".zeta",
        events=zeta_events.SqliteEventStore(tmp_path / "events.sqlite3"),
        registry=connector_registry(_slack_connector(push_ingress=push)),
    )

    with asyncio.Runner() as runner:
        try:
            response = runner.run(
                harness_worker.handle_push_ingress_request(
                    runtime,
                    "slack",
                    InboundRequest("POST", "/connectors/slack", {}, {}, b"{}"),
                )
            )
            events = runtime.events.list_events(
                zeta_events.Filter(event_type="slack.message.received")
            )
        finally:
            runner.run(runtime.aclose())

    assert response.status_code == 202
    assert response.body == b"accepted"
    assert len(events) == 1
    assert events[0].payload == {"text": "/connectors/slack"}


def test_zeta_push_ingress_is_idempotent_for_duplicate_events(
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()

    async def push(
        _request: InboundRequest,
    ) -> tuple[InboundResponse, tuple[DraftEvent, ...]]:
        return (
            InboundResponse(status_code=202),
            (
                zeta_events.DraftEvent(
                    "slack.message.received",
                    "slack",
                    {"text": "hello"},
                    idempotency_key="push-1",
                ),
            ),
        )

    runtime = harness_worker.WorkerServices(
        project_root=tmp_path,
        state_dir=tmp_path / ".zeta",
        events=zeta_events.SqliteEventStore(tmp_path / "events.sqlite3"),
        registry=connector_registry(_slack_connector(push_ingress=push)),
    )

    with asyncio.Runner() as runner:
        try:
            request = InboundRequest("POST", "/connectors/slack", {}, {}, b"{}")
            first = runner.run(
                harness_worker.handle_push_ingress_request(runtime, "slack", request)
            )
            second = runner.run(
                harness_worker.handle_push_ingress_request(runtime, "slack", request)
            )
            events = runtime.events.list_events(
                zeta_events.Filter(event_type="slack.message.received")
            )
        finally:
            runner.run(runtime.aclose())

    assert first.status_code == 202
    assert second.status_code == 202
    assert len(events) == 1


def test_zeta_push_ingress_validates_returned_event_payload(
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()

    async def push(
        _request: InboundRequest,
    ) -> tuple[InboundResponse, tuple[DraftEvent, ...]]:
        return (
            InboundResponse(status_code=202),
            (
                zeta_events.DraftEvent(
                    "slack.message.received",
                    "slack",
                    {"wrong": "shape"},
                    idempotency_key="push-1",
                ),
            ),
        )

    runtime = harness_worker.WorkerServices(
        project_root=tmp_path,
        state_dir=tmp_path / ".zeta",
        events=zeta_events.SqliteEventStore(tmp_path / "events.sqlite3"),
        registry=connector_registry(_slack_connector(push_ingress=push)),
    )

    with asyncio.Runner() as runner:
        try:
            with pytest.raises(Exception, match="required"):
                runner.run(
                    harness_worker.handle_push_ingress_request(
                        runtime,
                        "slack",
                        InboundRequest("POST", "/connectors/slack", {}, {}, b"{}"),
                    )
                )
            events = runtime.events.list_events(zeta_events.Filter())
        finally:
            runner.run(runtime.aclose())

    assert events == []


def test_zeta_worker_publishes_due_schedules(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_spec(
        agents_dir / "digest.md",
        """---
name: Digest
description: Emits a scheduled digest.
schedules:
  - cron: "* * * * *"
---
Summarize.
""",
    )
    runtime = harness_worker.WorkerServices(
        project_root=tmp_path,
        state_dir=tmp_path / ".zeta",
        events=zeta_events.SqliteEventStore(tmp_path / "events.sqlite3"),
    )

    try:
        published = harness_worker.publish_due_schedules(runtime)
        stored = runtime.events.list_events(
            zeta_events.Filter(event_type="agent.digest.scheduled")
        )
    finally:
        asyncio.run(runtime.aclose())

    assert [event.event_type for event in published] == ["agent.digest.scheduled"]
    assert len(stored) == 1


def test_zeta_run_once_fires_due_schedule(tmp_path: Path, monkeypatch) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_spec(
        agents_dir / "digest.md",
        """---
name: Digest
description: Emits a scheduled digest.
schedules:
  - cron: "* * * * *"
---
Summarize.
""",
    )
    runtime = harness_worker.WorkerServices(
        project_root=tmp_path,
        state_dir=tmp_path / ".zeta",
        events=zeta_events.SqliteEventStore(tmp_path / "events.sqlite3"),
    )

    async def _no_work(*_args: object, **_kwargs: object) -> str:
        return "queue empty"

    monkeypatch.setattr(harness_worker, "run_available_queue_item", _no_work)

    with asyncio.Runner() as runner:
        try:
            runner.run(harness_worker.run_once(runtime))
            scheduled = runtime.events.list_events(
                zeta_events.Filter(event_type="agent.digest.scheduled")
            )
            enqueued = runtime.events.event_has_queue_item(scheduled[0].id)
        finally:
            runner.run(runtime.aclose())

    assert len(scheduled) == 1
    assert enqueued is True


def test_zeta_resolve_state_dir_explicit_and_environment_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zeta.journal.sqlite import resolve_state_dir

    working_dir = tmp_path / "working"
    working_dir.mkdir()
    monkeypatch.chdir(working_dir)
    monkeypatch.setenv("ZETA_STATE_DIR", "environment")

    assert resolve_state_dir(Path("explicit")) == working_dir / "explicit"
    assert resolve_state_dir() == working_dir / "environment"

    monkeypatch.setenv("ZETA_STATE_DIR", "")
    assert resolve_state_dir() == working_dir / ".zeta"


def test_zeta_resolve_state_dir_finds_nearest_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zeta.journal.sqlite import resolve_state_dir

    project = tmp_path / "project"
    child = project / "src" / "package"
    marker = project / ".zeta"
    child.mkdir(parents=True)
    marker.mkdir()
    monkeypatch.delenv("ZETA_STATE_DIR", raising=False)

    assert resolve_state_dir(start=child) == marker


def test_zeta_resolve_state_dir_fallback_does_not_create_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zeta.journal.sqlite import resolve_state_dir

    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.delenv("ZETA_STATE_DIR", raising=False)

    assert resolve_state_dir(start=project) == project / ".zeta"
    assert not (project / ".zeta").exists()


def test_zeta_resolve_state_dir_excludes_home_marker_for_descendants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zeta.journal.sqlite import resolve_state_dir

    home = tmp_path / "home"
    project = home / "projects" / "zeta"
    project.mkdir(parents=True)
    (home / ".zeta").mkdir()
    monkeypatch.delenv("ZETA_STATE_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))

    assert resolve_state_dir(start=project) == project / ".zeta"
    assert resolve_state_dir(start=home) == home / ".zeta"


def test_zeta_resolve_state_dir_accepts_directory_symlink_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zeta.journal.sqlite import resolve_state_dir

    project = tmp_path / "project"
    state = tmp_path / "state"
    project.mkdir()
    state.mkdir()
    (project / ".zeta").symlink_to(state, target_is_directory=True)
    monkeypatch.delenv("ZETA_STATE_DIR", raising=False)

    assert resolve_state_dir(start=project) == project / ".zeta"


@pytest.mark.parametrize("broken_symlink", [False, True])
def test_zeta_resolve_state_dir_rejects_invalid_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    broken_symlink: bool,
) -> None:
    from zeta.journal.sqlite import resolve_state_dir

    project = tmp_path / "project"
    project.mkdir()
    marker = project / ".zeta"
    if broken_symlink:
        marker.symlink_to(tmp_path / "missing", target_is_directory=True)
    else:
        marker.write_text("not a directory", encoding="utf-8")
    monkeypatch.delenv("ZETA_STATE_DIR", raising=False)

    with pytest.raises(NotADirectoryError, match=r"\.zeta"):
        resolve_state_dir(start=project)


def test_zeta_resolve_state_dir_treats_current_directory_spellings_equally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zeta.journal.sqlite import resolve_state_dir

    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.delenv("ZETA_STATE_DIR", raising=False)

    expected = project / ".zeta"
    assert resolve_state_dir() == expected
    assert resolve_state_dir(start=Path(".")) == expected
    assert resolve_state_dir(start=Path("./")) == expected
    assert resolve_state_dir(start=project.resolve()) == expected


def test_zeta_implicit_store_and_session_paths_use_cwd_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zeta.journal.sqlite import event_store_path, zeta_sqlite_path
    from zeta.loop.runtime_context import default_session
    from zeta.models.profiles import profile_session_dir, user_models_config_path
    from zeta.substrate import SqliteObjectStore

    home = tmp_path / "home"
    project = home / "projects" / "zeta"
    project.mkdir(parents=True)
    monkeypatch.chdir(project)
    monkeypatch.delenv("ZETA_STATE_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))

    state_dir = project / ".zeta"
    assert event_store_path() == state_dir / "zeta.sqlite3"
    assert zeta_sqlite_path() == state_dir / "zeta.sqlite3"
    assert profile_session_dir() == state_dir / "sessions" / "default"
    assert user_models_config_path() == home / ".zeta" / "models.toml"

    session = default_session()
    trace_store = cast(SqliteObjectStore, session.trace_store)
    try:
        assert session.state_dir == state_dir
        assert session.session_dir == state_dir / "sessions" / "default"
    finally:
        trace_store.close()
        session.event_sink.close()


def test_zeta_run_until_idle_drains_queue(monkeypatch) -> None:
    messages = iter(["ran qi_1", "ran qi_2", "queue empty"])

    async def _next(_runtime: object) -> str:
        return next(messages)

    monkeypatch.setattr(harness_worker, "run_once", _next)

    runtime = cast(harness_worker.WorkerServices, object())
    result = asyncio.run(harness_worker.run_until_idle(runtime))

    assert result == "processed 2"


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
        agent_loop=_recording_return_run(run_calls),
        structured_output=_recording_structured_return(structured_calls),
    )
    store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    dispatcher = harness_dispatch.QueueingDispatcher(store, executors=[compiled])

    outcome = asyncio.run(
        dispatch_and_drain(
            dispatcher,
            zeta_events.DraftEvent(
                "slack.message.received",
                "test",
                {"text": "hello"},
                session_id="s1",
            ),
        )
    )
    returned = store.list_events(
        zeta_events.Filter(event_type="message.delivery.requested")
    )
    terminal = harness_queue.terminal_queue_item_result(
        outcome.lifecycle_events,
        event_id=outcome.event.id,
        target_agent="slack-qa",
    )

    _assert_return_run_called(run_calls)
    _assert_structured_return_called(structured_calls, spec, events)
    _assert_message_delivery_event(returned)
    _assert_terminal_return_event(terminal)


def test_zeta_agent_with_returns_publishes_multiple_ordered_events(
    tmp_path: Path,
) -> None:
    spec = _slack_return_agent_spec(tmp_path)
    events = _slack_return_event_registry()

    def structured_output(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "events": [
                {
                    "type": "message.delivery.requested",
                    "payload": {"channel_id": "C1", "text": "first"},
                },
                {
                    "type": "message.delivery.requested",
                    "payload": {"channel_id": "C2", "text": "second"},
                },
            ]
        }

    compiled = zeta_agents.compile_agent_definition(
        spec,
        event_registry=events,
        agent_loop=_recording_return_run([]),
        structured_output=structured_output,
    )
    store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    dispatcher = harness_dispatch.QueueingDispatcher(store, executors=[compiled])

    outcome = asyncio.run(
        dispatch_and_drain(
            dispatcher,
            zeta_events.DraftEvent(
                "slack.message.received",
                "test",
                {"text": "hello"},
            ),
        )
    )
    returned = store.list_events(
        zeta_events.Filter(event_type="message.delivery.requested")
    )
    terminal = harness_queue.terminal_queue_item_result(
        outcome.lifecycle_events,
        event_id=outcome.event.id,
        target_agent="slack-qa",
    )

    assert [event.payload["text"] for event in returned] == ["first", "second"]
    assert returned[0].idempotency_key == (f"agent.return:{outcome.event.id}:slack-qa")
    assert returned[1].idempotency_key == (
        f"agent.return:{outcome.event.id}:slack-qa:1"
    )
    assert terminal is not None
    assert [event["text"] for event in terminal["returned_events"]] == [
        "first",
        "second",
    ]


def test_zeta_agent_with_returns_may_publish_no_events(tmp_path: Path) -> None:
    spec = _slack_return_agent_spec(tmp_path)
    events = _slack_return_event_registry()

    compiled = zeta_agents.compile_agent_definition(
        spec,
        event_registry=events,
        agent_loop=_recording_return_run([]),
        structured_output=lambda *_args, **_kwargs: {"events": []},
    )
    store = zeta_events.SqliteEventStore(tmp_path / "events.sqlite3")
    dispatcher = harness_dispatch.QueueingDispatcher(store, executors=[compiled])

    outcome = asyncio.run(
        dispatch_and_drain(
            dispatcher,
            zeta_events.DraftEvent(
                "slack.message.received",
                "test",
                {"text": "hello"},
            ),
        )
    )
    terminal = harness_queue.terminal_queue_item_result(
        outcome.lifecycle_events,
        event_id=outcome.event.id,
        target_agent="slack-qa",
    )

    assert (
        store.list_events(zeta_events.Filter(event_type="message.delivery.requested"))
        == []
    )
    assert terminal is not None
    assert terminal["returned_events"] == []


def test_zeta_shared_session_agent_uses_stable_session_id() -> None:
    definition = AgentDefinition(
        "slack-qa",
        (EventPattern("slack.message.received"),),
        session="shared",
    )
    first = Event(
        id="evt_first",
        event_type="slack.message.received",
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
        event_type="slack.message.received",
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
        (EventPattern("slack.message.received"),),
        session="per-event",
    )
    event = Event(
        id="evt_first",
        event_type="slack.message.received",
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


def test_zeta_agent_spec_session_defaults_to_per_event(tmp_path: Path) -> None:
    spec = zeta_agents.load_spec(
        _write_spec(
            tmp_path / "worker.md",
            """---
name: Worker
description: Worker agent.
accepts:
  - slack.message.received
---
Do the work.
""",
        )
    )

    assert spec.session == "per-event"


def test_zeta_agent_spec_accepts_a_session_template(tmp_path: Path) -> None:
    spec = zeta_agents.load_spec(
        _write_spec(
            tmp_path / "chat.md",
            """---
name: Chat
description: Chat agent.
session: "{chat_id}"
accepts:
  - telegram.message.received
---
Reply.
""",
        )
    )

    assert spec.session == "{chat_id}"


def _session_event(event_id: str, payload: dict[str, object]) -> Event:
    return Event(
        id=event_id,
        event_type="telegram.message.received",
        source="test",
        payload=payload,
        idempotency_key=None,
        caused_by=None,
        session_id=None,
        run_id=None,
        turn_id=None,
        timestamp_ms=1,
        cursor=1,
    )


def test_zeta_templated_session_scopes_the_timeline_per_payload_value() -> None:
    definition = AgentDefinition(
        "telegram-assistant",
        (EventPattern("telegram.message.received"),),
        session="{chat_id}",
    )
    alice = _session_event("evt_one", {"chat_id": 111})
    alice_again = _session_event("evt_two", {"chat_id": 111})
    bob = _session_event("evt_three", {"chat_id": 222})

    assert agent_session_id(definition, alice) == "agent/telegram-assistant/111"
    assert agent_session_id(definition, alice_again) == "agent/telegram-assistant/111"
    assert agent_session_id(definition, bob) == "agent/telegram-assistant/222"


def test_zeta_templated_session_may_combine_payload_fields() -> None:
    definition = AgentDefinition(
        "slack-qa",
        (EventPattern("telegram.message.received"),),
        session="{channel_id}:{thread_ts}",
    )
    event = _session_event("evt_one", {"channel_id": "C1", "thread_ts": "99.7"})

    assert agent_session_id(definition, event) == "agent/slack-qa/C1:99.7"


def test_zeta_templated_session_raises_when_the_field_is_absent() -> None:
    definition = AgentDefinition(
        "telegram-assistant",
        (EventPattern("telegram.message.received"),),
        session="{chat_id}",
    )
    event = _session_event("evt_one", {"text": "hello"})

    with pytest.raises(RuntimeError, match="session template"):
        agent_session_id(definition, event)


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
  - slack.message.received
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


_CONNECTOR_FACTORY_MODULE = """\
from connectors import EventConnector


def myfs_event_connector():
    return EventConnector(
        id="myfs",
        events={"myfs.file": {"type": "object", "additionalProperties": True}},
    )
"""

_CONNECTOR_INSTANCE_MODULE = """\
from connectors import EventConnector

connector = EventConnector(
    id="myinst",
    events={"myinst.file": {"type": "object", "additionalProperties": True}},
)
"""

_CONNECTOR_BAD_MODULE = "x = 1\n"


class _FakeEntryPoint:
    def __init__(self, name: str, connector: EventConnector) -> None:
        self.name = name
        self.group = "zeta.event_connectors"
        self._connector = connector

    def load(self) -> Callable[[], EventConnector]:
        return lambda: self._connector


def _write_connector_module(agents_dir: Path, filename: str, body: str) -> None:
    connectors_dir = agents_dir / "connectors"
    connectors_dir.mkdir(parents=True, exist_ok=True)
    (connectors_dir / filename).write_text(body, encoding="utf-8")


def test_zeta_directory_connector_factory_is_discovered(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    agents.mkdir()
    _write_connector_module(agents, "myfs.py", _CONNECTOR_FACTORY_MODULE)

    registry = load_connector_registry(agents)

    assert registry.resolve("myfs") is not None


def test_zeta_directory_connector_instance_is_discovered(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    agents.mkdir()
    _write_connector_module(agents, "inst.py", _CONNECTOR_INSTANCE_MODULE)

    registry = load_connector_registry(agents)

    assert registry.resolve("myinst") is not None


def test_zeta_directory_connector_without_connector_errors(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    agents.mkdir()
    _write_connector_module(agents, "bad.py", _CONNECTOR_BAD_MODULE)

    with pytest.raises(ResourceError, match="bad.py"):
        load_connector_registry(agents)


def test_zeta_entry_point_and_directory_connectors_load_together(
    tmp_path: Path,
) -> None:
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "connectors.yaml").write_text(
        "event_connectors:\n  - ep\n", encoding="utf-8"
    )
    _write_connector_module(agents, "myfs.py", _CONNECTOR_FACTORY_MODULE)
    ep_connector = EventConnector(
        id="ep",
        events={"ep.file": {"type": "object", "additionalProperties": True}},
    )

    registry = load_connector_registry(
        agents, entry_points=[_FakeEntryPoint("ep", ep_connector)]
    )

    assert registry.resolve("ep") is not None
    assert registry.resolve("myfs") is not None


_CONNECTOR_DATACLASS_MODULE = """\
from dataclasses import dataclass

from connectors import EventConnector


@dataclass
class _Config:
    value: int = 1


def dc_event_connector():
    _Config()
    return EventConnector(
        id="dc",
        events={"dc.file": {"type": "object", "additionalProperties": True}},
    )
"""


def test_zeta_directory_connector_with_dataclass_is_discovered(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    agents.mkdir()
    _write_connector_module(agents, "dc.py", _CONNECTOR_DATACLASS_MODULE)

    registry = load_connector_registry(agents)

    assert registry.resolve("dc") is not None
