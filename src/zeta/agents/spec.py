"""Authored agent spec data structures and frontmatter parsing."""

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SLUG_PATTERN = re.compile(r"^[a-z0-9_-]+$")
BUILT_IN_FRONTMATTER_KEYS = frozenset(
    {
        "name",
        "description",
        "enabled",
        "resumable",
        "accepts",
        "returns",
        "skills",
        "tools",
        "schedules",
        "ingress",
        "egress",
    }
)


@dataclass(frozen=True)
class ScheduleEntry:
    """Structural schedule declaration for an authored agent."""

    cron: str
    timezone: str | None = None


@dataclass(frozen=True)
class IngressBinding:
    """External source binding that can produce an agent-accepted event."""

    source: str
    produces: str | None = None
    filter: Mapping[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None


@dataclass(frozen=True)
class EgressBinding:
    """External sink binding that can handle an agent-returned event."""

    sink: str
    accepts: str | None = None
    filter: Mapping[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None


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
    skills: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    schedules: tuple[ScheduleEntry, ...] = ()
    ingress: tuple[IngressBinding, ...] = ()
    egress: tuple[EgressBinding, ...] = ()
    extensions: dict[str, Any] | None = None

    def extension(self, key: str) -> Any | None:
        """Return a consumer-owned frontmatter extension by key."""
        return (self.extensions or {}).get(key)


class SpecError(ValueError):
    """Raised when an authored agent spec is structurally invalid."""


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
    try:
        authored_accepts = string_tuple(frontmatter.get("accepts", ()), "accepts", path)
        schedules = schedule_tuple(frontmatter.get("schedules", ()), path)
        accepts = accepts_with_schedules(authored_accepts, schedules, slug)
        return AgentSpec(
            slug=slug,
            name=required_string(frontmatter, "name", path),
            description=required_string(frontmatter, "description", path),
            instructions=instructions,
            path=relative_to_cwd(path),
            sha256=hashlib.sha256(raw_bytes).hexdigest(),
            enabled=bool_field(frontmatter.get("enabled", True), "enabled", path),
            resumable=bool_field(
                frontmatter.get("resumable", False), "resumable", path
            ),
            accepts=accepts,
            returns=string_tuple(frontmatter.get("returns", ()), "returns", path),
            skills=string_tuple(frontmatter.get("skills", ()), "skills", path),
            tools=string_tuple(frontmatter.get("tools", ()), "tools", path),
            schedules=schedules,
            ingress=ingress_tuple(frontmatter.get("ingress", ()), path),
            egress=egress_tuple(frontmatter.get("egress", ()), path),
            extensions={
                key: value
                for key, value in frontmatter.items()
                if key not in BUILT_IN_FRONTMATTER_KEYS
            },
        )
    except SpecError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise SpecError(f"invalid spec in {path}: {exc}") from exc


def load_specs(agents_dir: Path) -> tuple[AgentSpec, ...]:
    if not agents_dir.exists():
        return ()
    specs: list[AgentSpec] = []
    for path in sorted(agents_dir.iterdir()):
        if path.suffix != ".md" or not path.is_file() or path.is_symlink():
            continue
        spec = load_spec(path)
        if spec.enabled:
            specs.append(spec)
    return tuple(specs)


def matches(spec: AgentSpec, event_type: str) -> bool:
    """Return whether an enabled spec accepts an exact event type."""
    return spec.enabled and event_type in spec.accepts


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
    if not isinstance(value, str) or value == "":
        raise SpecError(f"missing required field {field!r} in {path}")
    return value


def bool_field(value: Any, field: str, path: Path) -> bool:
    if not isinstance(value, bool):
        raise SpecError(f"invalid value for {field!r} in {path}: expected boolean")
    return value


def string_tuple(value: Any, field: str, path: Path) -> tuple[str, ...]:
    if value is None or value == ():
        return ()
    if not isinstance(value, list | tuple):
        raise SpecError(f"invalid value for {field!r} in {path}: expected list")
    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or item == "":
            raise SpecError(
                f"invalid value for {field!r} in {path}: item {index} "
                "must be a non-empty string"
            )
        items.append(item)
    return tuple(items)


def schedule_tuple(value: Any, path: Path) -> tuple[ScheduleEntry, ...]:
    if value is None or value == ():
        return ()
    if not isinstance(value, list | tuple):
        raise SpecError(f"invalid value for 'schedules' in {path}: expected list")
    return tuple(schedule_entry(item, path) for item in value)


def ingress_tuple(value: Any, path: Path) -> tuple[IngressBinding, ...]:
    if value is None or value == ():
        return ()
    if not isinstance(value, list | tuple):
        raise SpecError(f"invalid value for 'ingress' in {path}: expected list")
    return tuple(ingress_binding(item, path) for item in value)


def ingress_binding(value: Any, path: Path) -> IngressBinding:
    if not isinstance(value, Mapping):
        raise SpecError(f"invalid value for 'ingress' in {path}: expected object")
    reject_unknown_binding_fields(
        value,
        "ingress",
        {"source", "produces", "filter", "idempotency_key"},
        path,
    )
    return IngressBinding(
        source=required_binding_string(value, "ingress", "source", path),
        produces=optional_binding_string(
            value.get("produces"), "ingress", "produces", path
        ),
        filter=binding_filter(value.get("filter", {}), "ingress", path),
        idempotency_key=optional_binding_string(
            value.get("idempotency_key"),
            "ingress",
            "idempotency_key",
            path,
        ),
    )


def egress_tuple(value: Any, path: Path) -> tuple[EgressBinding, ...]:
    if value is None or value == ():
        return ()
    if not isinstance(value, list | tuple):
        raise SpecError(f"invalid value for 'egress' in {path}: expected list")
    return tuple(egress_binding(item, path) for item in value)


def egress_binding(value: Any, path: Path) -> EgressBinding:
    if not isinstance(value, Mapping):
        raise SpecError(f"invalid value for 'egress' in {path}: expected object")
    reject_unknown_binding_fields(
        value,
        "egress",
        {"sink", "accepts", "filter", "idempotency_key"},
        path,
    )
    return EgressBinding(
        sink=required_binding_string(value, "egress", "sink", path),
        accepts=optional_binding_string(
            value.get("accepts"), "egress", "accepts", path
        ),
        filter=binding_filter(value.get("filter", {}), "egress", path),
        idempotency_key=optional_binding_string(
            value.get("idempotency_key"),
            "egress",
            "idempotency_key",
            path,
        ),
    )


def reject_unknown_binding_fields(
    value: Mapping[str, Any],
    field: str,
    supported: set[str],
    path: Path,
) -> None:
    unknown = sorted(set(value) - supported)
    if unknown:
        raise SpecError(
            f"invalid value for {field!r} in {path}: unsupported field {unknown[0]!r}"
        )


def required_binding_string(
    value: Mapping[str, Any],
    field: str,
    name: str,
    path: Path,
) -> str:
    item = value.get(name)
    if not isinstance(item, str) or item == "":
        raise SpecError(f"invalid value for {field!r} in {path}: {name} is required")
    return item


def optional_binding_string(
    value: Any,
    field: str,
    name: str,
    path: Path,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value == "":
        raise SpecError(
            f"invalid value for {field!r} in {path}: {name} must be a string"
        )
    return value


def binding_filter(value: Any, field: str, path: Path) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SpecError(
            f"invalid value for {field!r} in {path}: filter must be an object"
        )
    return dict(value)


def schedule_entry(value: Any, path: Path) -> ScheduleEntry:
    if not isinstance(value, Mapping):
        raise SpecError(f"invalid value for 'schedules' in {path}: expected object")
    if "event" in value:
        raise SpecError(
            f"invalid value for 'schedules' in {path}: event is not supported"
        )
    if "payload" in value:
        raise SpecError(
            f"invalid value for 'schedules' in {path}: payload is not supported"
        )
    cron = required_schedule_string(value, "cron", path)
    timezone = schedule_timezone_name(value.get("timezone"), path)
    return ScheduleEntry(
        cron=cron,
        timezone=timezone,
    )


def required_schedule_string(value: Mapping[str, Any], field: str, path: Path) -> str:
    item = value.get(field)
    if not isinstance(item, str) or item == "":
        raise SpecError(f"invalid value for 'schedules' in {path}: {field} is required")
    return item


def schedule_timezone_name(value: Any, path: Path) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value == "":
        raise SpecError(
            f"invalid value for 'schedules' in {path}: timezone must be a string"
        )
    return value


def accepts_with_schedules(
    accepts: tuple[str, ...],
    schedules: tuple[ScheduleEntry, ...],
    slug: str,
) -> tuple[str, ...]:
    if not schedules:
        return accepts
    scheduled_event = scheduled_event_type(slug)
    if scheduled_event in accepts:
        return accepts
    return (*accepts, scheduled_event)


def scheduled_event_type(agent_slug: str) -> str:
    return f"agent.{agent_slug}.scheduled"


def relative_to_cwd(path: Path) -> Path:
    try:
        return path.resolve().relative_to(Path.cwd().resolve())
    except ValueError:
        return path.resolve()
    except OSError:
        return path
