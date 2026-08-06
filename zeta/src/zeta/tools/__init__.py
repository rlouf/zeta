"""Concrete built-in tool implementations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from zeta.capabilities.executors import CapabilityFunction, InProcessCapabilityExecutor
from zeta.capabilities.registry import RegisteredCapability
from zeta.capabilities.types import Capability
from zeta.tools import bash, content, edit, grep, history, ls, read, web, write

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
        "zeta.bash": builtin_capability(bash.SPEC, bash.run),
        "zeta.ast_grep": builtin_capability(grep.AST_GREP_SPEC, grep.run_ast_grep),
        "zeta.edit": builtin_capability(edit.SPEC, edit.run),
        "zeta.finish": builtin_capability(
            content.FINISH_SPEC,
            content.content_workspace_unavailable,
        ),
        "zeta.patch": builtin_capability(edit.PATCH_SPEC, edit.run_patch),
        "zeta.query_content": builtin_capability(
            content.QUERY_CONTENT_SPEC,
            content.content_workspace_unavailable,
        ),
        "zeta.query_context_budget": builtin_capability(
            history.QUERY_CONTEXT_BUDGET_SPEC,
            history.query_context_budget_unavailable,
        ),
        "zeta.query_log": builtin_capability(
            history.QUERY_LOG_SPEC,
            history.query_log_unavailable,
        ),
        "zeta.transform_content": builtin_capability(
            content.TRANSFORM_CONTENT_SPEC,
            content.content_workspace_unavailable,
        ),
        "zeta.grep": builtin_capability(grep.SPEC, grep.run),
        "zeta.ls": builtin_capability(ls.SPEC, ls.run),
        "zeta.read": builtin_capability(read.SPEC, read.run),
        "zeta.web_search": builtin_capability(web.SEARCH_SPEC, web.search),
        "zeta.write": builtin_capability(write.SPEC, write.run),
    }


def builtin_capability(
    declaration: Capability,
    run: CapabilityFunction,
) -> RegisteredCapability:
    return RegisteredCapability(
        declaration,
        InProcessCapabilityExecutor(run),
    )
