"""Prompt compaction transform modules."""

from dataclasses import dataclass
from typing import Literal

CompactionStrategy = Literal["structural_trim", "drop_oldest"]


@dataclass(frozen=True)
class CompactionPolicy:
    """Select how model-facing working memory is bounded for one turn."""

    strategy: CompactionStrategy = "structural_trim"
    max_context_tokens: int | None = None
