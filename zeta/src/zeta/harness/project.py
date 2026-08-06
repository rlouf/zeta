"""Immutable compiled-project generation manifests."""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from connectors import EventConnector, EventConnectorRegistry

from zeta._version import __version__
from zeta.authoring.resources import (
    AgentProject,
    SkillRegistry,
    SkillResource,
    load_agent_project,
    validate_agent_project,
)
from zeta.authoring.schemas import EventRegistry
from zeta.authoring.spec import (
    AgentSpec,
    ExecutorSpec,
    ModelSpec,
    RetrySpec,
    ScheduleEntry,
    executor_config,
)
from zeta.capabilities.executors import (
    ToolExecutorProviderRegistry,
    tool_executor_providers_with_local,
)
from zeta.capabilities.registry import (
    AgentToolDefinition,
    AgentToolDefinitionError,
    CapabilityRegistry,
    agent_tool_definition_from_content,
    load_agent_tool_definition,
    validate_agent_tool_definition,
)
from zeta.context.transforms import (
    content_node_from_object,
    content_revision_from_object,
)
from zeta.events import DraftEvent, Event
from zeta.harness.store import RuntimeEventStore
from zeta.journal.store import Filter
from zeta.models.profiles import ModelSelection
from zeta.substrate import Store

PROJECT_SNAPSHOT_RECORDED = "runtime.project_snapshot.recorded"
PROJECT_SNAPSHOT_SCHEMA = "zeta.project_snapshot"
PROJECT_SNAPSHOT_VERSION = 4
EXECUTION_MANIFEST_SCHEMA = "zeta.execution_manifest"
EXECUTION_MANIFEST_VERSION = 3


class ProjectSnapshotUnavailable(RuntimeError):
    """Raised when a recorded project generation cannot be executed safely."""


@dataclass(frozen=True)
class ProjectSnapshot:
    """One validated authored project and its content-addressed manifest."""

    generation_id: str
    project: AgentProject
    manifest: dict[str, Any]
    tool_registry: CapabilityRegistry = field(
        default_factory=CapabilityRegistry,
        compare=False,
        repr=False,
    )

    def execution_manifest(self, spec: AgentSpec) -> dict[str, Any]:
        selected_skills = {
            name: self.manifest["skills"][name]
            for name in spec.skills
            if name in self.manifest["skills"]
        }
        selected_capabilities = {
            name: value
            for name, value in self.manifest["capabilities"].items()
            if name in spec.tools or value["name"] in spec.tools
        }
        relevant_events = {
            event_type: self.manifest["events"].get(event_type)
            for event_type in (*spec.accepts, *spec.publishes, *spec.returns)
        }
        manifest = {
            "schema": EXECUTION_MANIFEST_SCHEMA,
            "version": EXECUTION_MANIFEST_VERSION,
            "project_generation": self.generation_id,
            "agent": agent_manifest(spec),
            "events": relevant_events,
            "skills": selected_skills,
            "capabilities": selected_capabilities,
            "connectors": self.manifest["connectors"],
            "model": self.manifest["model"],
            "runtime_version": self.manifest["runtime_version"],
            "agent_tools": [
                item
                for item in self.manifest.get("agent_tools", [])
                if item.get("owner") == spec.slug
            ],
        }
        return {**manifest, "id": content_id("execution_manifest", manifest)}


def load_project_snapshot(
    agents_dir: Path,
    *,
    registry,
    tool_registry: CapabilityRegistry,
    model_selection: ModelSelection | None,
    tool_executors: ToolExecutorProviderRegistry | None = None,
    content_store: Store | None = None,
) -> ProjectSnapshot:
    project = load_agent_project(agents_dir, registry=registry)
    definitions = (
        *agent_file_tool_definitions(agents_dir, project),
        *agent_content_tool_definitions(project, content_store),
    )
    validate_agent_tool_counts(project, definitions)
    snapshot_registry = registry_with_agent_tools(tool_registry, definitions)
    project = project_with_agent_tool_grants(project, definitions)
    project = project_with_inherited_capabilities(project, snapshot_registry)
    validate_agent_project(
        project,
        tool_executors=tool_executor_providers_with_local(tool_executors),
    )
    manifest = project_manifest(
        project,
        tool_registry=snapshot_registry,
        model_selection=model_selection,
        agent_tools=definitions,
    )
    return ProjectSnapshot(
        generation_id=content_id("project", manifest),
        project=project,
        manifest=manifest,
        tool_registry=snapshot_registry,
    )


