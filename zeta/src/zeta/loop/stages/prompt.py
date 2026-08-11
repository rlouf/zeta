"""Build the prompt for one turn.

This stage resolves which capabilities the agent may use and assembles the
prompt components the model will see.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from zeta.capabilities.registry import (
    CapabilityRegistry,
)
from zeta.capabilities.registry import registry as _runtime_tool_registry
from zeta.context.builder import (
    PreparedPrompt,
    PromptBuilder,
    PromptEnvironment,
    prepared_prompt_from,
    render_model_input,
)
from zeta.context.components import PromptComponent
from zeta.events import Event
from zeta.journal.views import (
    event_view,
)
from zeta.loop.config import AgentConfig
from zeta.loop.outcomes import (
    RunState,
)
from zeta.loop.types import (
    TimelineEvent,
)
from zeta.models.types import ModelInput


def registered_capabilities(
    allowed_capabilities: Iterable[str] | None,
    *,
    tool_registry: CapabilityRegistry | None = None,
) -> tuple[str, ...]:
    """Filter to registered capabilities, preserving the caller's order."""
    active_tool_registry = tool_registry or _runtime_tool_registry
    if allowed_capabilities is None:
        return tuple(active_tool_registry.list_auto_enabled_capability_ids())
    enabled: list[str] = []
    seen: set[str] = set()
    for name in allowed_capabilities:
        if name.startswith("mcp.") and name.endswith(".*") and name.count(".") == 2:
            capability_ids = (
                capability_id
                for capability_id in active_tool_registry.list_capability_ids()
                if capability_id.startswith(name[:-1])
            )
        else:
            capability_id = active_tool_registry.resolve(name)
            capability_ids = () if capability_id is None else (capability_id,)
        for capability_id in capability_ids:
            if capability_id not in seen:
                enabled.append(capability_id)
                seen.add(capability_id)
    return tuple(enabled)


def agent_allowed_capabilities(
    config: AgentConfig,
    *,
    tool_registry: CapabilityRegistry | None = None,
) -> tuple[str, ...]:
    return registered_capabilities(
        config.allowed_capabilities,
        tool_registry=tool_registry,
    )


async def build_prompt_step(
    objective: str,
    timeline: Sequence[TimelineEvent],
    *,
    config: AgentConfig,
    allowed_capabilities: tuple[str, ...],
    context: str,
    current_events: Iterable[dict[str, Any]],
    content_components: Iterable[PromptComponent] = (),
    tools: list[dict[str, Any]],
    state: RunState,
    builder: PromptBuilder,
    environment: PromptEnvironment,
) -> tuple[PreparedPrompt, ModelInput]:
    state.note_step("build_prompt")
    prompt_plan = builder.plan_prompt(
        objective,
        [
            event_view(event) if isinstance(event, Event) else dict(event)
            for event in timeline
        ],
        system=config.system_prompt,
        allowed_capabilities=allowed_capabilities,
        context=context,
        current_events=current_events,
        content_components=content_components,
        tools=tools,
        tool_choice="auto",
        selected_model=config.model_name,
        thinking=config.thinking,
        environment=environment,
    )
    stored_prompt = await builder.commit_prompt_plan(prompt_plan)
    model_input = render_model_input(stored_prompt)
    prepared_prompt = prepared_prompt_from(stored_prompt, model_input=model_input)
    return prepared_prompt, model_input
