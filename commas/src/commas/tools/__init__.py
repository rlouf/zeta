"""Commas tool registration and compatibility re-exports."""

from __future__ import annotations

from typing import TYPE_CHECKING

from zeta.tools import bash, builtin_capability, edit, grep, ls, read, web, write
from zeta.tools import builtin_capabilities as zeta_builtin_capabilities

from commas.tools import query_log

if TYPE_CHECKING:
    from zeta.capabilities.registry import CapabilityRegistry


def ensure_builtin_tools_registered() -> None:
    from zeta.capabilities.registry import registry

    register_builtin_tools(registry)


def register_builtin_tools(registry: CapabilityRegistry) -> None:
    for capability in builtin_capabilities().values():
        if registry.get(capability.declaration.id.canonical()) is None:
            registry.register(capability)


def builtin_capabilities():
    capabilities = dict(zeta_builtin_capabilities())
    capabilities["zeta.query_log"] = builtin_capability(query_log.SPEC, query_log.run)
    return capabilities


__all__ = [
    "bash",
    "builtin_capabilities",
    "builtin_capability",
    "edit",
    "ensure_builtin_tools_registered",
    "grep",
    "ls",
    "query_log",
    "read",
    "register_builtin_tools",
    "web",
    "write",
]
