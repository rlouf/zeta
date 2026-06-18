"""Shared prompt transform contracts for Zeta."""

import logging
from dataclasses import dataclass
from typing import Protocol

from .budget import ContextUsage, measure
from .components import PromptComponent

LOGGER = logging.getLogger("zeta.context")
_warned_over_budget = False


class PromptTransform(Protocol):
    """Transform prompt components before the final model payload is built."""

    def apply(self, components: list[PromptComponent]) -> list[PromptComponent]: ...


class NoOpPromptTransform:
    """Default prompt transform that preserves current runtime behavior."""

    def apply(self, components: list[PromptComponent]) -> list[PromptComponent]:
        return list(components)


@dataclass(frozen=True)
class BudgetThresholdPromptTransform:
    """Run a transform once measurement exceeds a threshold, then re-measure.

    Each escalation transform runs only while the prompt is still over
    budget; when the whole ladder is exhausted the overflow is signalled
    loudly instead of shipped silently.
    """

    transform: PromptTransform
    max_tokens: int
    escalation: tuple[PromptTransform, ...] = ()

    @property
    def producer(self) -> str:
        return str(getattr(self.transform, "producer", "") or "")

    def apply(self, components: list[PromptComponent]) -> list[PromptComponent]:
        if measure(components).total_tokens <= self.max_tokens:
            return list(components)
        output = self.transform.apply(components)
        for transform in self.escalation:
            if measure(output).total_tokens <= self.max_tokens:
                return output
            output = transform.apply(output)
        usage = measure(output)
        if usage.total_tokens > self.max_tokens:
            warn_over_budget(usage, self.max_tokens)
        return output


def warn_over_budget(usage: ContextUsage, max_tokens: int) -> None:
    """Signal once per process that compaction could not reach the budget."""
    global _warned_over_budget
    if _warned_over_budget:
        return
    _warned_over_budget = True
    largest = max(usage.components, key=lambda component: component.tokens)
    LOGGER.warning(
        "prompt still over budget after compaction: ~%d tokens > %d budget "
        "(largest component: %s ~%d tokens)",
        usage.total_tokens,
        max_tokens,
        largest.kind,
        largest.tokens,
    )


def reset_over_budget_warning() -> None:
    """Re-arm the once-per-process over-budget warning."""
    global _warned_over_budget
    _warned_over_budget = False
