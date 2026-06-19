"""Authored-agent resource loading hooks."""

from __future__ import annotations

from agents.spec import AgentSpec


def resource_extensions(spec: AgentSpec) -> dict[str, object]:
    """Return non-core frontmatter extensions for resource-aware hosts."""
    return dict(spec.extensions or {})
