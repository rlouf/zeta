"""System prompt construction for Zeta."""

from __future__ import annotations

import json
from typing import Iterable

from ..protocols import SHELL_HANDOFF_RESULT_SCHEMA
from .skills import Skill
from .tools import model_tool_descriptors

BASE_SYSTEM_PROMPT = f"""You are Zeta, a shell-native coding agent.

You participate in the user's live shell session. The shell owns control flow,
current working directory, environment, history, job control, and command
handoff. You choose the next small action and then stop.

Work concretely from the available context. Prefer inspection before edits. Use
read-only tools for local context. Follow the active route instructions for
whether commands and mutations are staged for review or run directly. Keep
answers concise and do not invent command output, file contents, or tool
results.

When the transcript contains a {SHELL_HANDOFF_RESULT_SCHEMA} result, treat it as
the source of truth for what happened after a shell handoff. If the outcome is
cancelled, do not assume the proposed command ran; use the recorded shell_turns
as user-chosen context and explain the cancellation plainly if it matters.
"""

TOOL_PROTOCOL_PROMPT = """Tool protocol:

- Tools are native Chat Completions function tools exposed by the shell loop.
- You may request multiple read-only tool calls in one turn when useful.
- Some routes stage bash, edit, or write as handoffs; some run them directly.
- For staged handoffs, use one handoff tool at a time and then stop.
- Use a tool only when its schema matches the needed action.
- Do not mention unavailable tools.
- If no tool is needed, return a final answer.
"""


def system_prompt(
    route_prompt: str | None = None,
    *,
    allowed_tools: Iterable[str] | None = None,
    skills: Iterable[Skill] = (),
) -> str:
    """Build the Zeta system prompt with the active tool registry."""
    sections = [clean_prompt(route_prompt) or BASE_SYSTEM_PROMPT.strip()]
    sections.append(TOOL_PROTOCOL_PROMPT.strip())
    skill_section = skills_prompt(skills)
    if skill_section:
        sections.append(skill_section)
    sections.append(tools_prompt(allowed_tools))
    return "\n\n".join(sections)


def tools_prompt(allowed_tools: Iterable[str] | None = None) -> str:
    """Render active tools from the registry into the system prompt."""
    descriptors = model_tool_descriptors(allowed_tools)
    return "\n".join(
        [
            "Available tools with input JSON Schemas:",
            json.dumps(descriptors, ensure_ascii=False, separators=(",", ":")),
        ]
    )


def skills_prompt(skills: Iterable[Skill]) -> str:
    """Render discoverable skills into the system prompt."""
    items = list(skills)
    if not items:
        return ""
    lines = [
        "<available_skills>",
        "Use `read` to load a skill when the task matches its description.",
        "Resolve relative skill references against the skill directory.",
    ]
    for skill in items:
        lines.extend(
            [
                f"- name: {skill.name}",
                f"  description: {skill.description}",
                f"  location: {skill.location}",
            ]
        )
    lines.append("</available_skills>")
    return "\n".join(lines)


def clean_prompt(prompt: str | None) -> str:
    return (prompt or "").strip()
