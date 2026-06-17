"""Prompt construction APIs for Zeta."""

import logging
import os
from collections.abc import Mapping

from .budget import (
    ComponentUsage,
    ContextUsage,
    estimated_tokens,
    estimated_tokens_for_text,
    measure,
    render_stub,
)
from .builder import (
    PreparedPrompt,
    PromptBuilder,
    PromptPlan,
    ReconstructedPrompt,
    StoredPrompt,
    commit_prompt_plan,
    payload_sha256,
    plan_prompt,
    reconstructed_prompt_request,
    render_model_input,
)
from .compaction import (
    TASK_STATE_SCHEMA,
    DropOldestPromptTransform,
    ModelTaskStateExtractor,
    StructuralTrimPromptTransform,
    TaskStateExtractionPromptTransform,
    TaskStateExtractor,
    task_state_component,
    task_state_extraction_messages,
    task_state_json,
    task_state_message,
)
from .components import (
    TIMELINE_TAIL_LIMIT,
    PromptComponent,
    Representation,
    component_messages,
    prompt_component_object,
    prompt_components,
    zeta_context_message,
)
from .system import (
    GREP_TOOL_POLICY,
    TOOL_PROTOCOL_PROMPT,
    can_read_skill_files,
    clean_prompt,
    render_system_prompt,
    skills_prompt,
    system_prompt,
    tool_signature,
    tools_prompt,
)
from .system import (
    capability_available as capability_available,
)
from .transforms import (
    BudgetThresholdPromptTransform,
    NoOpPromptTransform,
    PromptTransform,
)

DEFAULT_TRIM_THRESHOLD_TOKENS = 100_000
LOGGER = logging.getLogger("zeta.prompt")


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
            threshold,
            escalation=(
                TaskStateExtractionPromptTransform(),
                DropOldestPromptTransform(max_tokens=threshold),
            ),
        )
    if mode == "task_state":
        return BudgetThresholdPromptTransform(
            TaskStateExtractionPromptTransform(),
            threshold,
            escalation=(DropOldestPromptTransform(max_tokens=threshold),),
        )
    LOGGER.warning(
        "unknown ZETA_TRIM mode %r; compaction disabled "
        "(expected off, structural, or task_state)",
        mode,
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
    "BudgetThresholdPromptTransform",
    "ComponentUsage",
    "ContextUsage",
    "DEFAULT_TRIM_THRESHOLD_TOKENS",
    "DropOldestPromptTransform",
    "GREP_TOOL_POLICY",
    "ModelTaskStateExtractor",
    "NoOpPromptTransform",
    "PreparedPrompt",
    "PromptPlan",
    "PromptBuilder",
    "PromptComponent",
    "PromptTransform",
    "ReconstructedPrompt",
    "Representation",
    "StructuralTrimPromptTransform",
    "TASK_STATE_SCHEMA",
    "TOOL_PROTOCOL_PROMPT",
    "TIMELINE_TAIL_LIMIT",
    "TaskStateExtractionPromptTransform",
    "TaskStateExtractor",
    "can_read_skill_files",
    "clean_prompt",
    "component_messages",
    "commit_prompt_plan",
    "estimated_tokens",
    "estimated_tokens_for_text",
    "measure",
    "plan_prompt",
    "prompt_component_object",
    "prompt_transform_from_env",
    "prompt_components",
    "payload_sha256",
    "reconstructed_prompt_request",
    "render_model_input",
    "render_system_prompt",
    "render_stub",
    "skills_prompt",
    "StoredPrompt",
    "system_prompt",
    "task_state_component",
    "task_state_extraction_messages",
    "task_state_json",
    "task_state_message",
    "tool_available",
    "tool_signature",
    "tools_prompt",
    "zeta_context_message",
]