def record_project_snapshot(
    events: RuntimeEventStore,
    snapshot: ProjectSnapshot,
) -> Event:
    return events.accept(
        DraftEvent(
            PROJECT_SNAPSHOT_RECORDED,
            "zeta",
            {
                "generation_id": snapshot.generation_id,
                "manifest": snapshot.manifest,
            },
            idempotency_key=f"project_snapshot:{snapshot.generation_id}",
        )
    ).event


def load_recorded_project_snapshot(
    events: RuntimeEventStore,
    generation_id: str,
    *,
    registry: EventConnectorRegistry | None,
    tool_executors: ToolExecutorProviderRegistry | None = None,
    tool_registry: CapabilityRegistry | None = None,
) -> ProjectSnapshot:
    for event in events.list_events(Filter(event_type=PROJECT_SNAPSHOT_RECORDED)):
        if event.payload.get("generation_id") != generation_id:
            continue
        manifest = event.payload.get("manifest")
        if not isinstance(manifest, Mapping):
            break
        parsed_manifest = dict(manifest)
        if content_id("project", parsed_manifest) != generation_id:
            raise ProjectSnapshotUnavailable(
                f"recorded project snapshot {generation_id!r} failed verification"
            )
        project = project_from_manifest(parsed_manifest, registry=registry)
        definitions = agent_tools_from_manifest(parsed_manifest)
        snapshot_registry = registry_with_agent_tools(
            tool_registry or CapabilityRegistry(),
            definitions,
        )
        validate_agent_project(
            project,
            tool_executors=tool_executor_providers_with_local(tool_executors),
        )
        return ProjectSnapshot(
            generation_id,
            project,
            parsed_manifest,
            snapshot_registry,
        )
    raise ProjectSnapshotUnavailable(
        f"recorded project snapshot {generation_id!r} was not found"
    )


def project_manifest(
    project: AgentProject,
    *,
    tool_registry: CapabilityRegistry,
    model_selection: ModelSelection | None,
    agent_tools: tuple[AgentToolDefinition, ...] = (),
) -> dict[str, Any]:
    return {
        "schema": PROJECT_SNAPSHOT_SCHEMA,
        "version": PROJECT_SNAPSHOT_VERSION,
        "agents": [agent_manifest(spec) for spec in project.specs],
        "events": {event_type: schema for event_type, schema in project.events.items()},
        "skills": {
            name: {
                "sha256": skill.sha256,
                "body": skill.body,
                "path": str(skill.path),
            }
            for name, skill in sorted(project.skills.skills.items())
        },
        "connectors": [
            connector_manifest(connector)
            for connector in sorted(
                project.connectors.event_connectors(), key=lambda item: item.id
            )
        ],
        "capabilities": capability_manifests(tool_registry),
        "agent_tools": [asdict(definition) for definition in agent_tools],
        "model": model_manifest(model_selection),
        "runtime_version": __version__,
    }


def agent_content_tool_definitions(
    project: AgentProject,
    store: Store | None,
) -> tuple[AgentToolDefinition, ...]:
    if store is None:
        return ()
    definitions: list[AgentToolDefinition] = []
    for spec in project.specs:
        current = store.get_ref(f"agent/{spec.slug}/content/head")
        if current is None:
            continue
        revision = content_revision_from_object(store.get_object(current.object_id))
        if revision.owner != spec.slug:
            raise ProjectSnapshotUnavailable(
                f"agent content for {spec.slug!r} belongs to another owner"
            )
        for key in revision.projection_order:
            object_id = revision.nodes[key]
            node = content_node_from_object(store.get_object(object_id))
            if node.kind != "tool_definition":
                continue
            try:
                definitions.append(
                    agent_tool_definition_from_content(
                        node.content,
                        owner=spec.slug,
                        key=key,
                        object_id=object_id,
                    )
                )
            except AgentToolDefinitionError as exc:
                raise ProjectSnapshotUnavailable(str(exc)) from exc
    return tuple(definitions)


