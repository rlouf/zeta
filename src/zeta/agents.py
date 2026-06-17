"""Declarative authored-agent specs for Zeta runtimes."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import yaml
from jinja2 import Environment, meta
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .agent import AgentConfig, AgentTurnResult, run_agent_turn
from .events import AgentDefinition, AgentRun, Event, TriggerRule
from .tools.registry import CapabilityRegistry

SLUG_PATTERN = re.compile(r"^[a-z0-9_-]+$")
RESERVED_TOOL_NAMES = frozenset({"__return"})
BUILT_IN_FRONTMATTER_KEYS = frozenset(
    {
        "name",
        "description",
        "enabled",
        "resumable",
        "accepts",
        "returns",
        "tools",
        "schedules",
    }
)


@dataclass(frozen=True)
class EventEnvelope:
    """Minimal event shape exposed to authored prompt templates."""

    event_type: str
    payload: dict[str, Any]

    @classmethod
    def from_event(cls, event: Event) -> EventEnvelope:
        return cls(event_type=event.event_type, payload=dict(event.payload))

    def to_template_context(self) -> dict[str, Any]:
        return {"event_type": self.event_type, "payload": self.payload}


@dataclass(frozen=True)
class ScheduleEntry:
    """Structural schedule declaration for an authored agent."""

    cron: str
    event: str
    payload: dict[str, Any]
    timezone: str | None = None


@dataclass(frozen=True)
class AgentSpec:
    """Parsed authored agent specification."""

    slug: str
    name: str
    description: str
    instructions: str
    path: Path
    sha256: str
    enabled: bool = True
    resumable: bool = False
    accepts: tuple[str, ...] = ()
    returns: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    schedules: tuple[ScheduleEntry, ...] = ()
    extensions: dict[str, Any] | None = None

    def extension(self, key: str) -> Any | None:
        """Return a consumer-owned frontmatter extension by key."""
        return (self.extensions or {}).get(key)


class SpecError(ValueError):
    """Raised when an authored agent spec is structurally invalid."""


class TemplateError(ValueError):
    """Raised when an authored prompt template is invalid or cannot render."""


class EventRegistryError(ValueError):
    """Raised when an event registry entry is invalid."""


class ManifestError(ValueError):
    """Raised when an authored spec does not match a deployment manifest."""


class EventRegistry:
    """Known event types and optional payload schemas."""

    def __init__(
        self,
        events: Mapping[str, Mapping[str, Any] | None] | None = None,
    ) -> None:
        self._schemas: dict[str, dict[str, Any] | None] = {}
        for event_type, schema in (events or {}).items():
            self.register(event_type, schema)

    def register(
        self,
        event_type: str,
        schema: Mapping[str, Any] | None = None,
    ) -> None:
        if event_type in self._schemas:
            raise EventRegistryError(f"event {event_type!r} is already registered")
        parsed_schema = dict(schema) if schema is not None else None
        if parsed_schema is not None:
            try:
                Draft202012Validator.check_schema(parsed_schema)
            except SchemaError as exc:
                raise EventRegistryError(
                    f"event {event_type!r} has a malformed schema: {exc.message}"
                ) from exc
        self._schemas[event_type] = parsed_schema

    def knows(self, event_type: str) -> bool:
        return event_type in self._schemas

    def schema(self, event_type: str) -> dict[str, Any] | None:
        schema = self._schemas.get(event_type)
        return dict(schema) if schema is not None else None


@dataclass(frozen=True)
class Manifest:
    """Deployment manifest used to validate authored agent specs."""

    tools: CapabilityRegistry | None = None
    events: EventRegistry | None = None
    extensions: Mapping[str, type[Any]] | None = None

    def validate(self, spec: AgentSpec) -> None:
        validate_prompt(spec)
        validate_tools(spec, self.tools)
        validate_events(spec, self.events)
        validate_extensions(spec, self.extensions or {})


AgentTurnRunner = Callable[..., AgentTurnResult]
TimelineFactory = Callable[[AgentRun], list[dict[str, Any]]]
ContextFactory = Callable[[AgentRun], str]


def load_spec(path: str | Path) -> AgentSpec:
    """Load one authored agent spec from a Markdown file."""
    path = Path(path)
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise SpecError(f"I/O error reading {path}: {exc}") from exc
    try:
        content = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SpecError(f"{path} is not valid UTF-8: {exc}") from exc
    frontmatter, instructions = split_frontmatter(content, path)
    slug = derive_slug(path)
    accepts = string_tuple(frontmatter.get("accepts", ()), "accepts", path)
    schedules = schedule_tuple(frontmatter.get("schedules", ()), path)
    validate_schedules_subset_of_accepts(schedules, accepts, path)
    return AgentSpec(
        slug=slug,
        name=required_string(frontmatter, "name", path),
        description=required_string(frontmatter, "description", path),
        instructions=instructions,
        path=relative_to_cwd(path),
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        enabled=bool_field(frontmatter.get("enabled", True), "enabled", path),
        resumable=bool_field(frontmatter.get("resumable", False), "resumable", path),
        accepts=accepts,
        returns=string_tuple(frontmatter.get("returns", ()), "returns", path),
        tools=string_tuple(frontmatter.get("tools", ()), "tools", path),
        schedules=schedules,
        extensions={
            key: value
            for key, value in frontmatter.items()
            if key not in BUILT_IN_FRONTMATTER_KEYS
        },
    )


def load_specs_recursive(directory: str | Path) -> list[AgentSpec]:
    """Load every enabled Markdown spec under a directory in stable order."""
    root = Path(directory)
    specs = []
    for path in sorted(root.rglob("*.md")):
        if not path.is_file() or path.is_symlink():
            continue
        spec = load_spec(path)
        if spec.enabled:
            specs.append(spec)
    return specs


def matches(spec: AgentSpec, event_type: str) -> bool:
    """Return whether an enabled spec accepts an exact event type."""
    return spec.enabled and event_type in spec.accepts


def render_prompt(spec: AgentSpec, envelope: EventEnvelope) -> str:
    """Render an authored prompt with one root variable, ``event``."""
    try:
        template = Environment(autoescape=False).from_string(spec.instructions)
        return template.render(event=envelope.to_template_context())
    except Exception as exc:
        raise TemplateError(
            f"template render error in agent {spec.slug!r}: {exc}"
        ) from exc


def validate_prompt(spec: AgentSpec) -> None:
    """Reject templates that reference roots other than ``event``."""
    environment = Environment(autoescape=False)
    try:
        parsed = environment.parse(spec.instructions)
    except Exception as exc:
        raise TemplateError(
            f"template syntax error in agent {spec.slug!r}: {exc}"
        ) from exc
    for name in meta.find_undeclared_variables(parsed):
        if name != "event":
            raise TemplateError(
                f"agent {spec.slug!r} template references unknown variable {name!r}"
            )


def validate_tools(spec: AgentSpec, registry: CapabilityRegistry | None) -> None:
    if registry is None:
        if spec.tools:
            raise ManifestError(f"agent {spec.slug!r} lists unknown tool {spec.tools[0]!r}")
        return
    for name in spec.tools:
        if name in RESERVED_TOOL_NAMES:
            raise ManifestError(f"agent {spec.slug!r} lists reserved tool {name!r}")
        if registry.resolve(name) is None:
            raise ManifestError(f"agent {spec.slug!r} lists unknown tool {name!r}")


def validate_events(spec: AgentSpec, registry: EventRegistry | None) -> None:
    if registry is None:
        if spec.accepts:
            raise ManifestError(
                f"agent {spec.slug!r} references unknown event {spec.accepts[0]!r}"
            )
        if spec.returns:
            raise ManifestError(
                f"agent {spec.slug!r} references unknown event {spec.returns[0]!r}"
            )
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


def validate_extensions(spec: AgentSpec, extensions: Mapping[str, type[Any]]) -> None:
    for key, value in (spec.extensions or {}).items():
        expected = extensions.get(key)
        if expected is None:
            raise ManifestError(f"agent {spec.slug!r} uses unknown extension {key!r}")
        if not isinstance(value, expected):
            raise ManifestError(
                f"agent {spec.slug!r} extension {key!r} has invalid shape"
            )


def derive_returns_schema(
    spec: AgentSpec,
    events: EventRegistry | None = None,
) -> dict[str, Any] | None:
    """Derive the per-event schema for events this spec may return."""
    if not spec.returns:
        return None
    branches = []
    for event_type in spec.returns:
        payload_schema = events.schema(event_type) if events is not None else None
        branches.append(
            {
                "type": "object",
                "required": ["type", "payload"],
                "properties": {
                    "type": {"const": event_type},
                    "payload": payload_schema or {},
                },
                "additionalProperties": False,
            }
        )
    return {"type": "object", "anyOf": branches}


def compile_agent_definition(
    spec: AgentSpec,
    *,
    config: AgentConfig | None = None,
    context: str | ContextFactory = "",
    timeline: Sequence[dict[str, Any]] | TimelineFactory = (),
    run_turn: AgentTurnRunner = run_agent_turn,
) -> AgentDefinition:
    """Compile a single-accept spec into an in-process runtime agent."""
    if len(spec.accepts) != 1:
        raise ValueError("compile_agent_definition requires exactly one accepted event")
    return compile_agent_definitions(
        spec,
        config=config,
        context=context,
        timeline=timeline,
        run_turn=run_turn,
    )[0]


def compile_agent_definitions(
    spec: AgentSpec,
    *,
    config: AgentConfig | None = None,
    context: str | ContextFactory = "",
    timeline: Sequence[dict[str, Any]] | TimelineFactory = (),
    run_turn: AgentTurnRunner = run_agent_turn,
) -> list[AgentDefinition]:
    """Compile one authored spec into runtime definitions for each accepted event."""
    if not spec.accepts:
        return []
    return [
        AgentDefinition(
            agent_id=spec.slug,
            trigger=TriggerRule(event_type=event_type),
            allowed_capabilities=spec.tools,
            system_prompt=spec.description,
            max_turns=config.max_turns if config is not None else None,
            dispatch_mode="session_scoped" if spec.resumable else "one_shot",
            run=agent_runner(spec, config, context, timeline, run_turn),
        )
        for event_type in spec.accepts
    ]


def agent_runner(
    spec: AgentSpec,
    config: AgentConfig | None,
    context: str | ContextFactory,
    timeline: Sequence[dict[str, Any]] | TimelineFactory,
    run_turn: AgentTurnRunner,
) -> Callable[[AgentRun], dict[str, Any]]:
    def run(agent_run: AgentRun) -> dict[str, Any]:
        effective_config = config_for_spec(spec, config)
        objective = render_prompt(spec, EventEnvelope.from_event(agent_run.triggering_event))
        if callable(timeline):
            run_timeline = cast(TimelineFactory, timeline)(agent_run)
        else:
            run_timeline = list(timeline)
        if callable(context):
            run_context = cast(ContextFactory, context)(agent_run)
        else:
            run_context = context
        result = run_turn(
            objective,
            run_timeline,
            effective_config,
            context=run_context,
            caused_by=agent_run.triggering_event.id,
        )
        return agent_turn_result_mapping(result)

    return run


def config_for_spec(spec: AgentSpec, config: AgentConfig | None) -> AgentConfig:
    if config is None:
        return AgentConfig(
            system_prompt=spec.description,
            allowed_capabilities=spec.tools,
        )
    return replace(
        config,
        system_prompt=config.system_prompt or spec.description,
        allowed_capabilities=config.allowed_capabilities or spec.tools,
    )


def agent_turn_result_mapping(result: AgentTurnResult) -> dict[str, Any]:
    payload: dict[str, Any] = {"final_text": result.final_text}
    if result.events:
        payload["events"] = result.events
    if result.staged_effect is not None:
        payload["staged_effect"] = result.staged_effect
    return payload


def split_frontmatter(content: str, path: Path) -> tuple[dict[str, Any], str]:
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise SpecError(f"missing frontmatter delimiter in {path}")
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() != "---":
            continue
        frontmatter_text = "".join(lines[1:index])
        body = "".join(lines[index + 1 :])
        try:
            raw = yaml.safe_load(frontmatter_text)
        except yaml.YAMLError as exc:
            raise SpecError(f"invalid YAML frontmatter in {path}: {exc}") from exc
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise SpecError(f"invalid YAML frontmatter in {path}: expected object")
        return dict(raw), body
    raise SpecError(f"missing closing frontmatter delimiter in {path}")


def derive_slug(path: Path) -> str:
    slug = path.stem
    if not SLUG_PATTERN.fullmatch(slug):
        raise SpecError(f"invalid slug {slug!r} for {path}: must match [a-z0-9_-]+")
    return slug


def required_string(frontmatter: Mapping[str, Any], field: str, path: Path) -> str:
    value = frontmatter.get(field)
    if not isinstance(value, str) or not value:
        raise SpecError(f"missing required field {field!r} in {path}")
    return value


def bool_field(value: Any, field: str, path: Path) -> bool:
    if not isinstance(value, bool):
        raise SpecError(f"invalid value for {field!r} in {path}: expected boolean")
    return value


def string_tuple(value: Any, field: str, path: Path) -> tuple[str, ...]:
    if value is None or value == ():
        return ()
    if not isinstance(value, list):
        raise SpecError(f"invalid value for {field!r} in {path}: expected list")
    out = []
    for item in value:
        if not isinstance(item, str):
            raise SpecError(f"invalid value for {field!r} in {path}: expected strings")
        out.append(item)
    return tuple(out)


def schedule_tuple(value: Any, path: Path) -> tuple[ScheduleEntry, ...]:
    if value is None or value == ():
        return ()
    if not isinstance(value, list):
        raise SpecError(f"invalid value for 'schedules' in {path}: expected list")
    return tuple(schedule_entry(item, path) for item in value)


def schedule_entry(value: Any, path: Path) -> ScheduleEntry:
    if not isinstance(value, dict):
        raise SpecError(f"invalid value for 'schedules' in {path}: expected objects")
    cron = required_schedule_string(value, "cron", path)
    event = required_schedule_string(value, "event", path)
    payload = schedule_payload(value.get("payload", {}), path)
    timezone = schedule_timezone(value.get("timezone"), path)
    return ScheduleEntry(
        cron=cron,
        event=event,
        payload=payload,
        timezone=timezone,
    )


def required_schedule_string(value: Mapping[str, Any], field: str, path: Path) -> str:
    item = value.get(field)
    if not isinstance(item, str):
        raise SpecError(
            f"invalid value for 'schedules' in {path}: {field} is required"
        )
    return item


def schedule_payload(value: Any, path: Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SpecError(
            f"invalid value for 'schedules' in {path}: payload must be an object"
        )
    return dict(value)


def schedule_timezone(value: Any, path: Path) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise SpecError(
        f"invalid value for 'schedules' in {path}: timezone must be a string"
    )


def validate_schedules_subset_of_accepts(
    schedules: Iterable[ScheduleEntry],
    accepts: tuple[str, ...],
    path: Path,
) -> None:
    for schedule in schedules:
        if schedule.event not in accepts:
            raise SpecError(
                f"invalid value for 'schedules' in {path}: schedule emits event "
                f"{schedule.event!r} that is not listed in accepts"
            )


def relative_to_cwd(path: Path) -> Path:
    try:
        return path.resolve().relative_to(Path.cwd().resolve())
    except ValueError:
        return path.resolve()
    except OSError:
        return path
