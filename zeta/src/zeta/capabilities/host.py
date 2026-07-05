"""Tool host directory for capability execution dispatch."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Protocol

from zeta.capabilities.paths import reset_base_dir, set_base_dir
from zeta.capabilities.registry import (
    CapabilityDirectory,
    CapabilityRegistry,
    RegisteredCapability,
    error_result,
)
from zeta.capabilities.types import ExecutionMode

__all__ = [
    "DuplicateHostCapabilityError",
    "HostDirectory",
    "ToolHost",
    "TransitionalInProcessHost",
]


class ToolHost(Protocol):
    """A connected process that owns and executes declared tools."""

    @property
    def declarations(self) -> tuple[RegisteredCapability, ...]:
        """Return the capabilities this host serves."""

    @property
    def closed(self) -> bool:
        """Return whether this host can no longer accept calls."""

    async def call(
        self,
        capability_id: str,
        params: dict[str, Any],
        mode: ExecutionMode,
        ctx: Any,
    ) -> dict[str, Any]:
        """Execute a hosted capability call and return its result payload."""


@dataclass(frozen=True)
class DuplicateHostCapabilityError(ValueError):
    """Raised when two simultaneously connected hosts declare the same tool."""

    capability_id: str

    def __str__(self) -> str:
        return f"tool {self.capability_id!r} is already served by a host"

    def to_result(self) -> dict[str, Any]:
        return error_result(
            "duplicate-tool",
            str(self),
            data={"capability_id": self.capability_id},
        )


class HostDirectory(CapabilityDirectory):
    """Per-session view of host-owned capability declarations."""

    def __init__(self) -> None:
        self._hosts: list[ToolHost] = []
        self._capabilities: dict[str, RegisteredCapability] = {}
        self._capability_hosts: dict[str, ToolHost] = {}
        self._names: dict[str, list[str]] = {}

    @classmethod
    def from_registry(cls, registry: CapabilityRegistry) -> HostDirectory:
        """Build the Phase 1 compatibility directory for a registry."""

        directory = cls()
        directory.register_host(TransitionalInProcessHost(registry))
        return directory

    def register_host(self, host: ToolHost) -> None:
        """Register all declarations for one host atomically."""

        declarations = tuple(host.declarations)
        additions: list[tuple[str, RegisteredCapability]] = []
        names: dict[str, list[str]] = {}
        for capability in declarations:
            capability_id = capability.declaration.id.canonical()
            if capability_id in self._capabilities or capability_id in names:
                raise DuplicateHostCapabilityError(capability_id)
            additions.append((capability_id, capability))
            names.setdefault(capability.declaration.id.name, []).append(capability_id)

        self._hosts.append(host)
        for capability_id, capability in additions:
            self._capabilities[capability_id] = capability
            self._capability_hosts[capability_id] = host
            self._names.setdefault(capability.declaration.id.name, []).append(
                capability_id
            )

    def host_for(self, capability_id: str) -> ToolHost | None:
        """Return the host serving a capability id, if one is connected."""

        capability_id = self.resolve(capability_id) or capability_id
        host = self._capability_hosts.get(capability_id)
        if host is None or host.closed:
            return None
        return host


@dataclass(frozen=True)
class TransitionalInProcessHost:
    """Phase 1 scaffolding that adapts the legacy registry to the host seam."""

    registry: CapabilityRegistry

    @property
    def declarations(self) -> tuple[RegisteredCapability, ...]:
        declarations = []
        for capability_id in self.registry.list_capability_ids():
            capability = self.registry.get(capability_id)
            if capability is not None:
                declarations.append(capability)
        return tuple(declarations)

    @property
    def closed(self) -> bool:
        return False

    async def call(
        self,
        capability_id: str,
        params: dict[str, Any],
        mode: ExecutionMode,
        ctx: Any,
    ) -> dict[str, Any]:
        from zeta.capabilities import execution as capability_execution

        token = set_base_dir(getattr(ctx, "base_dir", None))
        try:
            result = capability_execution.invoke_capability(
                capability_id,
                params,
                execution_mode=mode,
                tool_registry=self.registry,
            )
            if inspect.isawaitable(result):
                result = await result
            return result
        finally:
            reset_base_dir(token)
