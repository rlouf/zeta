"""Token estimation shared by prompt budgeting and trace summaries."""

from __future__ import annotations


def estimated_tokens_for_text(text: str) -> int:
    """Return a cheap, deterministic token estimate for a text payload."""
    return max(1, (len(text) + 3) // 4) if text else 0
