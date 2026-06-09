"""Shared prompt transform contracts for Zeta."""

from __future__ import annotations

from typing import Protocol

from .components import PromptComponent


class PromptTransform(Protocol):
    """Transform prompt components before the final model payload is built."""

    def apply(self, components: list[PromptComponent]) -> list[PromptComponent]: ...


class NoOpPromptTransform:
    """Default prompt transform that preserves current runtime behavior."""

    def apply(self, components: list[PromptComponent]) -> list[PromptComponent]:
        return list(components)
