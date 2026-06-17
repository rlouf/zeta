"""Registry for Zeta capabilities."""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from .base import Capability, ExecutionMode, error_result

__all__ = ["ExecutionMode", "CapabilityRegistry", "registry"]


class CapabilityRegistry:
    """Registry for Zeta capabilities."""

    def __init__(self) -> None:
        self._capabilities: dict[str, Capability] = {}
        self._aliases: dict[str, str] = {}

    def register(self, capability: Capability) -> None:
        """Register a capability implementation under its canonical id."""
        capability_id = capability.spec.id.canonical()
        if capability_id in self._capabilities:
            raise ValueError(f"capability {capability_id!r} is already registered")
        try:
            Draft202012Validator.check_schema(capability.spec.input_schema)
        except SchemaError as exc:
            raise ValueError(
                f"invalid schema for capability {capability_id!r}: {exc.message}"
            ) from exc
        if capability.policy.supports_staging and not capability.spec.mutates():
            raise ValueError(
                f"capability {capability_id!r} declares staging for read-only effects"
            )
        if (
            capability.spec.mutates()
            and not capability.policy.supports_staging
            and not capability.policy.supports_direct
        ):
            raise ValueError(
                f"capability {capability_id!r} supports neither staging nor direct execution"
            )
        self._capabilities[capability_id] = capability
        for alias in capability.spec.aliases:
            if alias in self._aliases:
                raise ValueError(f"capability alias {alias!r} is already registered")
            self._aliases[alias] = capability_id

    def get(self, capability_id: str) -> Capability | None:
        """Get a registered capability implementation by canonical id."""
        return self._capabilities.get(capability_id)

    def get_by_alias(self, alias: str) -> Capability | None:
        capability_id = self.resolve(alias)
        if capability_id is None:
            return None
        return self.get(capability_id)

    def resolve(self, name: str) -> str | None:
        if name in self._capabilities:
            return name
        return self._aliases.get(name)

    def model_alias(self, capability_id: str) -> str:
        capability = self._capabilities[capability_id]
        return capability.spec.aliases[0] if capability.spec.aliases else capability_id

    def list_capability_ids(self) -> list[str]:
        """List registered canonical capability ids."""
        return sorted(self._capabilities)

    def validate_capability_args(
        self, capability_id: str, params: dict[str, Any]
    ) -> list[str]:
        """Validate params against the capability's JSON Schema."""
        capability_id = self.resolve(capability_id) or capability_id
        capability = self.get(capability_id)
        if capability is None:
            return [f"unknown capability: {capability_id}"]
        try:
            validator = Draft202012Validator(capability.spec.input_schema)
        except SchemaError as exc:
            return [f"invalid schema for capability {capability_id}: {exc.message}"]
        errors = sorted(validator.iter_errors(params), key=_validation_error_sort_key)
        return [_format_validation_error(error) for error in errors]

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
        if not capability.spec.mutates():
            return capability.executor.invoke(
                capability.spec,
                params,
                mode=execution_mode,
            ).payload
        if execution_mode == "direct":
            if not capability.policy.supports_direct:
                return error_result(
                    "direct-execution-disallowed",
                    f"capability {capability_id} does not allow direct execution",
                )
            return capability.executor.invoke(
                capability.spec,
                params,
                mode=execution_mode,
            ).payload
        if not capability.policy.supports_staging:
            declared = ", ".join(capability.spec.effects) or "undeclared"
            return error_result(
                "staging-unsupported",
                f"capability {capability_id} has effects ({declared}) that cannot be staged "
                "for review; rerun in the do workflow (,,,)",
            )
        return capability.executor.invoke(
            capability.spec,
            params,
            mode=execution_mode,
        ).payload


registry = CapabilityRegistry()


def _validation_error_sort_key(error: ValidationError) -> tuple[str, str]:
    return (_json_path(error.absolute_path), error.message)


def _format_validation_error(error: ValidationError) -> str:
    return f"{_json_path(error.absolute_path)}: {error.message}"


def _json_path(parts: Any) -> str:
    path = "$"
    for part in parts:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}"
    return path