def agent_file_tool_definitions(
    agents_dir: Path,
    project: AgentProject,
) -> tuple[AgentToolDefinition, ...]:
    definitions: list[AgentToolDefinition] = []
    tools_dir = agents_dir / "tools"
    for spec in project.specs:
        owner_dir = tools_dir / spec.slug
        if not owner_dir.exists():
            continue
        if not owner_dir.is_dir():
            raise ProjectSnapshotUnavailable(
                f"agent tool path {owner_dir} is not a directory"
            )
        for path in sorted(owner_dir.glob("*.py")):
            if path.is_symlink() or not path.is_file():
                raise ProjectSnapshotUnavailable(
                    f"agent tool path {path} must be a regular file"
                )
            name = path.stem
            key = f"tools/{name}"
            capability_id = f"agent.{spec.slug}.{name}"
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise ProjectSnapshotUnavailable(
                    f"agent tool source {path} could not be read"
                ) from exc
            content = {
                "name": name,
                "capability_id": capability_id,
                "source": source,
            }
            object_id = content_id(
                "tool_definition",
                {"owner": spec.slug, "key": key, "content": content},
            )
            try:
                definitions.append(
                    validate_agent_tool_definition(
                        content,
                        owner=spec.slug,
                        key=key,
                        object_id=object_id,
                    )
                )
            except AgentToolDefinitionError as exc:
                raise ProjectSnapshotUnavailable(str(exc)) from exc
    return tuple(definitions)


def validate_agent_tool_counts(
    project: AgentProject,
    definitions: tuple[AgentToolDefinition, ...],
) -> None:
    for spec in project.specs:
        count = sum(definition.owner == spec.slug for definition in definitions)
        if count > 32:
            raise ProjectSnapshotUnavailable(
                f"agent {spec.slug!r} has too many authored tools"
            )


def registry_with_agent_tools(
    base: CapabilityRegistry,
    definitions: tuple[AgentToolDefinition, ...],
) -> CapabilityRegistry:
    if not definitions:
        return base
    registry = base.copy()
    for definition in definitions:
        try:
            registry.register(load_agent_tool_definition(definition))
        except (AgentToolDefinitionError, ValueError) as exc:
            raise ProjectSnapshotUnavailable(str(exc)) from exc
    return registry


def project_with_agent_tool_grants(
    project: AgentProject,
    definitions: tuple[AgentToolDefinition, ...],
) -> AgentProject:
    grants: dict[str, list[str]] = {}
    for definition in definitions:
        grants.setdefault(definition.owner, []).append(definition.capability_id)
    specs = []
    for spec in project.specs:
        authored = grants.get(spec.slug, [])
        if authored and spec.executor.provider != "local":
            raise ProjectSnapshotUnavailable(
                f"agent {spec.slug!r} must use the local executor for authored tools"
            )
        specs.append(
            replace(
                spec,
                tools=tuple(dict.fromkeys((*spec.tools, *authored))),
            )
        )
    return replace(project, specs=tuple(specs))


def project_with_inherited_capabilities(
    project: AgentProject,
    registry: CapabilityRegistry,
) -> AgentProject:
    """Resolve inheritance before Zeta hashes and executes a generation."""
    tools = tuple(registry.list_capability_ids())
    skills = tuple(sorted(project.skills.skills))
    return replace(
        project,
        specs=tuple(
            replace(
                spec,
                tools=tools if spec.tools_inherit else spec.tools,
                skills=skills if spec.skills_inherit else spec.skills,
            )
            for spec in project.specs
        ),
    )


def agent_tools_from_manifest(
    manifest: Mapping[str, Any],
) -> tuple[AgentToolDefinition, ...]:
    raw_tools = manifest.get("agent_tools")
    if not isinstance(raw_tools, list):
        raise ProjectSnapshotUnavailable("project snapshot has invalid agent tools")
    definitions = []
    for raw in raw_tools:
        if not isinstance(raw, Mapping):
            raise ProjectSnapshotUnavailable("project snapshot has invalid agent tool")
        try:
            definition = agent_tool_definition_from_content(
                {
                    "name": raw.get("name"),
                    "capability_id": raw.get("capability_id"),
                    "source": raw.get("source"),
                },
                owner=str(raw.get("owner") or ""),
                key=str(raw.get("key") or ""),
                object_id=str(raw.get("object_id") or ""),
            )
        except AgentToolDefinitionError as exc:
            raise ProjectSnapshotUnavailable(str(exc)) from exc
        definitions.append(definition)
    return tuple(definitions)


