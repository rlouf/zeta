"""Concrete built-in tool implementations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from zeta.capabilities.execution import (
    CapabilityFunction,
    InProcessCapabilityExecutor,
)
from zeta.capabilities.registry import RegisteredCapability
from zeta.capabilities.types import Capability
from zeta.tools import bash, edit, grep, ls, read, web, write

if TYPE_CHECKING:
    from zeta.capabilities.registry import CapabilityRegistry

__all__ = ["ensure_builtin_tools_registered", "register_builtin_tools"]


def ensure_builtin_tools_registered() -> None:
    from zeta.capabilities.registry import registry

    register_builtin_tools(registry)


def register_builtin_tools(registry: CapabilityRegistry) -> None:
    for capability in builtin_capabilities().values():
        if registry.get(capability.declaration.id.canonical()) is None:
            registry.register(capability)


def builtin_capabilities() -> dict[str, RegisteredCapability]:
    return {
        "zeta.bash": builtin_capability(bash.SPEC, bash.run, bash.stage),
        "zeta.ast_grep": builtin_capability(grep.AST_GREP_SPEC, grep.run_ast_grep),
        "zeta.edit": builtin_capability(edit.SPEC, edit.run, edit.stage),
        "zeta.grep": builtin_capability(grep.SPEC, grep.run),
        "zeta.ls": builtin_capability(ls.SPEC, ls.run),
        "zeta.read": builtin_capability(read.SPEC, read.run),
        "zeta.web_search": builtin_capability(web.SEARCH_SPEC, web.search),
        "zeta.write": builtin_capability(write.SPEC, write.run, write.stage),
    }


def builtin_capability(
    declaration: Capability,
    run: CapabilityFunction,
    stage: CapabilityFunction | None = None,
) -> RegisteredCapability:
    typed_stage = stage if callable(stage) else None
    return RegisteredCapability(
        declaration,
        InProcessCapabilityExecutor(
            run,
            typed_stage,
        ),
    )
