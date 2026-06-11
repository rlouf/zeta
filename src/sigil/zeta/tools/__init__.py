"""Registry for built-in and plugin Zeta tools."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from . import bash, edit, grep, ls, query_log, read, write
from .base import ToolImpl, ToolSpec, error_result
from .plugins import load_cli_plugins, user_tools_config_path

ExecutionMode = Literal["handoff", "direct"]

BUILTIN_TOOL_IMPLS: dict[str, ToolImpl] = {
    bash.SPEC.name: ToolImpl(bash.SPEC, bash.run, bash.stage),
    edit.SPEC.name: ToolImpl(edit.SPEC, edit.run, edit.stage),
    grep.SPEC.name: ToolImpl(grep.SPEC, grep.run),
    ls.SPEC.name: ToolImpl(ls.SPEC, ls.run),
    query_log.SPEC.name: ToolImpl(query_log.SPEC, query_log.run),
    read.SPEC.name: ToolImpl(read.SPEC, read.run),
    write.SPEC.name: ToolImpl(write.SPEC, write.run, write.stage),
}

BUILTIN_TOOL_SPECS: dict[str, ToolSpec] = {
    name: tool.spec for name, tool in BUILTIN_TOOL_IMPLS.items()
}

TOOL_IMPLS: dict[str, ToolImpl] = dict(BUILTIN_TOOL_IMPLS)
TOOL_SPECS: dict[str, ToolSpec] = {name: tool.spec for name, tool in TOOL_IMPLS.items()}
TOOL_ORIGINS: dict[str, dict[str, str]] = {
    name: {"origin": "builtin"} for name in BUILTIN_TOOL_IMPLS
}

_REGISTRY_CACHE_KEY: tuple[Path, int | None] | None = None
_REGISTRY_DIAGNOSTICS: list[dict[str, str]] = []


def ensure_registry_loaded() -> None:
    """Refresh plugin tools when the user config path or mtime changes."""
    global _REGISTRY_CACHE_KEY, _REGISTRY_DIAGNOSTICS
    config_path = user_tools_config_path()
    cache_key = (config_path, config_mtime_ns(config_path))
    if _REGISTRY_CACHE_KEY == cache_key:
        return
    loaded = load_cli_plugins(set(BUILTIN_TOOL_IMPLS), config_path=config_path)
    TOOL_IMPLS.clear()
    TOOL_IMPLS.update(BUILTIN_TOOL_IMPLS)
    TOOL_SPECS.clear()
    TOOL_SPECS.update(BUILTIN_TOOL_SPECS)
    TOOL_ORIGINS.clear()
    TOOL_ORIGINS.update({name: {"origin": "builtin"} for name in BUILTIN_TOOL_IMPLS})
    for name, plugin in loaded.tools.items():
        TOOL_IMPLS[name] = ToolImpl(plugin.spec, plugin.run)
        TOOL_SPECS[name] = plugin.spec
        TOOL_ORIGINS[name] = {"origin": "plugin", "plugin": plugin.label}
    _REGISTRY_DIAGNOSTICS = loaded.diagnostics
    _REGISTRY_CACHE_KEY = cache_key


def config_mtime_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def tool_metadata(name: str) -> dict[str, Any]:
    ensure_registry_loaded()
    spec = TOOL_SPECS.get(name)
    if spec is None:
        raise KeyError(name)
    return spec.metadata()


def allowed_tool_names(allowed_tools: Iterable[str] | None = None) -> list[str]:
    ensure_registry_loaded()
    allowed = set(allowed_tools) if allowed_tools is not None else None
    return [name for name in sorted(TOOL_SPECS) if allowed is None or name in allowed]


def tools_list(allowed_tools: Iterable[str] | None = None) -> dict[str, Any]:
    tools = []
    for name in allowed_tool_names(allowed_tools):
        meta = tool_metadata(name)
        meta.update(TOOL_ORIGINS.get(name, {"origin": "builtin"}))
        tools.append(meta)
    result: dict[str, Any] = {"tools": tools}
    if _REGISTRY_DIAGNOSTICS:
        result["diagnostics"] = _REGISTRY_DIAGNOSTICS
    return result


def model_tool_descriptors(
    allowed_tools: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Return provider-facing tool descriptors for the model prompt."""
    descriptors = []
    for name in allowed_tool_names(allowed_tools):
        spec = TOOL_SPECS[name]
        descriptors.append(
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.schema,
                },
            }
        )
    return descriptors


def validate_tool_args(name: str, params: dict[str, Any]) -> list[str]:
    """Validate params against the tool's JSON Schema."""
    ensure_registry_loaded()
    spec = TOOL_SPECS.get(name)
    if spec is None:
        return [f"unknown tool: {name}"]
    try:
        validator = Draft202012Validator(spec.schema)
    except SchemaError as exc:
        return [f"invalid schema for tool {name}: {exc.message}"]
    errors = sorted(validator.iter_errors(params), key=validation_error_sort_key)
    return [format_validation_error(error) for error in errors]


def validation_error_sort_key(error: ValidationError) -> tuple[str, str]:
    return (json_path(error.absolute_path), error.message)


def format_validation_error(error: ValidationError) -> str:
    return f"{json_path(error.absolute_path)}: {error.message}"


def json_path(parts: Any) -> str:
    path = "$"
    for part in parts:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}"
    return path


def run_tool(
    name: str,
    params: dict[str, Any],
    *,
    execution_mode: ExecutionMode = "handoff",
) -> dict[str, Any]:
    """Run one tool call under the staging contract its spec declares.

    Read-only tools always run. Mutating tools run in direct mode; in
    handoff mode they stage their work for review, and a mutating tool
    without a staging implementation (any plugin) is refused.
    """
    ensure_registry_loaded()
    tool = TOOL_IMPLS.get(name)
    if tool is None:
        return error_result("unknown-tool", f"unknown tool: {name}")
    if execution_mode == "direct" or not tool.spec.mutates():
        return tool.run(params)
    if tool.stage is None:
        declared = ", ".join(tool.spec.effects) or "undeclared"
        return error_result(
            "staging-unsupported",
            f"tool {name} has effects ({declared}) that cannot be staged "
            "for review; rerun in the do workflow (,,,)",
        )
    return tool.stage(params)