def project_from_manifest(
    manifest: Mapping[str, Any],
    *,
    registry: EventConnectorRegistry | None,
) -> AgentProject:
    schema = manifest.get("schema")
    if schema != PROJECT_SNAPSHOT_SCHEMA:
        raise ProjectSnapshotUnavailable(
            f"unsupported project snapshot schema {schema!r}"
        )
    version = manifest.get("version")
    if version != PROJECT_SNAPSHOT_VERSION:
        raise ProjectSnapshotUnavailable(
            f"unsupported project snapshot version {version!r}"
        )
    connectors = registry or EventConnectorRegistry()
    recorded_connectors = manifest.get("connectors")
    current_connectors = [
        connector_manifest(connector)
        for connector in sorted(connectors.event_connectors(), key=lambda item: item.id)
    ]
    if recorded_connectors != current_connectors:
        raise ProjectSnapshotUnavailable(
            "recorded project snapshot connector code is not available"
        )
    raw_events = manifest.get("events")
    if not isinstance(raw_events, Mapping):
        raise ProjectSnapshotUnavailable("project snapshot has invalid events")
    events = EventRegistry(
        {
            str(event_type): schema if isinstance(schema, Mapping) else None
            for event_type, schema in raw_events.items()
        }
    )
    raw_skills = manifest.get("skills")
    if not isinstance(raw_skills, Mapping):
        raise ProjectSnapshotUnavailable("project snapshot has invalid skills")
    skills = SkillRegistry(
        {
            str(name): skill_from_manifest(str(name), value)
            for name, value in raw_skills.items()
        }
    )
    raw_agents = manifest.get("agents")
    if not isinstance(raw_agents, list):
        raise ProjectSnapshotUnavailable("project snapshot has invalid agents")
    return AgentProject(
        specs=tuple(agent_from_manifest(value) for value in raw_agents),
        events=events,
        skills=skills,
        connectors=connectors,
    )


def agent_from_manifest(value: Any) -> AgentSpec:
    if not isinstance(value, Mapping):
        raise ProjectSnapshotUnavailable("project snapshot has invalid agent")
    raw_model = value.get("model")
    model = (
        ModelSpec(name=str(raw_model["name"]), url=str(raw_model["url"]))
        if isinstance(raw_model, Mapping)
        else None
    )
    raw_retry = value.get("retry")
    retry = (
        RetrySpec(
            max_attempts=_optional_int(raw_retry.get("max_attempts")),
            backoff_seconds=_optional_float(raw_retry.get("backoff_seconds")),
        )
        if isinstance(raw_retry, Mapping)
        else None
    )
    raw_executor = value.get("executor")
    if not isinstance(raw_executor, Mapping):
        raise ProjectSnapshotUnavailable("project snapshot has invalid tool executor")
    provider = raw_executor.get("provider")
    config = raw_executor.get("config")
    if (
        not isinstance(provider, str)
        or provider == ""
        or not isinstance(config, Mapping)
    ):
        raise ProjectSnapshotUnavailable("project snapshot has invalid tool executor")
    try:
        normalized_config = executor_config(config)
    except ValueError as exc:
        raise ProjectSnapshotUnavailable(
            "project snapshot has invalid tool executor config"
        ) from exc
    raw_schedules = value.get("schedules")
    schedules = (
        tuple(
            ScheduleEntry(
                cron=str(schedule["cron"]),
                timezone=_optional_str(schedule.get("timezone")),
                catchup=_optional_str(schedule.get("catchup")),
            )
            for schedule in raw_schedules
            if isinstance(schedule, Mapping)
        )
        if isinstance(raw_schedules, list)
        else ()
    )
    raw_manifest = value.get("manifest")
    return AgentSpec(
        slug=str(value["slug"]),
        name=str(value["name"]),
        description=str(value["description"]),
        instructions=str(value["instructions"]),
        path=Path(str(value["path"])),
        sha256=str(value["sha256"]),
        enabled=bool(value.get("enabled", True)),
        session=str(value.get("session", "per-event")),
        model=model,
        executor=ExecutorSpec(provider=provider, config=normalized_config),
        accepts=_string_tuple(value.get("accepts")),
        publishes=_string_tuple(value.get("publishes")),
        returns=_string_tuple(value.get("returns")),
        skills=_string_tuple(value.get("skills")),
        skills_inherit=bool(value.get("skills_inherit", False)),
        tools=_string_tuple(value.get("tools")),
        tools_inherit=bool(value.get("tools_inherit", False)),
        schedules=schedules,
        retry=retry,
        base_dir=(
            Path(str(value["base_dir"])) if value.get("base_dir") is not None else None
        ),
        manifest=dict(raw_manifest) if isinstance(raw_manifest, Mapping) else {},
    )


