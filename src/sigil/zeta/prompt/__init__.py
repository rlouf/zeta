"""Prompt construction APIs for Zeta."""

import os
from collections.abc import Mapping

from .budget import (
    ComponentUsage,
    ContextBudget,
    ContextUsage,
    estimated_tokens,
    estimated_tokens_for_text,
    measure,
    render_stub,
)
from .builder import PreparedPrompt, PromptBuilder
from .components import (
    PromptComponent,
    Representation,
    can_read_skill_files,
    component_messages,
    prompt_component_object,
    prompt_components,
    update_component_refs,
    zeta_context_message,
)
from .system import (
    BASE_SYSTEM_PROMPT,
    GREP_TOOL_POLICY,
    TOOL_PROTOCOL_PROMPT,
    clean_prompt,
    skills_prompt,
    system_prompt,
    tool_available,
    tool_signature,
    tools_prompt,
)
from .structural_trim import StructuralTrimPromptTransform
from .task_state import (
    ModelTaskStateExtractor,
    TASK_STATE_SCHEMA,
    TaskStateExtractionPromptTransform,
    TaskStateExtractor,
    task_state_component,
    task_state_extraction_messages,
    task_state_json,
    task_state_message,
)
from .transforms import (
    BudgetThresholdPromptTransform,
    ChainedTransform,
    NoOpPromptTransform,
    PromptTransform,
)

DEFAULT_TRIM_THRESHOLD_TOKENS = 100_000


def prompt_transform_from_env(
    env: Mapping[str, str] | None = None,
) -> PromptTransform:
    """Build the configured prompt transform, preserving no-op default behavior."""
    values = os.environ if env is None else env
    mode = values.get("ZETA_TRIM", "off").strip().lower()
    threshold = trim_threshold_tokens(values)
    if mode in ("", "off"):
        return NoOpPromptTransform()
    if mode == "structural":
        return BudgetThresholdPromptTransform(
            StructuralTrimPromptTransform(),
            ContextBudget(threshold),
        )
    if mode == "task_state":
        return BudgetThresholdPromptTransform(
            TaskStateExtractionPromptTransform(),
            ContextBudget(threshold),
        )
    return NoOpPromptTransform()


def trim_threshold_tokens(env: Mapping[str, str]) -> int:
    value = env.get("ZETA_TRIM_THRESHOLD_TOKENS", "")
    if not value.strip():
        return DEFAULT_TRIM_THRESHOLD_TOKENS
    try:
        return max(0, int(value))
    except ValueError:
        return DEFAULT_TRIM_THRESHOLD_TOKENS


__all__ = [
    "BASE_SYSTEM_PROMPT",
    "BudgetThresholdPromptTransform",
    "ChainedTransform",
    "ComponentUsage",
    "ContextBudget",
    "ContextUsage",
    "DEFAULT_TRIM_THRESHOLD_TOKENS",
    "GREP_TOOL_POLICY",
    "ModelTaskStateExtractor",
    "NoOpPromptTransform",
    "PreparedPrompt",
    "PromptBuilder",
    "PromptComponent",
    "PromptTransform",
    "Representation",
    "StructuralTrimPromptTransform",
    "TASK_STATE_SCHEMA",
    "TOOL_PROTOCOL_PROMPT",
    "TaskStateExtractionPromptTransform",
    "TaskStateExtractor",
    "can_read_skill_files",
    "clean_prompt",
    "component_messages",
    "estimated_tokens",
    "estimated_tokens_for_text",
    "measure",
    "prompt_component_object",
    "prompt_transform_from_env",
    "prompt_components",
    "render_stub",
    "skills_prompt",
    "system_prompt",
    "task_state_component",
    "task_state_extraction_messages",
    "task_state_json",
    "task_state_message",
    "tool_available",
    "tool_signature",
    "tools_prompt",
    "update_component_refs",
    "zeta_context_message",
]
