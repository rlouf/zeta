"""Concrete built-in tool implementations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from zeta.capabilities.executors import CapabilityFunction, InProcessCapabilityExecutor
from zeta.capabilities.registry import RegisteredCapability
from zeta.capabilities.types import Capability, CapabilityId
from zeta.tools import bash, edit, grep, ls, read, web, write

if TYPE_CHECKING:
    from zeta.capabilities.registry import CapabilityRegistry

__all__ = ["ensure_builtin_tools_registered", "register_builtin_tools"]

QUERY_LOG_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "since": {
            "type": "string",
            "description": (
                "Only runs at or after YYYY-MM-DD, or an age like 2d, 6h, or 30m."
            ),
        },
        "failed": {
            "type": "boolean",
            "description": "Only failed or aborted runs.",
        },
        "run_id": {
            "type": "string",
            "description": "Expand one prior run by full id or unique id prefix.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 50,
            "description": "Maximum number of prior runs to list.",
        },
    },
}

QUERY_LOG_SPEC = Capability(
    CapabilityId("zeta", "query_log"),
    (
        "Query prior model runs in the current authorized session. Use it to "
        "recover earlier decisions, outcomes, tool activity, and prompt trace ids. "
        "Cite the returned run ids when relying on history."
    ),
    QUERY_LOG_SCHEMA,
)


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
        "zeta.patch": builtin_capability(edit.PATCH_SPEC, edit.run_patch),
        "zeta.query_log": builtin_capability(
            QUERY_LOG_SPEC,
            query_log_unavailable,
        ),
        "zeta.grep": builtin_capability(grep.SPEC, grep.run),
        "zeta.ls": builtin_capability(ls.SPEC, ls.run),
        "zeta.read": builtin_capability(read.SPEC, read.run),
        "zeta.web_search": builtin_capability(web.SEARCH_SPEC, web.search),
        "zeta.write": builtin_capability(write.SPEC, write.run),
    }


def query_log_unavailable(_params: dict[str, Any]) -> dict[str, Any]:
    """Refuse history access unless the runtime supplies an authorized reader."""
    return {
        "ok": False,
        "error": {
            "code": "query-log-unavailable",
            "message": "query_log is unavailable outside a durable runtime session",
        },
    }


def builtin_capability(
    declaration: Capability,
    run: CapabilityFunction,
) -> RegisteredCapability:
    return RegisteredCapability(
        declaration,
        InProcessCapabilityExecutor(run),
    )
