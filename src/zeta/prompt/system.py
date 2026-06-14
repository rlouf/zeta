"""System prompt construction for Zeta."""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

from jinja2 import Environment, StrictUndefined

from ..skills import Skill, available_skills
from ..tools.registry import registry as tool_registry

PROMPT_TEMPLATE_ENV = Environment(
    autoescape=False,
    lstrip_blocks=True,
    trim_blocks=False,
    undefined=StrictUndefined,
)

TOOL_PROTOCOL_PROMPT = """Tool protocol:

- Tools are native Chat Completions function tools exposed by the runtime.
- You may request multiple read-only tool calls in one turn when useful.
- Some workflows stage mutating tool calls for review; some apply them directly.
- For staged effects, use one mutating tool at a time and then stop.
- Use a tool only when its schema matches the needed action.
- Do not mention unavailable tools.
- If no tool is needed, return a final answer.
"""

GREP_TOOL_POLICY = (
    "Use `grep` to locate occurrences before reading files when the target "
    "text/symbol is known."
)

SYSTEM_PROMPT_TEMPLATE = """{{ base_prompt }}

{{ date_line }}

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
    base_prompt: str | None = None,
    *,
    allowed_tools: Iterable[str] | None = None,
    skills: Iterable[Skill] | None = None,
) -> str:
    """Assemble the system prompt around the caller's base prompt.

    The base prompt is workflow content and belongs to the caller; this
    module only adds the runtime scaffolding: date line, tool protocol,
    skills, and tool descriptors.
    """
    active_tools = enabled_tool_names(allowed_tools)
    active_skills = (
        tuple(skills)
        if skills is not None
        else tuple(available_skills() if can_read_skill_files(active_tools) else ())
    )
    return render_system_prompt(
        base_prompt,
        allowed_tools=active_tools,
        skills=active_skills,
    )


def render_system_prompt(
    base_prompt: str | None = None,
    *,
    allowed_tools: Iterable[str] | None = None,
    skills: Iterable[Skill] = (),
) -> str:
    """Render the system prompt from already-resolved prompt inputs."""
    active_tools = tuple(allowed_tools) if allowed_tools is not None else None
    return render_prompt_template(
        SYSTEM_PROMPT_TEMPLATE,
        base_prompt=clean_prompt(base_prompt),
        date_line=current_date_line(),
        tool_protocol=TOOL_PROTOCOL_PROMPT.strip(),
        grep_tool_policy=GREP_TOOL_POLICY
        if tool_available("grep", active_tools)
        else "",
        skills_prompt=skills_prompt(skills),
        tools_prompt=tools_prompt(active_tools),
    )


def current_date_line() -> str:
    """State today's date so relative time references resolve correctly.

    Date only, never time of day: the system prompt is a content-addressed
    trace component, and a finer stamp would defeat its deduplication.
    """
    return time.strftime("Today is %Y-%m-%d (%A).", time.localtime())


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


def enabled_tool_names(allowed_tools: Iterable[str] | None) -> tuple[str, ...]:
    available = tool_registry.list_tool_names()
    if allowed_tools is None:
        return tuple(available)
    allowed = set(allowed_tools)
    return tuple(name for name in available if name in allowed)


def model_tool_descriptors(
    allowed_tools: Iterable[str] | None,
) -> list[dict[str, Any]]:
    """Return provider-facing tool descriptors for the model prompt."""
    descriptors = []
    for name in enabled_tool_names(allowed_tools):
        tool = tool_registry.get(name)
        if tool is None:
            continue
        descriptors.append(
            {
                "type": "function",
                "function": {
                    "name": tool.spec.name,
                    "description": tool.spec.description,
                    "parameters": tool.spec.schema,
                },
            }
        )
    return descriptors
