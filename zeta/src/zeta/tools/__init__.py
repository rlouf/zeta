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

QUERY_CONTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "key_prefix": {"type": "string"},
        "kind": {"type": "string"},
        "source_scope": {"enum": ["run", "session", "agent"]},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        "cursor": {"type": "integer", "minimum": 0},
    },
}

QUERY_CONTENT_SPEC = Capability(
    CapabilityId("zeta", "query_content"),
    (
        "Query the current content workspace. The result contains stable object "
        "references and bounded previews."
    ),
    QUERY_CONTENT_SCHEMA,
)

TRANSFORM_CONTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "expected_head",
        "reason",
        "inputs",
        "transformation",
        "destination",
    ],
    "properties": {
        "expected_head": {"type": "string", "minLength": 1},
        "reason": {"type": "string", "minLength": 1},
        "inputs": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "uniqueItems": True,
                },
                "kind": {"type": "string"},
                "source_scope": {"enum": ["run", "session", "agent"]},
            },
        },
        "transformation": {
            "type": "object",
            "required": ["type"],
            "properties": {
                "type": {
                    "enum": [
                        "literal",
                        "patch",
                        "drop",
                        "identity",
                        "model",
                        "python",
                    ]
                },
                "value": {},
                "title": {"type": "string"},
                "attributes": {"type": "object"},
                "patch": {"type": "object"},
                "instruction": {"type": "string", "minLength": 1},
                "mode": {"enum": ["one", "map", "reduce"]},
                "max_concurrency": {"type": "integer", "minimum": 1, "maximum": 8},
                "source": {"type": "string", "minLength": 1, "maxLength": 131072},
                "timeout_seconds": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "maximum": 300,
                },
            },
        },
        "destination": {
            "type": "object",
            "additionalProperties": False,
            "required": ["scope", "expected_object_id"],
            "properties": {
                "key": {"type": "string", "minLength": 1},
                "kind": {"type": "string", "minLength": 1},
                "scope": {"enum": ["run", "session", "agent"]},
                "expected_object_id": {"type": ["string", "null"]},
            },
        },
    },
}

TRANSFORM_CONTENT_SPEC = Capability(
    CapabilityId("zeta", "transform_content"),
    (
        "Create a new content revision. Use an explicit run, session, or agent "
        "destination. Durable changes become active only after the run succeeds."
    ),
    TRANSFORM_CONTENT_SCHEMA,
)

FINISH_SPEC = Capability(
    CapabilityId("zeta", "finish"),
    (
        "Select one object from the current content graph as the final answer. "
        "Use this when copying the complete value into a model message is wasteful."
    ),
    {
        "type": "object",
        "additionalProperties": False,
        "required": ["object_id"],
        "properties": {"object_id": {"type": "string", "minLength": 1}},
    },
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
        "zeta.finish": builtin_capability(FINISH_SPEC, content_workspace_unavailable),
        "zeta.patch": builtin_capability(edit.PATCH_SPEC, edit.run_patch),
        "zeta.query_content": builtin_capability(
            QUERY_CONTENT_SPEC,
            content_workspace_unavailable,
        ),
        "zeta.query_log": builtin_capability(
            QUERY_LOG_SPEC,
            query_log_unavailable,
        ),
        "zeta.transform_content": builtin_capability(
            TRANSFORM_CONTENT_SPEC,
            content_workspace_unavailable,
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


def content_workspace_unavailable(_params: dict[str, Any]) -> dict[str, Any]:
    """Refuse content changes when no run owns a content workspace."""
    return {
        "ok": False,
        "error": {
            "code": "content-workspace-unavailable",
            "message": "content tools are unavailable outside a Zeta run",
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
