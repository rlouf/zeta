"""Tool host directory for capability execution dispatch."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Protocol

from zeta.capabilities.registry import (
    CapabilityRegistry,
    CapabilityToolSchema,
    RegisteredCapability,
    error_result,
    model_descriptor,
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


class HostDirectory:
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

    def get(self, capability_id: str) -> RegisteredCapability | None:
        """Get a hosted declaration by canonical id."""

        return self._capabilities.get(capability_id)

    def resolve(self, name: str) -> str | None:
        """Resolve a model-facing name or canonical id to a hosted id."""

        if name in self._capabilities:
            return name
        matches = self._names.get(name, [])
        if len(matches) != 1:
            return None
        return matches[0]

    def model_name(self, capability_id: str) -> str:
        """Return the model-facing function name for a hosted capability."""

        capability = self._capabilities[capability_id]
        return capability.declaration.id.name

    def list_capability_ids(self) -> list[str]:
        """List canonical ids served by connected hosts."""

        return sorted(self._capabilities)

    def list_auto_enabled_capability_ids(self) -> list[str]:
        """List host declarations available by default."""

        return self.list_capability_ids()

    def model_tool_schema(
        self,
        enabled_ids: tuple[str, ...],
        *,
        name_overrides: dict[str, str] | None = None,
    ) -> CapabilityToolSchema:
        """Build the per-run model-visible schema from hosted declarations."""

        name_overrides = name_overrides or {}
        name_to_id: dict[str, str] = {}
        descriptors = []
        for requested_id in enabled_ids:
            capability_id = self.resolve(requested_id)
            if capability_id is None:
                continue
            capability = self.get(capability_id)
            if capability is None:
                continue
            name = name_overrides.get(capability_id, self.model_name(capability_id))
            existing = name_to_id.get(name)
            if existing is not None and existing != capability_id:
                raise ValueError(
                    f"ambiguous capability name {name!r}: "
                    f"{existing!r} and {capability_id!r}"
                )
            name_to_id[name] = capability_id
            descriptors.append(model_descriptor(name, capability))
        return CapabilityToolSchema(name_to_id=name_to_id, descriptors=descriptors)


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
        del ctx
        from zeta.capabilities import execution as capability_execution

        result = capability_execution.invoke_capability(
            capability_id,
            params,
            execution_mode=mode,
            tool_registry=self.registry,
        )
        if inspect.isawaitable(result):
            result = await result
        return result
