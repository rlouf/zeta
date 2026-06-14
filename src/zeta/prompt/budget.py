"""Prompt component budget accounting."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass

from .components import PromptComponent


@dataclass(frozen=True)
class ComponentUsage:
    """Estimated token usage for one prompt component."""

    index: int
    kind: str
    representation: str
    object_id: str | None
    tokens: int


@dataclass(frozen=True)
class ContextUsage:
    """Estimated token usage for a prompt component list."""

    total_tokens: int
    components: tuple[ComponentUsage, ...]


def estimated_tokens_for_text(text: str) -> int:
    """Return a cheap, deterministic token estimate for a text payload."""
    return max(1, (len(text) + 3) // 4) if text else 0


def estimated_tokens(component: PromptComponent) -> int:
    """Return a cheap, deterministic token estimate for any prompt component."""
    return estimated_tokens_for_text(component_text(component))


def measure(components: Iterable[PromptComponent]) -> ContextUsage:
    """Return total and per-component approximate token usage."""
    breakdown = tuple(
        ComponentUsage(
            index=index,
            kind=component.kind,
            representation=component.representation,
            object_id=component.object_id,
            tokens=estimated_tokens(component),
        )
        for index, component in enumerate(components)
    )
    return ContextUsage(
        total_tokens=sum(component.tokens for component in breakdown),
        components=breakdown,
    )


def render_stub(component: PromptComponent) -> str:
    """Render the canonical elided-content stub."""
    object_id = component.source_object_id or component.object_id or "unknown"
    n_tokens = estimated_tokens(component)
    return (
        f"[elided {component.kind} {n_tokens}~tok id={object_id} "
        "\u2014 re-run the original tool call to recover this content]"
    )


def component_text(component: PromptComponent) -> str:
    if component.message is not None:
        content = component.message.get("content")
        if isinstance(content, str):
            return content
        return _compact_json(component.message)
    return _compact_json(component.data)


def _compact_json(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
