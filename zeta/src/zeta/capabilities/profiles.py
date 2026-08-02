"""Model-facing presentations for canonical capabilities."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

ArgumentAdapter = Callable[[dict[str, Any]], dict[str, Any]]


def identity_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    return dict(arguments)


@dataclass(frozen=True)
class ToolPresentation:
    name: str
    description: str
    input_schema: dict[str, Any]
    adapt_arguments: ArgumentAdapter = identity_arguments


@dataclass(frozen=True)
class ToolProfile:
    name: str
    overrides: dict[str, ToolPresentation]


def adapt_exec_command(arguments: dict[str, Any]) -> dict[str, Any]:
    return {"command": arguments["cmd"]}


EXEC_COMMAND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["cmd"],
    "properties": {"cmd": {"type": "string"}},
}

APPLY_PATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["patch"],
    "properties": {"patch": {"type": "string", "minLength": 1}},
}

NATIVE_TOOL_PROFILE = ToolProfile("native", {})

# Models call tools more reliably when the tool interface matches their training.
# Keep this mapping here so capability IDs and executors stay stable.
CODEX_TOOL_PROFILE = ToolProfile(
    "codex",
    {
        "zeta.bash": ToolPresentation(
            "exec_command",
            "Run a shell command.",
            EXEC_COMMAND_SCHEMA,
            adapt_exec_command,
        ),
        "zeta.patch": ToolPresentation(
            "apply_patch",
            "Apply a patch to files.",
            APPLY_PATCH_SCHEMA,
        ),
    },
)

TOOL_PROFILES: Mapping[str, ToolProfile] = {
    NATIVE_TOOL_PROFILE.name: NATIVE_TOOL_PROFILE,
    CODEX_TOOL_PROFILE.name: CODEX_TOOL_PROFILE,
}
TOOL_PROFILE_NAMES = tuple(sorted(TOOL_PROFILES))


def resolve_tool_profile(profile: str | ToolProfile | None) -> ToolProfile:
    if isinstance(profile, ToolProfile):
        return profile
    name = profile or NATIVE_TOOL_PROFILE.name
    selected = TOOL_PROFILES.get(name)
    if selected is None:
        raise ValueError(f"unknown tool profile: {name}")
    return selected