def skill_from_manifest(name: str, value: Any) -> SkillResource:
    if not isinstance(value, Mapping):
        raise ProjectSnapshotUnavailable("project snapshot has invalid skill")
    body = str(value["body"])
    sha256 = str(value["sha256"])
    if hashlib.sha256(body.encode()).hexdigest() != sha256:
        raise ProjectSnapshotUnavailable(f"recorded skill {name!r} failed verification")
    return SkillResource(name, Path(str(value["path"])), body, sha256)


def agent_manifest(spec: AgentSpec) -> dict[str, Any]:
    manifest = {
        "slug": spec.slug,
        "name": spec.name,
        "description": spec.description,
        "instructions": spec.instructions,
        "path": str(spec.path),
        "sha256": spec.sha256,
        "enabled": spec.enabled,
        "session": spec.session,
        "model": (
            {"name": spec.model.name, "url": spec.model.url}
            if spec.model is not None
            else None
        ),
        "executor": {
            "provider": spec.executor.provider,
            "config": executor_config(spec.executor.config),
        },
        "accepts": list(spec.accepts),
        "publishes": list(spec.publishes),
        "skills": list(spec.skills),
        "skills_inherit": spec.skills_inherit,
        "tools": list(spec.tools),
        "tools_inherit": spec.tools_inherit,
        "schedules": [
            {
                "cron": schedule.cron,
                "timezone": schedule.timezone,
                "catchup": schedule.catchup,
            }
            for schedule in spec.schedules
        ],
        "retry": (
            {
                "max_attempts": spec.retry.max_attempts,
                "backoff_seconds": spec.retry.backoff_seconds,
            }
            if spec.retry is not None
            else None
        ),
        "base_dir": str(spec.base_dir) if spec.base_dir is not None else None,
        "manifest": spec.manifest,
    }
    if spec.returns:
        manifest["returns"] = list(spec.returns)
    return manifest


def connector_manifest(connector: EventConnector) -> dict[str, Any]:
    handlers = [
        *connector.ingress.values(),
        *connector.egress.values(),
        *((connector.push_ingress,) if connector.push_ingress is not None else ()),
    ]
    return {
        "id": connector.id,
        "source": [callable_manifest(handler) for handler in handlers],
        "events": {name: schema for name, schema in sorted(connector.events.items())},
        "ingress": sorted(connector.ingress),
        "egress": sorted(connector.egress),
        "egress_semantics": dict(sorted(connector.egress_semantics.items())),
        "filters": {name: schema for name, schema in sorted(connector.filters.items())},
    }


def callable_manifest(handler: Any) -> dict[str, Any]:
    target = (
        handler
        if inspect.isfunction(handler) or inspect.ismethod(handler)
        else type(handler)
    )
    module = inspect.getmodule(target)
    try:
        path = inspect.getsourcefile(target)
    except TypeError:
        path = None
    return {
        "module": module.__name__ if module is not None else None,
        "qualname": getattr(target, "__qualname__", type(handler).__qualname__),
        "source_sha256": file_sha256(Path(path)) if path is not None else None,
    }


def capability_manifests(registry: CapabilityRegistry) -> dict[str, Any]:
    manifests: dict[str, Any] = {}
    for capability_id in registry.list_capability_ids():
        registered = registry.get(capability_id)
        if registered is None:
            continue
        declaration = registered.declaration
        manifests[capability_id] = {
            "provider": declaration.id.provider,
            "name": declaration.id.name,
            "description": declaration.description,
            "input_schema": declaration.input_schema,
            "delivery_semantics": declaration.delivery_semantics,
            "executor": callable_manifest(registered.executor),
        }
    return manifests


def model_manifest(selection: ModelSelection | None) -> dict[str, Any] | None:
    if selection is None:
        return None
    return {
        "profile": selection.profile,
        "model": selection.model,
        "url": selection.url,
        "thinking": selection.thinking,
        "api": selection.api,
        "tool_profile": selection.tool_profile,
    }


def content_id(prefix: str, value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return f"{prefix}:sha256:{hashlib.sha256(encoded).hexdigest()}"


def file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value)


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_float(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) else None
