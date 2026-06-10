"""System prompt construction for Zeta."""

from __future__ import annotations

from typing import Any, Iterable

from jinja2 import Environment, StrictUndefined

from ...protocols import SHELL_HANDOFF_RESULT_SCHEMA
from ..skills import Skill, available_skills
from ..tools import allowed_tool_names, model_tool_descriptors

PROMPT_TEMPLATE_ENV = Environment(
    autoescape=False,
    lstrip_blocks=True,
    trim_blocks=False,
    undefined=StrictUndefined,
)

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

When the run timeline contains a {SHELL_HANDOFF_RESULT_SCHEMA} result, treat it
as the source of truth for what happened after a shell handoff. If the outcome is
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

SYSTEM_PROMPT_TEMPLATE = """{{ base_prompt }}

{{ tool_protocol }}
{% if grep_tool_policy %}

Tool policy:

- {{ grep_tool_policy }}
{% endif %}
{% if skills_prompt %}

{{ skills_prompt }}
{% endif %}

{{ tools_prompt }}"""

TOOLS_PROMPT_TEMPLATE = """Available tools:{% if tools %}
{% for tool in tools %}- {{ tool.signature }}{% if tool.description %}: {{ tool.description }}{% endif %}
{% endfor %}{% else %}
(none){% endif %}"""

SKILLS_PROMPT_TEMPLATE = """{% if skills -%}
<available_skills>
When the task matches a skill description, use `read` to inspect that skill file.
Resolve relative skill references against the skill directory.
{% for skill in skills -%}
- name: {{ skill.name }}
  description: {{ skill.description }}
  location: {{ skill.location }}
{% endfor -%}
</available_skills>
{%- endif %}"""


def system_prompt(
    route_prompt: str | None = None,
    *,
    allowed_tools: Iterable[str] | None = None,
    skills: Iterable[Skill] | None = None,
) -> str:
    """Build the Zeta system prompt from the active tools and available skills."""
    active_tools = tuple(allowed_tool_names(allowed_tools))
    active_skills = (
        tuple(skills)
        if skills is not None
        else tuple(available_skills() if can_read_skill_files(active_tools) else ())
    )
    return render_system_prompt(
        route_prompt,
        allowed_tools=active_tools,
        skills=active_skills,
    )


def render_system_prompt(
    route_prompt: str | None = None,
    *,
    allowed_tools: Iterable[str] | None = None,
    skills: Iterable[Skill] = (),
) -> str:
    """Render the Zeta system prompt from already-resolved prompt inputs."""
    active_tools = tuple(allowed_tools) if allowed_tools is not None else None
    return render_prompt_template(
        SYSTEM_PROMPT_TEMPLATE,
        base_prompt=clean_prompt(route_prompt) or BASE_SYSTEM_PROMPT.strip(),
        tool_protocol=TOOL_PROTOCOL_PROMPT.strip(),
        grep_tool_policy=GREP_TOOL_POLICY
        if tool_available("grep", active_tools)
        else "",
        skills_prompt=skills_prompt(skills),
        tools_prompt=tools_prompt(active_tools),
    )


def can_read_skill_files(enabled_tools: Iterable[str]) -> bool:
    return "read" in set(enabled_tools)


def tool_available(name: str, allowed_tools: Iterable[str] | None = None) -> bool:
    for descriptor in model_tool_descriptors(allowed_tools):
        function = descriptor.get("function")
        if isinstance(function, dict) and function.get("name") == name:
            return True
    return False


def tools_prompt(allowed_tools: Iterable[str] | None = None) -> str:
    """Render active tools from the registry into the system prompt."""
    return render_prompt_template(
        TOOLS_PROMPT_TEMPLATE,
        tools=tool_prompt_items(allowed_tools),
    )


def tool_prompt_items(
    allowed_tools: Iterable[str] | None = None,
) -> list[dict[str, str]]:
    return [
        tool_prompt_item(descriptor)
        for descriptor in model_tool_descriptors(allowed_tools)
    ]


def tool_prompt_item(descriptor: dict[str, Any]) -> dict[str, str]:
    function = descriptor.get("function")
    if not isinstance(function, dict):
        return {"name": "unknown", "signature": "unknown()", "description": ""}
    name = str(function.get("name") or "unknown")
    description = str(function.get("description") or "").strip()
    parameters = function.get("parameters")
    schema = parameters if isinstance(parameters, dict) else {}
    return {
        "name": name,
        "signature": tool_signature(name, schema),
        "description": description,
    }


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
    return render_prompt_template(
        SKILLS_PROMPT_TEMPLATE,
        skills=skill_prompt_items(skills),
    )


def skill_prompt_items(skills: Iterable[Skill]) -> list[dict[str, str]]:
    return [
        {
            "name": skill.name,
            "description": skill.description,
            "location": str(skill.location),
        }
        for skill in skills
    ]


def clean_prompt(prompt: str | None) -> str:
    return (prompt or "").strip()


def render_prompt_template(template: str, **context: Any) -> str:
    return PROMPT_TEMPLATE_ENV.from_string(template).render(**context).strip()
