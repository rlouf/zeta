"""Authored agent spec data structures and frontmatter parsing."""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from connectors import EgressBinding, IngressBinding

from zeta.addresses import content_address

SLUG_PATTERN = re.compile(r"^[a-z0-9_-]+$")
MASTER_AGENT_ID = "zeta.master"
SESSION_MESSAGE_REQUESTED = "session.message.requested"
BUILT_IN_FRONTMATTER_KEYS = frozenset(
    {
        "name",
        "description",
        "enabled",
        "session",
        "model",
        "executor",
        "accepts",
        "publishes",
        "returns",
        "skills",
        "tools",
        "schedules",
        "retry",
        "base_dir",
    }
)
_JSON_INTEGER_MIN = -(1 << 63)
_JSON_INTEGER_MAX = (1 << 64) - 1
_YAML_BOOL_PATTERN = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")
_YAML_INT_PATTERN = re.compile(
    r"^(?:[-+]?0b[0-1]+|[-+]?0o[0-7]+|[-+]?0x[0-9a-fA-F]+|[-+]?(?:0|[1-9][0-9]*))$"
)
_YAML_FLOAT_PATTERN = re.compile(
    r"^(?:"
    r"[-+]?(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?"
    r"|[-+]?[0-9]+[eE][-+]?[0-9]+"
    r"|[-+]?\.(?:inf|Inf|INF)"
    r"|\.(?:nan|NaN|NAN)"
    r")$"
)


class _AuthoringLoader(yaml.SafeLoader):
    """Keep authored scalar semantics independent of PyYAML's YAML 1.1 defaults."""

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in seen
                seen.add(key)
            except TypeError as exc:
                raise yaml.constructor.ConstructorError(
                    None,
                    None,
                    "found an unhashable object key",
                    key_node.start_mark,
                ) from exc
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    None,
                    None,
                    f"found duplicate key {key!r}",
                    key_node.start_mark,
                )
        return super().construct_mapping(node, deep=deep)


_REMOVED_YAML_TAGS = {
    "tag:yaml.org,2002:bool",
    "tag:yaml.org,2002:float",
    "tag:yaml.org,2002:int",
    "tag:yaml.org,2002:merge",
    "tag:yaml.org,2002:timestamp",
}
_AuthoringLoader.yaml_implicit_resolvers = {
    first: [
        (tag, pattern) for tag, pattern in resolvers if tag not in _REMOVED_YAML_TAGS
    ]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _construct_yaml_int(loader: _AuthoringLoader, node: Any) -> int:
    return int(loader.construct_scalar(node), 0)


def _construct_yaml_float(loader: _AuthoringLoader, node: Any) -> float | str:
    source = loader.construct_scalar(node)
    value = yaml.constructor.SafeConstructor.construct_yaml_float(loader, node)
    if not math.isfinite(value) and source.lower().lstrip("+-") not in {
        ".inf",
        ".nan",
    }:
        return source
    return value


_AuthoringLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    _YAML_BOOL_PATTERN,
    list("tTfF"),
)
_AuthoringLoader.add_implicit_resolver(
    "tag:yaml.org,2002:int",
    _YAML_INT_PATTERN,
    list("-+0123456789"),
)
_AuthoringLoader.add_implicit_resolver(
    "tag:yaml.org,2002:float",
    _YAML_FLOAT_PATTERN,
    list("-+0123456789."),
)
_AuthoringLoader.add_constructor("tag:yaml.org,2002:int", _construct_yaml_int)
_AuthoringLoader.add_constructor("tag:yaml.org,2002:float", _construct_yaml_float)


@dataclass(frozen=True)
class ScheduleEntry:
    """Structural schedule declaration for an authored agent."""

    cron: str
    timezone: str | None = None
    catchup: str | None = None


@dataclass(frozen=True)
class ModelSpec:
    """Named model profile for one authored agent."""

    profile: str


@dataclass(frozen=True)
class RetrySpec:
    """Per-agent retry policy override from authored frontmatter."""

    max_attempts: int | None = None
    backoff_seconds: float | None = None


