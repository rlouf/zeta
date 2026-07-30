"""Capability and tool executor providers.

A capability runs somewhere. These protocols and providers name where: in this
process, or in a host the project selects through an entry point.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, Protocol, cast

from zeta.capabilities.paths import reset_base_dir, set_base_dir
from zeta.capabilities.registry import (
    CapabilityRegistry,
)
from zeta.capabilities.registry import registry as _default_tool_registry
from zeta.capabilities.types import ExecutionMode

CapabilityFunction = Callable[
    [dict[str, Any]], dict[str, Any] | Awaitable[dict[str, Any]]
]


ToolExecutorSetup = Callable[
    [str, CapabilityRegistry, Mapping[str, Any]], Awaitable["ToolExecutor"]
]


class CapabilityExecutor(Protocol):
    def __call__(
        self,
        params: dict[str, Any],
        *,
        mode: ExecutionMode,
        effect_key: str | None = None,
    ) -> dict[str, Any] | Awaitable[dict[str, Any]]: ...


class ToolExecutor(Protocol):
    """Execute calls in one persistent agent tool environment.

    A worker may call an executor concurrently and closes it after all agent
    invocations have finished.
    """

    async def call(
        self,
        capability_id: str,
        params: dict[str, Any],
        mode: ExecutionMode,
        *,
        base_dir: Path | None,
        effect_key: str | None,
    ) -> dict[str, Any]:
        """Return the normalized result for one capability call."""

    async def aclose(self) -> None:
        """Release resources owned by this executor."""


TOOL_EXECUTOR_ENTRY_POINT_GROUP = "zeta.tool_executors"


@dataclass(frozen=True)
class ToolExecutorProvider:
    """Transfer a persistent executor's lifecycle to the worker."""

    id: str
    setup: ToolExecutorSetup


@dataclass
class ToolExecutorProviderRegistry:
    """Named tool executor providers available to a runtime."""

    providers: dict[str, ToolExecutorProvider] = field(default_factory=dict)

    def register(self, provider: ToolExecutorProvider) -> None:
        if provider.id in self.providers:
            raise ValueError(
                f"tool executor provider {provider.id!r} is already registered"
            )
        self.providers[provider.id] = provider

    def resolve(self, provider_id: str) -> ToolExecutorProvider | None:
        return self.providers.get(provider_id)


@dataclass(frozen=True)
class InProcessToolExecutor:
    """Execute capabilities through the local registry."""

    registry: CapabilityRegistry

    async def call(
        self,
        capability_id: str,
        params: dict[str, Any],
        mode: ExecutionMode,
        *,
        base_dir: Path | None,
        effect_key: str | None,
    ) -> dict[str, Any]:
        token = set_base_dir(base_dir)
        try:
            result = invoke_capability(
                capability_id,
                params,
                execution_mode=mode,
                tool_registry=self.registry,
                effect_key=effect_key,
            )
            if inspect.isawaitable(result):
                result = await result
            return result
        finally:
            reset_base_dir(token)

    async def aclose(self) -> None:
        return None


async def local_tool_executor_provider(
    agent_id: str,
    registry: CapabilityRegistry,
    config: Mapping[str, Any],
) -> ToolExecutor:
    """Set up the built-in executor that invokes local capabilities."""
    del agent_id
    if config:
        raise ValueError("local tool executor does not accept configuration")
    return InProcessToolExecutor(registry)


def load_tool_executor_provider_registry(
    entry_points: Iterable[Any] | None = None,
) -> ToolExecutorProviderRegistry:
    """Load built-in and installed tool executor providers."""
    registry = ToolExecutorProviderRegistry()
    registry.register(ToolExecutorProvider("local", local_tool_executor_provider))
    for entry_point in tool_executor_entry_points(entry_points):
        provider = load_entry_point_tool_executor_provider(entry_point)
        if provider.id != entry_point.name:
            raise ValueError(
                f"tool executor entry point {entry_point.name!r} returned "
                f"provider id {provider.id!r}"
            )
        registry.register(provider)
    return registry


def tool_executor_entry_points(
    entry_points: Iterable[Any] | None = None,
) -> tuple[Any, ...]:
    discovered = (
        importlib_metadata.entry_points() if entry_points is None else entry_points
    )
    select = getattr(discovered, "select", None)
    if callable(select):
        return tuple(select(group=TOOL_EXECUTOR_ENTRY_POINT_GROUP))
    if isinstance(discovered, Mapping):
        grouped = cast(Mapping[str, Iterable[Any]], discovered)
        return tuple(grouped.get(TOOL_EXECUTOR_ENTRY_POINT_GROUP, ()))
    return tuple(
        entry_point
        for entry_point in discovered
        if getattr(entry_point, "group", None) == TOOL_EXECUTOR_ENTRY_POINT_GROUP
    )


def load_entry_point_tool_executor_provider(entry_point: Any) -> ToolExecutorProvider:
    provider = entry_point.load()
    if callable(provider):
        provider = provider()
    if not isinstance(provider, ToolExecutorProvider):
        raise ValueError(
            f"tool executor entry point {entry_point.name!r} did not return "
            "a ToolExecutorProvider"
        )
    return provider


@dataclass(frozen=True)
class InProcessCapabilityExecutor:
    run: CapabilityFunction
    stage: CapabilityFunction | None = None

    async def __call__(
        self,
        params: dict[str, Any],
        *,
        mode: ExecutionMode,
        effect_key: str | None = None,
    ) -> dict[str, Any]:
        del effect_key
        if mode == "stage" and self.stage is not None:
            result = self.stage(params)
        else:
            result = self.run(params)
        if inspect.isawaitable(result):
            result = await result
        return dict(cast(dict[str, Any], result))


async def invoke_capability(
    capability_id: str,
    params: dict[str, Any],
    *,
    execution_mode: ExecutionMode = "stage",
    tool_registry: CapabilityRegistry | None = None,
    effect_key: str | None = None,
) -> dict[str, Any]:
    active_tool_registry = tool_registry or _default_tool_registry
    return await active_tool_registry.invoke_async(
        capability_id,
        params,
        execution_mode=execution_mode,
        effect_key=effect_key,
    )
