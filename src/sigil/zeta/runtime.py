"""Zeta runtime services used by Sigil routes."""

from __future__ import annotations

from typing import Iterable

from .context import load_project_context  # noqa: F401  (route-facing surface)
from .prompt import can_read_skill_files, system_prompt
from .skills import available_skills, expand_skill_directive  # noqa: F401
from .tools import allowed_tool_names
from .transcript import append_transcript, transcript_tail  # noqa: F401


def zeta_system_prompt(
    route_prompt: str | None = None,
    *,
    allowed_tools: Iterable[str] | None = None,
) -> str:
    """Compose the system prompt from route text, enabled tools, and skills."""
    enabled_tools = allowed_tool_names(allowed_tools)
    skills = available_skills() if can_read_skill_files(enabled_tools) else []
    return system_prompt(route_prompt, allowed_tools=enabled_tools, skills=skills)
