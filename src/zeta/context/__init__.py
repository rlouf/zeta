"""Prompt transform configuration for Zeta."""

import logging
import os
from collections.abc import Mapping

from zeta.context.compaction import (
    DropOldestPromptTransform,
    StructuralTrimPromptTransform,
    TaskStateExtractionPromptTransform,
)
from zeta.context.transforms import (
    BudgetThresholdPromptTransform,
    NoOpPromptTransform,
    PromptTransform,
)
from zeta.run.config import CompactionPolicy

DEFAULT_TRIM_THRESHOLD_TOKENS = 100_000
LOGGER = logging.getLogger("zeta.context")


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


def prompt_transform_from_policy(policy: CompactionPolicy | None) -> PromptTransform:
    if policy is None:
        return prompt_transform_from_env()
    max_tokens = policy.max_context_tokens
    if max_tokens is None:
        max_tokens = DEFAULT_TRIM_THRESHOLD_TOKENS
    if policy.strategy == "structural_trim":
        return BudgetThresholdPromptTransform(
            StructuralTrimPromptTransform(),
            max_tokens,
            escalation=(DropOldestPromptTransform(max_tokens=max_tokens),),
        )
    if policy.strategy == "drop_oldest":
        return BudgetThresholdPromptTransform(
            DropOldestPromptTransform(max_tokens=max_tokens),
            max_tokens,
        )
    LOGGER.warning(
        "unknown compaction strategy %r; compaction disabled", policy.strategy
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
    "DEFAULT_TRIM_THRESHOLD_TOKENS",
    "prompt_transform_from_env",
    "prompt_transform_from_policy",
]
