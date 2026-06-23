"""Registry for Zeta capabilities."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any, cast

from zeta.capabilities.types import Capability, ExecutionMode

__all__ = [
    "ExecutionMode",
    "CapabilityError",
    "CapabilityToolSchema",
    "CapabilityRegistry",
    "RegisteredCapability",
    "registry",
]


@dataclass(frozen=True)
class CapabilityError:
    code: str
    message: str
    data: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> CapabilityError | dict[str, Any]:
        code = value.get("code")
        message = value.get("message")
        if not isinstance(code, str) or not isinstance(message, str):
            return dict(value)
        data = value.get("data")
        extra = {
            key: item
            for key, item in value.items()
            if key not in {"code", "message", "data"}
        }
        return cls(
            code=code,
            message=message,
            data=data if isinstance(data, dict) else None,
            extra=extra,
        )

    def to_mapping(self) -> dict[str, Any]:
        payload = {"code": self.code, "message": self.message, **self.extra}
        if self.data is not None:
            payload["data"] = self.data
        return payload


def validated_capability_result_payload(
    capability_id: str,
    value: dict[str, Any],
) -> dict[str, Any]:
    validated = dict(value)
    ok = validated.get("ok")
    if isinstance(ok, bool):
        if ok is False and not isinstance(validated.get("error"), dict):
            validated["error"] = invalid_capability_result_error(capability_id)
        return validated
    validated["ok"] = False
    validated["error"] = invalid_capability_result_error(capability_id)
    return validated


def error_result(
    code: str,
    message: str,
    *,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"ok": False, "error": error}


@dataclass(frozen=True)
class CapabilityToolSchema:
    name_to_id: dict[str, str]
    descriptors: list[dict[str, Any]]


@dataclass(frozen=True)
class RegisteredCapability:
    declaration: Capability
    executor: Any


class CapabilityRegistry:
    """Registry for Zeta capabilities."""

    def __init__(self) -> None:
        self._capabilities: dict[str, RegisteredCapability] = {}
        self._names: dict[str, list[str]] = {}

    def register(self, capability: RegisteredCapability) -> None:
        """Register a capability implementation under its canonical id."""
        capability_id = capability.declaration.id.canonical()
        if capability_id in self._capabilities:
            raise ValueError(f"capability {capability_id!r} is already registered")
        self._capabilities[capability_id] = capability
        self._names.setdefault(capability.declaration.id.name, []).append(capability_id)

    def get(self, capability_id: str) -> RegisteredCapability | None:
        """Get a registered capability implementation by canonical id."""
        return self._capabilities.get(capability_id)

    def get_by_name(self, name: str) -> RegisteredCapability | None:
        capability_id = self.resolve(name)
        if capability_id is None:
            return None
        return self.get(capability_id)

    def resolve(self, name: str) -> str | None:
        if name in self._capabilities:
            return name
        matches = self._names.get(name, [])
        if len(matches) != 1:
            return None
        return matches[0]

    def model_name(self, capability_id: str) -> str:
        capability = self._capabilities[capability_id]
        return capability.declaration.id.name

    def list_capability_ids(self) -> list[str]:
        """List registered canonical capability ids."""
        return sorted(self._capabilities)

    def list_auto_enabled_capability_ids(self) -> list[str]:
        return self.list_capability_ids()

    def model_tool_schema(
        self,
        enabled_ids: tuple[str, ...],
        *,
        name_overrides: dict[str, str] | None = None,
    ) -> CapabilityToolSchema:
        """Build the per-run model-visible tool schema for capabilities."""
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

    def invoke(
        self,
        capability_id: str,
        params: dict[str, Any],
        *,
        execution_mode: ExecutionMode = "stage",
    ) -> dict[str, Any]:
        """Invoke one capability under the staging contract its policy declares.

        Read-only capabilities always run. Mutating capabilities run in direct
        mode; in stage mode they stage their work for review, and a capability
        without a staging implementation is refused.
        """
        capability_id = self.resolve(capability_id) or capability_id
        capability = self.get(capability_id)
        if capability is None:
            return error_result(
                "unknown-capability", f"unknown capability: {capability_id}"
            )
        return invoke_executor(
            capability_id,
            capability,
            params,
            execution_mode,
        )

    async def invoke_async(
        self,
        capability_id: str,
        params: dict[str, Any],
        *,
        execution_mode: ExecutionMode = "stage",
    ) -> dict[str, Any]:
        capability_id = self.resolve(capability_id) or capability_id
        capability = self.get(capability_id)
        if capability is None:
            return error_result(
                "unknown-capability", f"unknown capability: {capability_id}"
            )
        return await invoke_executor_async(
            capability_id,
            capability,
            params,
            execution_mode,
        )


registry = CapabilityRegistry()


def invoke_executor(
    capability_id: str,
    capability: RegisteredCapability,
    params: dict[str, Any],
    mode: ExecutionMode,
) -> dict[str, Any]:
    try:
        result = capability.executor(params, mode=mode)
        if inspect.isawaitable(result):
            result = asyncio.run(cast(Coroutine[Any, Any, dict[str, Any]], result))
    except Exception as exc:
        return error_result(
            "executor-exception",
            f"{type(exc).__name__}: {exc}",
            data={"capability_id": capability_id},
        )
    return validated_capability_result_payload(capability_id, result)


async def invoke_executor_async(
    capability_id: str,
    capability: RegisteredCapability,
    params: dict[str, Any],
    mode: ExecutionMode,
) -> dict[str, Any]:
    try:
        if inspect.iscoroutinefunction(capability.executor):
            result = await capability.executor(params, mode=mode)
        else:
            result = await asyncio.to_thread(
                capability.executor,
                params,
                mode=mode,
            )
    except Exception as exc:
        return error_result(
            "executor-exception",
            f"{type(exc).__name__}: {exc}",
            data={"capability_id": capability_id},
        )
    if inspect.isawaitable(result):
        result = await result
    result = cast(dict[str, Any], result)
    return validated_capability_result_payload(capability_id, result)


def invalid_capability_result_error(capability_id: str) -> dict[str, Any]:
    return CapabilityError(
        code="invalid-capability-result",
        message="capability result must include boolean ok",
        data={"capability_id": capability_id},
    ).to_mapping()


def model_descriptor(alias: str, capability: RegisteredCapability) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": alias,
            "description": capability.declaration.description,
            "parameters": capability.declaration.input_schema,
        },
    }
