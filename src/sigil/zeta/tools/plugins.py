"""CLI-backed Zeta tool plugins."""

from __future__ import annotations

import json
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .base import EFFECT_KINDS, ToolSpec, diagnostic, error_result

DEFAULT_TIMEOUT_MS = 30_000
MAX_STDERR_LENGTH = 2_000

PluginMode = Literal["metadata", "run"]


@dataclass(frozen=True)
class CliPluginTool:
    """A Zeta tool implemented by a configured command."""

    spec: ToolSpec
    command: tuple[str, ...]
    timeout_ms: int

    @property
    def label(self) -> str:
        return self.command[0]

    def run(self, params: dict[str, Any]) -> dict[str, Any]:
        result = run_plugin_json(
            self.command,
            self.timeout_ms,
            mode="run",
            params=params,
        )
        if result.ok and isinstance(result.value, dict):
            return result.value
        if result.ok:
            result = PluginJsonResult(
                False,
                code="plugin-run-invalid-result",
                message="plugin result JSON must be an object",
            )
        return error_result(result.code, result.message)


@dataclass(frozen=True)
class PluginLoadResult:
    tools: dict[str, CliPluginTool]
    diagnostics: list[dict[str, str]]


@dataclass(frozen=True)
class PluginJsonResult:
    ok: bool
    value: Any = None
    code: str = ""
    message: str = ""


def user_tools_config_path() -> Path:
    return Path.home() / ".zeta" / "tools.toml"


def load_cli_plugins(
    builtin_names: set[str],
    *,
    config_path: Path | None = None,
) -> PluginLoadResult:
    path = config_path or user_tools_config_path()
    if not path.exists():
        return PluginLoadResult({}, [])
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return PluginLoadResult(
            {},
            [
                diagnostic(
                    "plugin-config-invalid",
                    f"could not read {path}: {exc}",
                    severity="error",
                )
            ],
        )
    raw_tools = config.get("tools", [])
    if not isinstance(raw_tools, list):
        return PluginLoadResult(
            {},
            [
                diagnostic(
                    "plugin-config-invalid",
                    "tools must be an array of tables",
                    severity="error",
                )
            ],
        )
    plugins: dict[str, CliPluginTool] = {}
    diagnostics: list[dict[str, str]] = []
    for index, entry in enumerate(raw_tools):
        loaded, entry_diagnostics = load_cli_plugin_entry(entry, index)
        diagnostics.extend(entry_diagnostics)
        if loaded is None:
            continue
        name = loaded.spec.name
        if name in builtin_names:
            diagnostics.append(
                diagnostic(
                    "plugin-name-collision",
                    f"plugin tool {name} conflicts with a built-in tool and was ignored",
                    severity="error",
                )
            )
            continue
        if name in plugins:
            diagnostics.append(
                diagnostic(
                    "plugin-name-collision",
                    f"plugin tool {name} was already registered and was ignored",
                    severity="error",
                )
            )
            continue
        plugins[name] = loaded
    return PluginLoadResult(plugins, diagnostics)


def load_cli_plugin_entry(
    entry: Any,
    index: int,
) -> tuple[CliPluginTool | None, list[dict[str, str]]]:
    if not isinstance(entry, dict):
        return None, [entry_diagnostic(index, "tool entry must be a table")]
    command = entry.get("command")
    if not valid_command(command):
        return None, [
            entry_diagnostic(index, "command must be a non-empty string array")
        ]
    timeout_ms = entry.get("timeout_ms", DEFAULT_TIMEOUT_MS)
    if not isinstance(timeout_ms, int) or timeout_ms <= 0:
        return None, [entry_diagnostic(index, "timeout_ms must be a positive integer")]
    command_tuple = tuple(command)
    metadata = run_plugin_json(command_tuple, timeout_ms, mode="metadata")
    if not metadata.ok:
        return None, [diagnostic(metadata.code, metadata.message, severity="error")]
    spec, message = tool_spec_from_metadata(metadata.value)
    if spec is None:
        return None, [diagnostic("plugin-metadata-invalid", message, severity="error")]
    return CliPluginTool(spec, command_tuple, timeout_ms), []


def valid_command(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(part, str) and part for part in value)
    )


def entry_diagnostic(index: int, message: str) -> dict[str, str]:
    return diagnostic(
        "plugin-config-invalid",
        f"tools[{index}]: {message}",
        severity="error",
    )


def tool_spec_from_metadata(value: Any) -> tuple[ToolSpec | None, str]:
    if not isinstance(value, dict):
        return None, "metadata JSON must be an object"
    name = value.get("name")
    description = value.get("description")
    schema = value.get("schema")
    interactive = value.get("interactive")
    effects = value.get("effects", [])
    if not isinstance(name, str) or not name:
        return None, "metadata.name must be a non-empty string"
    if not isinstance(description, str):
        return None, "metadata.description must be a string"
    if not isinstance(schema, dict):
        return None, "metadata.schema must be an object"
    if not isinstance(interactive, bool):
        return None, "metadata.interactive must be a boolean"
    if not isinstance(effects, list) or not set(effects) <= EFFECT_KINDS:
        kinds = ", ".join(sorted(EFFECT_KINDS))
        return None, f"metadata.effects must be an array drawn from: {kinds}"
    return ToolSpec(name, description, schema, interactive, tuple(effects)), ""


def run_plugin_json(
    command: tuple[str, ...],
    timeout_ms: int,
    *,
    mode: PluginMode,
    params: dict[str, Any] | None = None,
) -> PluginJsonResult:
    argv = plugin_argv(command, mode)
    stdin = "" if params is None else json.dumps(params, ensure_ascii=False)
    try:
        completed = subprocess.run(
            argv,
            input=stdin,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_ms / 1000,
        )
    except subprocess.TimeoutExpired:
        return PluginJsonResult(
            False,
            code=plugin_error_code(mode, "timeout"),
            message=f"plugin command timed out after {timeout_ms}ms: {command[0]}",
        )
    except OSError as exc:
        return PluginJsonResult(
            False,
            code=plugin_error_code(mode, "failed"),
            message=f"plugin command failed to start: {exc}",
        )
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        suffix = f": {stderr[:MAX_STDERR_LENGTH]}" if stderr else ""
        return PluginJsonResult(
            False,
            code=plugin_error_code(mode, "failed"),
            message=(
                f"plugin command exited with status {completed.returncode}: "
                f"{command[0]}{suffix}"
            ),
        )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return PluginJsonResult(
            False,
            code=plugin_error_code(mode, "invalid-json"),
            message=f"plugin stdout was not valid JSON: {exc.msg}",
        )
    return PluginJsonResult(True, value=value)


def plugin_argv(command: tuple[str, ...], mode: PluginMode) -> list[str]:
    argv = list(command)
    if mode != "run":
        argv.append(f"--{mode}")
    return argv


def plugin_error_code(mode: PluginMode, failure: str) -> str:
    return f"plugin-{mode}-{failure}"
