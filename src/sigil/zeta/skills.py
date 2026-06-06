"""Skill discovery and prompt expansion for Zeta."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

SKILL_FILE = "SKILL.md"
SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9-]+$")
SKILL_DIRECTIVE_PATTERN = re.compile(r"^\s*@([a-z0-9-]+):(?:[ \t]*([\s\S]*))?$")
SKIP_DIRECTORIES = {"node_modules"}


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    location: Path
    body: str
    disable_model_invocation: bool = False


@dataclass(frozen=True)
class SkillDiagnostic:
    path: Path
    message: str


@dataclass(frozen=True)
class SkillCatalog:
    skills: dict[str, Skill] = field(default_factory=dict)
    diagnostics: list[SkillDiagnostic] = field(default_factory=list)


def discover_skills(cwd: str | Path | None = None) -> SkillCatalog:
    """Discover Zeta skills from user and project skill roots."""
    current = Path(cwd or os.getcwd()).resolve()
    discovered: dict[str, Skill] = {}
    diagnostics: list[SkillDiagnostic] = []
    seen_paths: set[Path] = set()
    for root in _skill_search_roots(current):
        for skill_root in _skill_roots(root):
            try:
                resolved = skill_root.resolve()
            except OSError as exc:
                diagnostics.append(SkillDiagnostic(skill_root, str(exc)))
                continue
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            skill, diagnostic = load_skill(resolved)
            if diagnostic is not None:
                diagnostics.append(diagnostic)
                continue
            if skill is not None:
                discovered[skill.name] = skill
    return SkillCatalog(skills=discovered, diagnostics=diagnostics)


def available_skills(cwd: str | Path | None = None) -> list[Skill]:
    """Return skills that may be advertised to the model."""
    catalog = discover_skills(cwd)
    return [
        skill
        for skill in sorted(catalog.skills.values(), key=lambda item: item.name)
        if not skill.disable_model_invocation
    ]


def expand_skill_directive(objective: str, cwd: str | Path | None = None) -> str:
    """Expand one leading ``@skill-name:`` directive when the skill is known."""
    match = SKILL_DIRECTIVE_PATTERN.match(objective)
    if match is None:
        return objective
    name = match.group(1)
    task = match.group(2) or ""
    skill = discover_skills(cwd).skills.get(name)
    if skill is None:
        return objective
    return "\n".join(
        [
            f'<skill name="{skill.name}" location="{skill.location}">',
            f"References are relative to {skill.location}.",
            "",
            skill.body.strip(),
            "</skill>",
            "",
            task,
        ]
    )


def load_skill(path: Path) -> tuple[Skill | None, SkillDiagnostic | None]:
    """Load one skill root containing ``SKILL.md``."""
    skill_file = path / SKILL_FILE
    try:
        text = skill_file.read_text(encoding="utf-8")
    except OSError as exc:
        return None, SkillDiagnostic(skill_file, str(exc))
    metadata, body = _split_frontmatter(text)
    name = str(metadata.get("name") or path.name).strip()
    description = str(metadata.get("description") or "").strip()
    if not SKILL_NAME_PATTERN.fullmatch(name):
        return None, SkillDiagnostic(
            skill_file,
            f"invalid skill name {name!r}: use lowercase letters, digits, and hyphens",
        )
    if not description:
        return None, SkillDiagnostic(skill_file, "missing non-empty description")
    return (
        Skill(
            name=name,
            description=description,
            location=path,
            body=body,
            disable_model_invocation=_metadata_bool(
                metadata.get("disable-model-invocation")
            ),
        ),
        None,
    )


def _skill_search_roots(current: Path) -> list[Path]:
    project_roots = [
        directory / ".agents" / "skills"
        for directory in [*reversed(current.parents), current]
    ]
    return [
        Path.home() / ".zeta" / "skills",
        Path.home() / ".agents" / "skills",
        *project_roots,
    ]


def _skill_roots(root: Path) -> list[Path]:
    if not root.exists():
        return []
    skill_roots: list[Path] = []
    pending = [root]
    seen_directories: set[Path] = set()
    while pending:
        directory = pending.pop()
        try:
            resolved = directory.resolve()
        except OSError:
            continue
        if resolved in seen_directories:
            continue
        seen_directories.add(resolved)
        if (directory / SKILL_FILE).is_file():
            skill_roots.append(directory)
            continue
        try:
            children = sorted(directory.iterdir(), key=lambda path: path.name)
        except OSError:
            continue
        pending.extend(
            reversed(
                [
                    child
                    for child in children
                    if child.is_dir()
                    and child.name not in SKIP_DIRECTORIES
                    and not child.name.startswith(".")
                ]
            )
        )
    return skill_roots


def _split_frontmatter(text: str) -> tuple[dict[str, object], str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() != "---":
            continue
        metadata = _parse_metadata("".join(lines[1:index]))
        return metadata, "".join(lines[index + 1 :])
    return {}, text


def _parse_metadata(text: str) -> dict[str, object]:
    metadata: dict[str, object] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        metadata[key] = _parse_scalar(value)
    return metadata


def _parse_scalar(value: str) -> object:
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _metadata_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False
