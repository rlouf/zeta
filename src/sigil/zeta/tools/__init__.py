"""Registry for built-in and plugin Zeta tools."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from . import bash, edit, grep, ls, read, write
from .base import ToolImpl, ToolSpec, diagnostic, error_result
from .plugins import load_cli_plugins, user_tools_config_path

ExecutionMode = Literal["handoff", "direct"]

BUILTIN_TOOL_IMPLS: dict[str, ToolImpl] = {
    bash.SPEC.name: ToolImpl(bash.SPEC, bash.analyze, bash.run),
    edit.SPEC.name: ToolImpl(edit.SPEC, edit.analyze, edit.run),
    grep.SPEC.name: ToolImpl(grep.SPEC, grep.analyze, grep.run),
    ls.SPEC.name: ToolImpl(ls.SPEC, ls.analyze, ls.run),
    read.SPEC.name: ToolImpl(read.SPEC, read.analyze, read.run),
    write.SPEC.name: ToolImpl(write.SPEC, write.analyze, write.run),
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
        TOOL_IMPLS[name] = ToolImpl(plugin.spec, plugin.analyze, plugin.run)
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


def analyze_tool(name: str, params: dict[str, Any]) -> dict[str, Any]:
    ensure_registry_loaded()
    tool = TOOL_IMPLS.get(name)
    if tool is None:
        return {
            "valid": False,
            "resolved": False,
            "effects": [],
            "diagnostics": [
                diagnostic("unknown-tool", f"unknown tool: {name}", severity="error")
            ],
        }
    return tool.analyze(params)


def run_tool(
    name: str,
    params: dict[str, Any],
    *,
    edit_mode: str = "review_patch",
    execution_mode: ExecutionMode = "handoff",
) -> dict[str, Any]:
    ensure_registry_loaded()
    tool = TOOL_IMPLS.get(name)
    if tool is None:
        return error_result("unknown-tool", f"unknown tool: {name}")
    if name == bash.SPEC.name and execution_mode == "direct":
        return bash.run_direct(params)
    if name == write.SPEC.name and execution_mode == "direct":
        return write.run_direct(params)
    if name == edit.SPEC.name and (
        edit_mode == "direct_replace" or execution_mode == "direct"
    ):
        return edit.run_direct(params)
    return tool.run(params)