@dataclass(frozen=True)
class ExecutorSpec:
    """Tool executor selection for one authored agent."""

    provider: str = "local"
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentSpec:
    """Parsed authored agent specification."""

    slug: str
    name: str
    description: str
    instructions: str
    path: Path
    content_address: str
    enabled: bool = True
    session: str = "per-event"
    model: ModelSpec | None = None
    executor: ExecutorSpec = field(default_factory=ExecutorSpec)
    accepts: tuple[str, ...] = ()
    publishes: tuple[str, ...] = ()
    returns: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    skills_inherit: bool = True
    tools: tuple[str, ...] = ()
    tools_inherit: bool = True
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
        publishes, egress = publishes_entries(
            frontmatter.get("publishes", ()),
            path,
        )
        returns = string_tuple(frontmatter.get("returns", ()), "returns", path)
        return AgentSpec(
            slug=slug,
            name=required_string(frontmatter, "name", path),
            description=required_string(frontmatter, "description", path),
            instructions=instructions,
            path=relative_to_cwd(path),
            content_address=content_address(raw_bytes),
            enabled=bool_field(frontmatter.get("enabled", True), "enabled", path),
            session=session_field(frontmatter.get("session"), path),
            model=model_spec(frontmatter.get("model"), path),
            executor=executor_spec(frontmatter.get("executor"), path),
            accepts=accepts,
            publishes=publishes,
            returns=returns,
            skills=string_tuple(frontmatter.get("skills", ()), "skills", path),
            skills_inherit="skills" not in frontmatter,
            tools=string_tuple(frontmatter.get("tools", ()), "tools", path),
            tools_inherit="tools" not in frontmatter,
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
            raw = yaml.load(frontmatter_text, Loader=_AuthoringLoader)
        except yaml.YAMLError as exc:
            raise SpecError(f"invalid YAML frontmatter in {path}: {exc}") from exc
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise SpecError(f"invalid YAML frontmatter in {path}: expected object")
        try:
            normalized = _json_value(raw, set())
        except ValueError as exc:
            raise SpecError(f"invalid YAML frontmatter in {path}: {exc}") from exc
        if not isinstance(normalized, dict):
            raise SpecError(f"invalid YAML frontmatter in {path}: expected object")
        return normalized, body
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
    if not isinstance(value, str) or value == "":
        raise SpecError(f"invalid value for 'model' in {path}: expected profile name")
    return ModelSpec(profile=value)


def executor_spec(value: Any, path: Path) -> ExecutorSpec:
    if value is None:
        return ExecutorSpec()
    if not isinstance(value, Mapping):
        raise SpecError(f"invalid value for 'executor' in {path}: expected object")
    unknown = sorted(set(value) - {"provider", "config"})
    if unknown:
        raise SpecError(
            f"invalid value for 'executor' in {path}: unsupported field {unknown[0]!r}"
        )
    provider = value.get("provider")
    if not isinstance(provider, str) or provider == "":
        raise SpecError(
            f"invalid value for 'executor' in {path}: provider must be a non-empty string"
        )
    config = value.get("config", {})
    if not isinstance(config, Mapping):
        raise SpecError(
            f"invalid value for 'executor' in {path}: config must be an object"
        )
    try:
        normalized_config = executor_config(config)
    except ValueError as exc:
        raise SpecError(
            f"invalid value for 'executor' in {path}: config must contain only "
            "JSON values with string object keys"
        ) from exc
    return ExecutorSpec(provider=provider, config=normalized_config)


def executor_config(value: Any) -> dict[str, Any]:
    """Normalize config for stable snapshots and executor cache keys."""
    if not isinstance(value, Mapping):
        raise ValueError("executor config must be an object")
    normalized = _json_value(value, set())
    if not isinstance(normalized, dict):
        raise ValueError("executor config must be an object")
    return normalized


def _json_value(value: Any, active: set[int]) -> Any:
    if value is None or isinstance(value, str | bool):
        return value
    if isinstance(value, int):
        if not _JSON_INTEGER_MIN <= value <= _JSON_INTEGER_MAX:
            raise ValueError("integers must fit i64 or u64")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("numbers must be finite")
        return value
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise ValueError("values must not contain cycles")
        if not all(isinstance(key, str) for key in value):
            raise ValueError("object keys must be strings")
        if "<<" in value:
            raise ValueError("merge keys are not supported")
        active.add(identity)
        try:
            return {key: _json_value(item, active) for key, item in value.items()}
        finally:
            active.remove(identity)
    if isinstance(value, list):
        identity = id(value)
        if identity in active:
            raise ValueError("values must not contain cycles")
        active.add(identity)
        try:
            return [_json_value(item, active) for item in value]
        finally:
            active.remove(identity)
    raise ValueError("frontmatter contains a non-JSON value")


def base_dir_field(value: Any, path: Path) -> Path | None:
    """Preserve authored paths so declarations stay portable across machines."""
    if value is None:
        return None
    if not isinstance(value, str) or value.strip() == "":
        raise SpecError(
            f"invalid value for 'base_dir' in {path}: expected a path string"
        )
    return Path(value)


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


def publishes_entries(
    value: Any, path: Path
) -> tuple[tuple[str, ...], tuple[EgressBinding, ...]]:
    if value is None or value == ():
        return (), ()
    if not isinstance(value, list | tuple):
        raise SpecError(f"invalid value for 'publishes' in {path}: expected list")
    events: list[str] = []
    bindings: list[EgressBinding] = []
    for index, item in enumerate(value):
        if isinstance(item, str) and item:
            events.append(item)
            continue
        entry = event_entry(item, "publishes", index, path)
        event = required_event(entry, "publishes", index, path)
        events.append(event)
        bindings.append(
            EgressBinding(
                event=event,
                options=mapping_field(
                    entry.get("with", {}),
                    "publishes",
                    "with",
                    index,
                    path,
                ),
                idempotency_key=optional_string_field(
                    entry.get("idempotency_key"),
                    "publishes",
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
    if field == "publishes" and "filter" in value:
        raise SpecError(
            f"invalid value for 'publishes' in {path}: item {index} must use "
            "'with' for published event options"
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
    catchup = schedule_catchup(value.get("catchup"), path)
    return ScheduleEntry(
        cron=cron,
        timezone=timezone,
        catchup=catchup,
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


def schedule_catchup(value: Any, path: Path) -> str | None:
    if value is None:
        return None
    if value != "latest":
        raise SpecError(
            f"invalid value for 'schedules' in {path}: catchup must be 'latest'"
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


def session_field(value: Any, path: Path) -> str:
    """Return the validated session rule for one authored agent.

    `shared` means the agent identifies the session. `per-event` means the
    triggering event does. Any template identifies it by a value the event
    carries, such as `{chat_id}`.
    """
    if value is None:
        return "per-event"
    if not isinstance(value, str) or not value:
        raise SpecError(f"invalid value for 'session' in {path}: expected a string")
    if value in {"shared", "per-event"} or "{" in value:
        return value
    raise SpecError(
        f"invalid value for 'session' in {path}: {value!r} is not 'shared', "
        "'per-event', or a template such as '{chat_id}'"
    )
