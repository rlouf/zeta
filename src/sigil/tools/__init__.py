"""Concrete Sigil tool implementations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from zeta.capabilities import (
    Capability,
    CapabilityFunction,
    CapabilityPolicy,
    CapabilitySpec,
    InProcessCapabilityExecutor,
)

from . import bash, edit, grep, ls, query_log, read, web, write

if TYPE_CHECKING:
    from zeta.capabilities import CapabilityRegistry

__all__ = ["ensure_builtin_tools_registered", "register_builtin_tools"]


def ensure_builtin_tools_registered() -> None:
    from zeta.capabilities import registry

    register_builtin_tools(registry)


def register_builtin_tools(registry: CapabilityRegistry) -> None:
    for capability in builtin_capabilities().values():
        if registry.get(capability.spec.id.canonical()) is None:
            registry.register(capability)


def builtin_capabilities() -> dict[str, Capability]:
    return {
        "sigil.bash": builtin_capability(bash.SPEC, bash.run, bash.stage),
        "sigil.ast_grep": builtin_capability(grep.AST_GREP_SPEC, grep.run_ast_grep),
        "sigil.edit": builtin_capability(edit.SPEC, edit.run, edit.stage),
        "sigil.grep": builtin_capability(grep.SPEC, grep.run),
        "sigil.ls": builtin_capability(ls.SPEC, ls.run),
        "sigil.query_log": builtin_capability(query_log.SPEC, query_log.run),
        "sigil.read": builtin_capability(read.SPEC, read.run),
        "sigil.web_search": builtin_capability(web.SEARCH_SPEC, web.search),
        "sigil.write": builtin_capability(write.SPEC, write.run, write.stage),
    }


def builtin_capability(
    spec: CapabilitySpec,
    run: CapabilityFunction,
    stage: CapabilityFunction | None = None,
) -> Capability:
    typed_stage = stage if callable(stage) else None
    return Capability(
        spec,
        CapabilityPolicy(
            supports_staging=typed_stage is not None,
            supports_direct=True,
            trust="host",
            timeout_seconds=DEFAULT_TIMEOUT_SECONDS_BY_ALIAS.get(
                spec.aliases[0] if spec.aliases else "",
            ),
        ),
        InProcessCapabilityExecutor(
            run,
            typed_stage,
        ),
    )


DEFAULT_TIMEOUT_SECONDS_BY_ALIAS = {"bash": bash.DEFAULT_TIMEOUT_SECONDS}
