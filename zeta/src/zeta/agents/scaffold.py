"""Scaffold a new authored agent file from a template.

Deterministic (no model call): fills a skeleton with valid frontmatter and a
prompt body that references only ``event``, validates it through ``load_spec``,
and writes ``agents/<slug>.md``.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterable
from pathlib import Path

import yaml

from zeta.agents.spec import SLUG_PATTERN, SpecError, load_spec

DEFAULT_TOOLS = ("read", "grep", "edit", "write")

_BODY = """\
{{ event.payload }}

TODO: describe what this agent should do with the event above, and which files
(relative to base_dir) it should read or write.
"""


class ScaffoldError(ValueError):
    """Raised when a new agent cannot be scaffolded."""


def slug_to_name(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").title()


def render_agent_markdown(
    *,
    name: str,
    description: str,
    accepts: Iterable[str] = (),
    tools: Iterable[str] = (),
    skills: Iterable[str] = (),
    base_dir: str | None = None,
) -> str:
    frontmatter: dict[str, object] = {"name": name, "description": description}
    if base_dir:
        frontmatter["base_dir"] = base_dir
    accepts_list = list(accepts)
    if accepts_list:
        frontmatter["accepts"] = accepts_list
    frontmatter["tools"] = list(tools) or list(DEFAULT_TOOLS)
    skills_list = list(skills)
    if skills_list:
        frontmatter["skills"] = skills_list
    dumped = yaml.dump(frontmatter, sort_keys=False, allow_unicode=True).rstrip()
    return f"---\n{dumped}\n---\n{_BODY}"


def scaffold_agent(
    project_root: Path,
    slug: str,
    *,
    name: str | None = None,
    description: str | None = None,
    accepts: Iterable[str] = (),
    tools: Iterable[str] = (),
    skills: Iterable[str] = (),
    base_dir: str | None = None,
    overwrite: bool = False,
) -> Path:
    if not SLUG_PATTERN.match(slug):
        raise ScaffoldError(
            f"invalid agent slug {slug!r}: use lowercase letters, digits, '-' or '_'"
        )
    content = render_agent_markdown(
        name=name or slug_to_name(slug),
        description=description or f"TODO: describe the {slug} agent.",
        accepts=accepts,
        tools=tools,
        skills=skills,
        base_dir=base_dir,
    )
    _validate(content, slug)
    path = project_root / "agents" / f"{slug}.md"
    if path.exists() and not overwrite:
        raise ScaffoldError(f"agent already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _validate(content: str, slug: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / f"{slug}.md"
        probe.write_text(content, encoding="utf-8")
        try:
            load_spec(probe)
        except SpecError as exc:
            raise ScaffoldError(f"generated agent is invalid: {exc}") from exc
