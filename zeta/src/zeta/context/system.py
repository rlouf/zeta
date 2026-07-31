"""System prompt construction for Zeta."""

import time
from collections.abc import Iterable
from typing import Any

from jinja2 import Environment, StrictUndefined

from zeta.capabilities.registry import CapabilityRegistry
from zeta.capabilities.registry import registry as _runtime_tool_registry

PROMPT_TEMPLATE_ENV = Environment(
    autoescape=False,
    lstrip_blocks=True,
    trim_blocks=False,
    undefined=StrictUndefined,
)


TOOL_PROTOCOL_PROMPT = """Tool protocol:

- Tools are native Chat Completions function tools exposed by the runtime.
- You may request multiple read-only tool calls in one turn when useful.
- Mutating tools apply their effects when you call them.
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
{{ tools_prompt }}"""

TOOLS_PROMPT_TEMPLATE = """Available tools:{% if tools %}
{% for tool in tools %}- {{ tool.signature }}{% if tool.description %}: {{ tool.description }}{% endif %}
{% endfor %}{% else %}
(none){% endif %}"""


def system_prompt(
    base_prompt: str | None = None,
    *,
    allowed_capabilities: Iterable[str] | None = None,
    tool_descriptors: Iterable[dict[str, Any]] | None = None,
) -> str:
    """Assemble the system prompt around the caller's base prompt.

    The base prompt belongs to the caller; this
    module only adds the runtime scaffolding: date line, tool protocol,
    and tool descriptors.
    """
    active_descriptors = (
        list(tool_descriptors) if tool_descriptors is not None else None
    )
    active_capabilities = (
        tuple(allowed_capabilities or ())
        if active_descriptors is not None
        else enabled_capability_ids(allowed_capabilities)
    )
    return render_system_prompt(
        base_prompt,
        allowed_capabilities=active_capabilities,
        tool_descriptors=active_descriptors,
    )


def render_system_prompt(
    base_prompt: str | None = None,
    *,
    allowed_capabilities: Iterable[str] | None = None,
    tool_descriptors: Iterable[dict[str, Any]] | None = None,
) -> str:
    """Render the system prompt from already-resolved prompt inputs."""
    active_capabilities = (
        tuple(allowed_capabilities) if allowed_capabilities is not None else None
    )
    active_descriptors = (
        list(tool_descriptors)
        if tool_descriptors is not None
        else model_capability_descriptors(active_capabilities)
    )
    return render_prompt_template(
        SYSTEM_PROMPT_TEMPLATE,
        base_prompt=clean_prompt(base_prompt),
        date_line=current_date_line(),
        tool_protocol=TOOL_PROTOCOL_PROMPT.strip(),
        grep_tool_policy=GREP_TOOL_POLICY
        if capability_available("grep", tool_descriptors=active_descriptors)
        else "",
        tools_prompt=tools_prompt(tool_descriptors=active_descriptors),
    )


def current_date_line() -> str:
    """State today's date so relative time references resolve correctly.

    Date only, never time of day: the system prompt is a content-addressed
    trace component, and a finer stamp would defeat its deduplication.
    """
    return time.strftime("Today is %Y-%m-%d (%A).", time.localtime())


def capability_available(
    name: str,
    allowed_capabilities: Iterable[str] | None = None,
    *,
    tool_descriptors: Iterable[dict[str, Any]] | None = None,
) -> bool:
    descriptors = (
        tool_descriptors
        if tool_descriptors is not None
        else model_capability_descriptors(allowed_capabilities)
    )
    for descriptor in descriptors:
        function = descriptor.get("function")
        if isinstance(function, dict) and function.get("name") == name:
            return True
    return False


def tools_prompt(
    allowed_capabilities: Iterable[str] | None = None,
    *,
    tool_descriptors: Iterable[dict[str, Any]] | None = None,
) -> str:
    """Render active capabilities from the registry into the system prompt."""
    return render_prompt_template(
        TOOLS_PROMPT_TEMPLATE,
        tools=tool_prompt_items(
            allowed_capabilities,
            tool_descriptors=tool_descriptors,
        ),
    )


def tool_prompt_items(
    allowed_capabilities: Iterable[str] | None = None,
    *,
    tool_descriptors: Iterable[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    descriptors = (
        tool_descriptors
        if tool_descriptors is not None
        else model_capability_descriptors(allowed_capabilities)
    )
    return [tool_prompt_item(descriptor) for descriptor in descriptors]


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


def clean_prompt(prompt: str | None) -> str:
    return (prompt or "").strip()


def render_prompt_template(template: str, **context: Any) -> str:
    return PROMPT_TEMPLATE_ENV.from_string(template).render(**context).strip()


tool_registry = _runtime_tool_registry


def enabled_capability_ids(
    allowed_capabilities: Iterable[str] | None,
    *,
    tool_registry: CapabilityRegistry | None = None,
) -> tuple[str, ...]:
    active_tool_registry = tool_registry or _runtime_tool_registry
    if allowed_capabilities is None:
        return tuple(active_tool_registry.list_auto_enabled_capability_ids())
    available = active_tool_registry.list_capability_ids()
    enabled = []
    for name in allowed_capabilities:
        capability_id = active_tool_registry.resolve(name)
        if capability_id is not None and capability_id in available:
            enabled.append(capability_id)
    return tuple(enabled)


def model_capability_descriptors(
    allowed_capabilities: Iterable[str] | None,
    *,
    tool_registry: CapabilityRegistry | None = None,
) -> list[dict[str, Any]]:
    """Return provider-facing tool descriptors for the model prompt."""
    active_tool_registry = tool_registry or _runtime_tool_registry
    enabled_ids = enabled_capability_ids(
        allowed_capabilities,
        tool_registry=active_tool_registry,
    )
    return active_tool_registry.model_tool_schema(enabled_ids).descriptors
