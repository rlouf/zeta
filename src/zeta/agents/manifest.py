"""Deployment manifest validation for authored agents."""

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, cast, runtime_checkable

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from zeta.agents.events import EventRegistry
from zeta.agents.prompts import validate_prompt
from zeta.agents.spec import AgentSpec
from zeta.events import DraftEvent, Event

RESERVED_TOOL_NAMES = frozenset({"__return"})


class ManifestError(ValueError):
    """Raised when an authored spec does not match a deployment manifest."""


@runtime_checkable
class ToolResolver(Protocol):
    """Anything that can look up a tool by name."""

    def resolve(self, name: str) -> Any: ...


@runtime_checkable
class SkillResolver(Protocol):
    """Anything that can look up a skill by name."""

    def knows(self, name: str) -> bool: ...


@dataclass(frozen=True)
class IngressBinding:
    """External source binding parsed from a plugin-owned manifest section."""

    source: str
    event: str | None = None
    filter: Mapping[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None


@dataclass(frozen=True)
class EgressBinding:
    """External sink binding parsed from a plugin-owned manifest section."""

    sink: str
    event: str | None = None
    filter: Mapping[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None


PluginIngressPoller = Callable[
    [IngressBinding],
    Iterable[DraftEvent] | Awaitable[Iterable[DraftEvent]],
]
PluginEgressHandler = Callable[
    [Event, EgressBinding, str],
    Mapping[str, Any] | None | Awaitable[Mapping[str, Any] | None],
]


@dataclass(frozen=True)
class PluginManifestSection:
    """Plugin-owned frontmatter section metadata."""

    key: str
    schema: Mapping[str, Any] | None = None
    events: Mapping[str, Mapping[str, Any] | None] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentPlugin:
    """Static plugin metadata used by Zeta project validation."""

    name: str
    events: Mapping[str, Mapping[str, Any] | None] = field(default_factory=dict)
    manifest_sections: Mapping[str, PluginManifestSection] = field(default_factory=dict)
    ingress_pollers: Mapping[str, PluginIngressPoller] = field(default_factory=dict)
    egress_handlers: Mapping[str, PluginEgressHandler] = field(default_factory=dict)


@runtime_checkable
class PluginResolver(Protocol):
    """Anything that can resolve installed plugin metadata."""

    def resolve(self, name: str) -> AgentPlugin | None: ...

    def names_for_section(self, key: str, value: Any) -> Iterable[str]: ...


@dataclass(frozen=True)
class Manifest:
    """Deployment manifest used to validate authored agent specs."""

    tools: ToolResolver | None = None
    skills: SkillResolver | Mapping[str, Any] | None = None
    events: EventRegistry | None = None
    plugins: PluginResolver | Mapping[str, AgentPlugin] | None = None

    def validate(self, spec: AgentSpec) -> None:
        validate_prompt(spec)
        validate_tools(spec, self.tools)
        validate_skills(spec, self.skills)
        validate_plugin_sections(spec, self.plugins)
        validate_plugin_bindings(spec, self.plugins)
        validate_events(spec, self.events)


def validate_tools(spec: AgentSpec, registry: ToolResolver | None) -> None:
    for name in spec.tools:
        if name in RESERVED_TOOL_NAMES:
            raise ManifestError(f"agent {spec.slug!r} lists reserved tool {name!r}")
        if registry is not None and registry.resolve(name) is None:
            raise ManifestError(f"agent {spec.slug!r} lists unknown tool {name!r}")


def validate_skills(
    spec: AgentSpec,
    registry: SkillResolver | Mapping[str, Any] | None,
) -> None:
    if registry is None:
        return
    for name in spec.skills:
        if isinstance(registry, Mapping):
            known = name in registry
        else:
            known = registry.knows(name)
        if not known:
            raise ManifestError(f"agent {spec.slug!r} lists unknown skill {name!r}")


def validate_events(spec: AgentSpec, registry: EventRegistry | None) -> None:
    if registry is None:
        return
    for event_type in spec.accepts:
        if not registry.knows(event_type):
            raise ManifestError(
                f"agent {spec.slug!r} references unknown event {event_type!r} in accepts"
            )
    for event_type in spec.returns:
        if not registry.knows(event_type):
            raise ManifestError(
                f"agent {spec.slug!r} references unknown event {event_type!r} in returns"
            )


def validate_plugin_bindings(
    spec: AgentSpec,
    plugins: PluginResolver | Mapping[str, AgentPlugin] | None,
) -> None:
    for binding in ingress_bindings(spec):
        validate_ingress_binding(spec, binding, plugins)
    for binding in egress_bindings(spec):
        validate_egress_binding(spec, binding, plugins)


def validate_plugin_sections(
    spec: AgentSpec,
    plugins: PluginResolver | Mapping[str, AgentPlugin] | None,
) -> None:
    plugin_map = resolved_plugins_for_spec(spec, plugins)
    for key, value in spec.manifest.items():
        if key in {"ingress", "egress"}:
            continue
        claimants = [
            plugin for plugin in plugin_map.values() if key in plugin.manifest_sections
        ]
        if not claimants:
            raise ManifestError(
                f"agent {spec.slug!r} uses unknown manifest section {key!r}"
            )
        if len(claimants) > 1:
            names = "', '".join(plugin.name for plugin in claimants)
            raise ManifestError(
                f"agent {spec.slug!r} manifest section {key!r} is claimed by "
                f"multiple plugins: '{names}'"
            )
        section = claimants[0].manifest_sections[key]
        validate_section_schema(
            value,
            section.schema,
            f"agent {spec.slug!r} has invalid manifest section {key!r}",
        )


def validate_ingress_binding(
    spec: AgentSpec,
    binding: IngressBinding,
    plugins: PluginResolver | Mapping[str, AgentPlugin] | None,
) -> None:
    plugin = resolve_plugin(plugins, binding.source)
    if plugin is None:
        raise ManifestError(
            f"agent {spec.slug!r} references unknown ingress source {binding.source!r}"
        )
    section = plugin_manifest_section(plugin, "ingress")
    validate_section_schema(
        ingress_binding_record(binding),
        section.schema,
        f"agent {spec.slug!r} has invalid ingress binding for {binding.source!r}",
    )
    event_type = selected_plugin_event(
        binding.event,
        section.events,
        "ingress source",
        binding.source,
    )
    if event_type not in spec.accepts:
        raise ManifestError(
            f"agent {spec.slug!r} ingress event {event_type!r} is not listed in accepts"
        )
    if binding.idempotency_key is None:
        raise ManifestError(
            f"agent {spec.slug!r} ingress source {binding.source!r} requires idempotency_key"
        )
    validate_binding_filter(
        binding.filter,
        section.events[event_type],
        f"agent {spec.slug!r} has invalid ingress filter for {binding.source!r}",
    )


def validate_egress_binding(
    spec: AgentSpec,
    binding: EgressBinding,
    plugins: PluginResolver | Mapping[str, AgentPlugin] | None,
) -> None:
    plugin = resolve_plugin(plugins, binding.sink)
    if plugin is None:
        raise ManifestError(
            f"agent {spec.slug!r} references unknown egress sink {binding.sink!r}"
        )
    section = plugin_manifest_section(plugin, "egress")
    validate_section_schema(
        egress_binding_record(binding),
        section.schema,
        f"agent {spec.slug!r} has invalid egress binding for {binding.sink!r}",
    )
    event_type = selected_plugin_event(
        binding.event,
        section.events,
        "egress sink",
        binding.sink,
    )
    if event_type not in spec.returns:
        raise ManifestError(
            f"agent {spec.slug!r} egress event {event_type!r} is not listed in returns"
        )
    validate_binding_filter(
        binding.filter,
        section.events[event_type],
        f"agent {spec.slug!r} has invalid egress filter for {binding.sink!r}",
    )


def resolve_plugin(
    plugins: PluginResolver | Mapping[str, AgentPlugin] | None,
    name: str,
) -> AgentPlugin | None:
    if plugins is None:
        return None
    if isinstance(plugins, Mapping):
        return cast(Mapping[str, AgentPlugin], plugins).get(name)
    return plugins.resolve(name)


def resolved_plugins_for_spec(
    spec: AgentSpec,
    plugins: PluginResolver | Mapping[str, AgentPlugin] | None,
) -> Mapping[str, AgentPlugin]:
    if isinstance(plugins, Mapping):
        return cast(Mapping[str, AgentPlugin], plugins)
    if plugins is None:
        return {}
    resolved: dict[str, AgentPlugin] = {}
    for key, value in spec.manifest.items():
        for name in plugins.names_for_section(key, value):
            plugin = plugins.resolve(name)
            if plugin is not None:
                resolved[name] = plugin
    return resolved


def plugin_manifest_section(plugin: AgentPlugin, key: str) -> PluginManifestSection:
    section = plugin.manifest_sections.get(key)
    if section is None:
        raise ManifestError(f"plugin {plugin.name!r} does not support {key!r}")
    return section


def selected_plugin_event(
    event_type: str | None,
    available: Mapping[str, Mapping[str, Any] | None],
    binding_kind: str,
    plugin_name: str,
) -> str:
    if event_type is not None:
        if event_type not in available:
            raise ManifestError(
                f"{binding_kind} {plugin_name!r} does not support event {event_type!r}"
            )
        return event_type
    if len(available) != 1:
        raise ManifestError(
            f"{binding_kind} {plugin_name!r} requires an event type because it has "
            f"{len(available)} event types"
        )
    return next(iter(available))


def validate_binding_filter(
    value: Mapping[str, Any],
    schema: Mapping[str, Any] | None,
    message: str,
) -> None:
    if schema is None:
        if value:
            raise ManifestError(f"{message}: filter is not supported")
        return
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(dict(value))
    except SchemaError as exc:
        raise ManifestError(
            f"{message}: plugin filter schema is invalid: {exc.message}"
        ) from exc
    except ValidationError as exc:
        raise ManifestError(f"{message}: {exc.message}") from exc


def validate_section_schema(
    value: Any,
    schema: Mapping[str, Any] | None,
    message: str,
) -> None:
    if schema is None:
        return
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(value)
    except SchemaError as exc:
        raise ManifestError(
            f"{message}: plugin section schema is invalid: {exc.message}"
        ) from exc
    except ValidationError as exc:
        raise ManifestError(f"{message}: {exc.message}") from exc


def ingress_bindings(spec: AgentSpec) -> tuple[IngressBinding, ...]:
    section = spec.manifest.get("ingress", ())
    return tuple(
        IngressBinding(
            source=required_binding_string(item, "ingress", "source", spec),
            event=optional_binding_string(item.get("event"), "ingress", "event", spec),
            filter=binding_filter(item.get("filter", {}), "ingress", spec),
            idempotency_key=optional_binding_string(
                item.get("idempotency_key"),
                "ingress",
                "idempotency_key",
                spec,
            ),
        )
        for item in binding_items(section, "ingress", spec)
    )


def egress_bindings(spec: AgentSpec) -> tuple[EgressBinding, ...]:
    section = spec.manifest.get("egress", ())
    return tuple(
        EgressBinding(
            sink=required_binding_string(item, "egress", "sink", spec),
            event=optional_binding_string(item.get("event"), "egress", "event", spec),
            filter=binding_filter(item.get("filter", {}), "egress", spec),
            idempotency_key=optional_binding_string(
                item.get("idempotency_key"),
                "egress",
                "idempotency_key",
                spec,
            ),
        )
        for item in binding_items(section, "egress", spec)
    )


def binding_items(
    section: Any,
    key: str,
    spec: AgentSpec,
) -> tuple[Mapping[str, Any], ...]:
    if section is None or section == ():
        return ()
    if not isinstance(section, list | tuple):
        raise ManifestError(
            f"agent {spec.slug!r} has invalid {key!r} section: expected list"
        )
    return tuple(binding_item(item, key, spec) for item in section)


def binding_item(value: Any, key: str, spec: AgentSpec) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ManifestError(
            f"agent {spec.slug!r} has invalid {key!r} section: expected object"
        )
    supported = (
        {"source", "event", "filter", "idempotency_key"}
        if key == "ingress"
        else {
            "sink",
            "event",
            "filter",
            "idempotency_key",
        }
    )
    unknown = sorted(set(value) - supported)
    if unknown:
        raise ManifestError(
            f"agent {spec.slug!r} has invalid {key!r} section: "
            f"unsupported field {unknown[0]!r}"
        )
    return value


def required_binding_string(
    value: Mapping[str, Any],
    key: str,
    name: str,
    spec: AgentSpec,
) -> str:
    item = value.get(name)
    if not isinstance(item, str) or item == "":
        raise ManifestError(
            f"agent {spec.slug!r} has invalid {key!r} section: {name} is required"
        )
    return item


def optional_binding_string(
    value: Any,
    key: str,
    name: str,
    spec: AgentSpec,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value == "":
        raise ManifestError(
            f"agent {spec.slug!r} has invalid {key!r} section: {name} must be a string"
        )
    return value


def binding_filter(value: Any, key: str, spec: AgentSpec) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ManifestError(
            f"agent {spec.slug!r} has invalid {key!r} section: filter must be an object"
        )
    return dict(value)


def ingress_binding_record(binding: IngressBinding) -> dict[str, Any]:
    record: dict[str, Any] = {
        "source": binding.source,
        "filter": dict(binding.filter),
    }
    if binding.event is not None:
        record["event"] = binding.event
    if binding.idempotency_key is not None:
        record["idempotency_key"] = binding.idempotency_key
    return record


def egress_binding_record(binding: EgressBinding) -> dict[str, Any]:
    record: dict[str, Any] = {
        "sink": binding.sink,
        "filter": dict(binding.filter),
    }
    if binding.event is not None:
        record["event"] = binding.event
    if binding.idempotency_key is not None:
        record["idempotency_key"] = binding.idempotency_key
    return record
