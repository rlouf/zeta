"""Shared option and store helpers for the command line."""

from pathlib import Path
from typing import Any

import click
from zeta.capabilities.registry import CapabilityRegistry
from zeta.harness.store import RuntimeEventStore
from zeta.journal.sqlite import event_store_path
from zeta.paths import resolve_state_dir
from zeta.tools import register_builtin_tools


def state_dir_option(function: Any) -> Any:
    """Keep state selection on the leaf command that consumes it."""

    return click.option(
        "--state-dir",
        type=click.Path(file_okay=False, path_type=Path),
        help="Override the runtime state directory.",
    )(function)


def runtime_event_store(
    state_dir: Path | None,
    *,
    read_only: bool = True,
) -> RuntimeEventStore:
    path = event_store_path(resolve_state_dir(state_dir))
    return RuntimeEventStore.open(path, read_only=read_only)


def cli_tool_registry() -> CapabilityRegistry:
    registry = CapabilityRegistry()
    register_builtin_tools(registry)
    return registry


def connector_names_from_option(value: str | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    names = tuple(name.strip() for name in value.split(",") if name.strip())
    return names
