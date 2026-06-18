"""Authored agent spec data structures and frontmatter parsing."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
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
        "tools",
        "schedules",
    }
)


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
            raise SpecError(f"invalid value for {field!r}: expected strings")
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
        raise SpecError(f"invalid value for 'schedules' in {path}: {field} is required")
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
