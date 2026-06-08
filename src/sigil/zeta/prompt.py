"""System prompt construction for Zeta."""

from __future__ import annotations

from typing import Any, Iterable

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

Preserve user changes. Do not overwrite files you did not inspect. Avoid
destructive commands unless explicitly requested. Do not commit unless asked.
After direct mutations, run focused verification when practical; if verification
is skipped, say so.

Project context is ordered from broad to local; later, more local instructions
override earlier ones when they conflict.

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

GREP_TOOL_POLICY = (
    "Use `grep` to locate occurrences before reading files when the target "
    "text/symbol is known."
)


def system_prompt(
    route_prompt: str | None = None,
    *,
    allowed_tools: Iterable[str] | None = None,
    skills: Iterable[Skill] = (),
) -> str:
    """Build the Zeta system prompt with the active tool registry."""
    active_tools = tuple(allowed_tools) if allowed_tools is not None else None
    sections = [clean_prompt(route_prompt) or BASE_SYSTEM_PROMPT.strip()]
    sections.append(TOOL_PROTOCOL_PROMPT.strip())
    if tool_available("grep", active_tools):
        sections.append(f"Tool policy:\n\n- {GREP_TOOL_POLICY}")
    skill_section = skills_prompt(skills)
    if skill_section:
        sections.append(skill_section)
    sections.append(tools_prompt(active_tools))
    return "\n\n".join(sections)


def tool_available(name: str, allowed_tools: Iterable[str] | None = None) -> bool:
    for descriptor in model_tool_descriptors(allowed_tools):
        function = descriptor.get("function")
        if isinstance(function, dict) and function.get("name") == name:
            return True
    return False


def tools_prompt(allowed_tools: Iterable[str] | None = None) -> str:
    """Render active tools from the registry into the system prompt."""
    descriptors = model_tool_descriptors(allowed_tools)
    lines = ["Available tools:"]
    if not descriptors:
        lines.append("(none)")
        return "\n".join(lines)
    lines.extend(tool_prompt_line(descriptor) for descriptor in descriptors)
    return "\n".join(lines)


def tool_prompt_line(descriptor: dict[str, Any]) -> str:
    function = descriptor.get("function")
    if not isinstance(function, dict):
        return "- unknown()"
    name = str(function.get("name") or "unknown")
    description = str(function.get("description") or "").strip()
    parameters = function.get("parameters")
    schema = parameters if isinstance(parameters, dict) else {}
    signature = tool_signature(name, schema)
    if not description:
        return f"- {signature}"
    return f"- {signature}: {description}"


def tool_signature(name: str, schema: dict[str, Any]) -> str:
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return f"{name}()"
    raw_required = schema.get("required")
    required = (
        {item for item in raw_required if isinstance(item, str)}
        if isinstance(raw_required, list)
        else set()
    )
    args = [
        property_name
        for property_name in properties
        if isinstance(property_name, str) and property_name in required
    ]
    args.extend(
        f"{property_name}?"
        for property_name in properties
        if isinstance(property_name, str) and property_name not in required
    )
    return f"{name}({', '.join(args)})"


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
