"""Shared prompt transform contracts for Zeta."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .budget import ContextBudget, measure
from .components import PromptComponent


class PromptTransform(Protocol):
    """Transform prompt components before the final model payload is built."""

    def apply(self, components: list[PromptComponent]) -> list[PromptComponent]: ...


class NoOpPromptTransform:
    """Default prompt transform that preserves current runtime behavior."""

    def apply(self, components: list[PromptComponent]) -> list[PromptComponent]:
        return list(components)


@dataclass(frozen=True)
class ChainedTransform:
    """Apply prompt transforms in order."""

    transforms: tuple[PromptTransform, ...]

    def apply(self, components: list[PromptComponent]) -> list[PromptComponent]:
        output = list(components)
        for transform in self.transforms:
            output = transform.apply(output)
        return output


@dataclass(frozen=True)
class BudgetThresholdPromptTransform:
    """Run a transform only after measurement exceeds a threshold."""

    transform: PromptTransform
    budget: ContextBudget

    @property
    def producer(self) -> str:
        return str(getattr(self.transform, "producer", "") or "")

    def apply(self, components: list[PromptComponent]) -> list[PromptComponent]:
        if measure(components).total_tokens <= self.budget.max_tokens:
            return list(components)
        return self.transform.apply(components)
