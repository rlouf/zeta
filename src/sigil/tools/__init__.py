"""Concrete Sigil tool implementations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from zeta.tools.base import ToolImpl

from . import bash, edit, grep, ls, query_log, read, write

if TYPE_CHECKING:
    from zeta.tools.registry import ToolRegistry

__all__ = ["ensure_builtin_tools_registered", "register_builtin_tools"]


def ensure_builtin_tools_registered() -> None:
    from zeta.tools.registry import registry

    register_builtin_tools(registry)


def register_builtin_tools(registry: ToolRegistry) -> None:
    for name, tool in builtin_tools().items():
        if registry.get(name) is None:
            registry.register(name, tool)


def builtin_tools() -> dict[str, ToolImpl]:
    return {
        "bash": ToolImpl(bash.SPEC, bash.run, bash.stage),
        "edit": ToolImpl(edit.SPEC, edit.run, edit.stage),
        "grep": ToolImpl(grep.SPEC, grep.run),
        "ls": ToolImpl(ls.SPEC, ls.run),
        "query_log": ToolImpl(query_log.SPEC, query_log.run),
        "read": ToolImpl(read.SPEC, read.run),
        "write": ToolImpl(write.SPEC, write.run, write.stage),
    }
