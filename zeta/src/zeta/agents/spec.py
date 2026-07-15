"""Authored agent spec data structures and frontmatter parsing."""

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from connectors import EgressBinding, IngressBinding

SLUG_PATTERN = re.compile(r"^[a-z0-9_-]+$")
BUILT_IN_FRONTMATTER_KEYS = frozenset(
    {
        "name",
        "description",
        "enabled",
        "resumable",
        "model",
        "accepts",
        "returns",
        "skills",
        "tools",
        "schedules",
        "retry",
        "base_dir",
    }
)


@dataclass(frozen=True)
class ScheduleEntry:
    """Structural schedule declaration for an authored agent."""

    cron: str
    timezone: str | None = None


@dataclass(frozen=True)
class ModelSpec:
    """Concrete model endpoint for one authored agent."""

    name: str
    url: str


@dataclass(frozen=True)
class RetrySpec:
    """Per-agent retry policy override from authored frontmatter."""

    max_attempts: int | None = None
    backoff_seconds: float | None = None


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
    model: ModelSpec | None = None
    accepts: tuple[str, ...] = ()
    returns: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    schedules: tuple[ScheduleEntry, ...] = ()
    retry: RetrySpec | None = None
    base_dir: Path | None = None
    ingress: tuple[IngressBinding, ...] = ()
    egress: tuple[EgressBinding, ...] = ()
    manifest: dict[str, Any] = field(default_factory=dict)


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
        authored_accepts, ingress = accepts_entries(
            frontmatter.get("accepts", ()),
            path,
        )
        schedules = schedule_tuple(frontmatter.get("schedules", ()), path)
        accepts = accepts_with_schedules(authored_accepts, schedules, slug)
        returns, egress = returns_entries(frontmatter.get("returns", ()), path)
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
            model=model_spec(frontmatter.get("model"), path),
            accepts=accepts,
            returns=returns,
            skills=string_tuple(frontmatter.get("skills", ()), "skills", path),
            tools=string_tuple(frontmatter.get("tools", ()), "tools", path),
            schedules=schedules,
            retry=retry_spec(frontmatter.get("retry"), path),
            base_dir=base_dir_field(frontmatter.get("base_dir"), path),
            ingress=ingress,
            egress=egress,
            manifest={
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


def model_spec(value: Any, path: Path) -> ModelSpec | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise SpecError(f"invalid value for 'model' in {path}: expected object")
    unknown = sorted(set(value) - {"name", "url"})
    if unknown:
        raise SpecError(
            f"invalid value for 'model' in {path}: unsupported field {unknown[0]!r}"
        )
    return ModelSpec(
        name=required_model_string(value, "name", path),
        url=required_model_string(value, "url", path),
    )


def base_dir_field(value: Any, path: Path) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or value.strip() == "":
        raise SpecError(
            f"invalid value for 'base_dir' in {path}: expected a path string"
        )
    expanded = Path(value).expanduser()
    if not expanded.is_absolute():
        raise SpecError(
            f"invalid value for 'base_dir' in {path}: "
            f"{value!r} must resolve to an absolute path"
        )
    return expanded


def retry_spec(value: Any, path: Path) -> RetrySpec | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise SpecError(f"invalid value for 'retry' in {path}: expected object")
    unknown = sorted(set(value) - {"max_attempts", "backoff_seconds"})
    if unknown:
        raise SpecError(
            f"invalid value for 'retry' in {path}: unsupported field {unknown[0]!r}"
        )
    return RetrySpec(
        max_attempts=optional_positive_int(value.get("max_attempts"), "retry", path),
        backoff_seconds=optional_nonnegative_number(
            value.get("backoff_seconds"),
            "retry",
            path,
        ),
    )


def optional_positive_int(value: Any, field: str, path: Path) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise SpecError(
            f"invalid value for {field!r} in {path}: max_attempts "
            "must be a positive integer"
        )
    return value


def optional_nonnegative_number(value: Any, field: str, path: Path) -> float | None:
    if value is None:
        return None
    if not isinstance(value, int | float) or isinstance(value, bool) or value < 0:
        raise SpecError(
            f"invalid value for {field!r} in {path}: backoff_seconds "
            "must be a non-negative number"
        )
    return float(value)


def required_model_string(value: Mapping[str, Any], field: str, path: Path) -> str:
    item = value.get(field)
    if not isinstance(item, str) or item == "":
        raise SpecError(
            f"invalid value for 'model' in {path}: {field} must be a non-empty string"
        )
    return item


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


def accepts_entries(
    value: Any, path: Path
) -> tuple[tuple[str, ...], tuple[IngressBinding, ...]]:
    if value is None or value == ():
        return (), ()
    if not isinstance(value, list | tuple):
        raise SpecError(f"invalid value for 'accepts' in {path}: expected list")
    events: list[str] = []
    bindings: list[IngressBinding] = []
    for index, item in enumerate(value):
        if isinstance(item, str) and item:
            events.append(item)
            continue
        entry = event_entry(item, "accepts", index, path)
        event = required_event(entry, "accepts", index, path)
        events.append(event)
        bindings.append(
            IngressBinding(
                event=event,
                filter=mapping_field(
                    entry.get("filter", {}),
                    "accepts",
                    "filter",
                    index,
                    path,
                ),
                idempotency_key=optional_string_field(
                    entry.get("idempotency_key"),
                    "accepts",
                    "idempotency_key",
                    index,
                    path,
                ),
            )
        )
    return tuple(events), tuple(bindings)


def returns_entries(
    value: Any, path: Path
) -> tuple[tuple[str, ...], tuple[EgressBinding, ...]]:
    if value is None or value == ():
        return (), ()
    if not isinstance(value, list | tuple):
        raise SpecError(f"invalid value for 'returns' in {path}: expected list")
    events: list[str] = []
    bindings: list[EgressBinding] = []
    for index, item in enumerate(value):
        if isinstance(item, str) and item:
            events.append(item)
            continue
        entry = event_entry(item, "returns", index, path)
        event = required_event(entry, "returns", index, path)
        events.append(event)
        bindings.append(
            EgressBinding(
                event=event,
                options=mapping_field(
                    entry.get("with", {}),
                    "returns",
                    "with",
                    index,
                    path,
                ),
                idempotency_key=optional_string_field(
                    entry.get("idempotency_key"),
                    "returns",
                    "idempotency_key",
                    index,
                    path,
                ),
            )
        )
    return tuple(events), tuple(bindings)


def event_entry(value: Any, field: str, index: int, path: Path) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SpecError(
            f"invalid value for {field!r} in {path}: item {index} "
            "must be a non-empty string or object"
        )
    supported = (
        {"event", "filter", "idempotency_key"}
        if field == "accepts"
        else {"event", "with", "idempotency_key"}
    )
    if field == "returns" and "filter" in value:
        raise SpecError(
            f"invalid value for 'returns' in {path}: item {index} must use "
            "'with' for returned event options"
        )
    unknown = sorted(set(value) - supported)
    if unknown:
        raise SpecError(
            f"invalid value for {field!r} in {path}: item {index} has "
            f"unsupported field {unknown[0]!r}"
        )
    return value


def required_event(value: Mapping[str, Any], field: str, index: int, path: Path) -> str:
    event = value.get("event")
    if not isinstance(event, str) or event == "":
        raise SpecError(
            f"invalid value for {field!r} in {path}: item {index} event is required"
        )
    return event


def optional_string_field(
    value: Any,
    field: str,
    name: str,
    index: int,
    path: Path,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value == "":
        raise SpecError(
            f"invalid value for {field!r} in {path}: item {index} "
            f"{name} must be a string"
        )
    return value


def mapping_field(
    value: Any,
    field: str,
    name: str,
    index: int,
    path: Path,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SpecError(
            f"invalid value for {field!r} in {path}: item {index} "
            f"{name} must be an object"
        )
    return dict(value)


def schedule_tuple(value: Any, path: Path) -> tuple[ScheduleEntry, ...]:
    if value is None or value == ():
        return ()
    if not isinstance(value, list | tuple):
        raise SpecError(f"invalid value for 'schedules' in {path}: expected list")
    return tuple(schedule_entry(item, path) for item in value)


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
