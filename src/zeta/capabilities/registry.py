"""Registry for Zeta capabilities."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from .base import Capability, CapabilityResult, ExecutionMode, error_result

__all__ = [
    "ExecutionMode",
    "CapabilityError",
    "CapabilityProjection",
    "CapabilityRegistry",
    "CapabilityResultPayload",
    "registry",
]

CAPABILITY_RESULT_FIELDS = frozenset({"ok", "content", "metadata", "effect", "error"})


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


@dataclass(frozen=True)
class CapabilityResultPayload:
    ok: bool
    content: Any | None = None
    metadata: dict[str, Any] | None = None
    effect: dict[str, Any] | None = None
    error: CapabilityError | dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    _present_fields: frozenset[str] = field(default_factory=frozenset, repr=False)

    @classmethod
    def from_mapping(
        cls,
        capability_id: str,
        value: dict[str, Any],
    ) -> CapabilityResultPayload:
        normalized = normalized_capability_result_mapping(capability_id, value)
        return cls(
            ok=normalized["ok"],
            content=normalized.get("content"),
            metadata=dict_field(normalized, "metadata"),
            effect=dict_field(normalized, "effect"),
            error=capability_result_error(normalized),
            extra=capability_result_extra(normalized),
            _present_fields=capability_result_present_fields(normalized),
        )

    def to_mapping(self) -> dict[str, Any]:
        payload = {"ok": self.ok, **self.extra}
        if "content" in self._present_fields:
            payload["content"] = self.content
        if "metadata" in self._present_fields:
            payload["metadata"] = self.metadata
        if "effect" in self._present_fields:
            payload["effect"] = self.effect
        if "error" in self._present_fields:
            if isinstance(self.error, CapabilityError):
                payload["error"] = self.error.to_mapping()
            else:
                payload["error"] = self.error
        return payload


def normalized_capability_result_mapping(
    capability_id: str,
    value: dict[str, Any],
) -> dict[str, Any]:
    normalized = dict(value)
    ok = normalized.get("ok")
    if isinstance(ok, bool):
        if ok is False and not isinstance(normalized.get("error"), dict):
            normalized["error"] = invalid_capability_result_error(capability_id)
        return normalized
    normalized["ok"] = False
    normalized["error"] = invalid_capability_result_error(capability_id)
    return normalized


def dict_field(value: dict[str, Any], key: str) -> dict[str, Any] | None:
    field_value = value.get(key)
    return field_value if isinstance(field_value, dict) else None


def capability_result_error(
    value: dict[str, Any],
) -> CapabilityError | dict[str, Any] | None:
    error = value.get("error")
    return CapabilityError.from_mapping(error) if isinstance(error, dict) else None


def capability_result_extra(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item for key, item in value.items() if key not in CAPABILITY_RESULT_FIELDS
    }


def capability_result_present_fields(value: dict[str, Any]) -> frozenset[str]:
    return frozenset(
        key for key in CAPABILITY_RESULT_FIELDS if key in value and key != "ok"
    )


@dataclass(frozen=True)
class CapabilityProjection:
    alias_to_id: dict[str, str]
    descriptors: list[dict[str, Any]]


class CapabilityRegistry:
    """Registry for Zeta capabilities."""

    def __init__(self) -> None:
        self._capabilities: dict[str, Capability] = {}
        self._aliases: dict[str, list[str]] = {}

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
            self._aliases.setdefault(alias, []).append(capability_id)

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
        matches = self._aliases.get(name, [])
        if len(matches) != 1:
            return None
        return matches[0]

    def model_alias(self, capability_id: str) -> str:
        capability = self._capabilities[capability_id]
        return capability.spec.aliases[0] if capability.spec.aliases else capability_id

    def list_capability_ids(self) -> list[str]:
        """List registered canonical capability ids."""
        return sorted(self._capabilities)

    def list_ids(self) -> list[str]:
        """List registered canonical capability ids."""
        return self.list_capability_ids()

    def list_auto_enabled_capability_ids(self) -> list[str]:
        return [
            capability_id
            for capability_id in self.list_capability_ids()
            if capability_auto_enabled(self._capabilities[capability_id])
        ]

    def project(
        self,
        enabled_ids: tuple[str, ...],
        *,
        alias_overrides: dict[str, str] | None = None,
    ) -> CapabilityProjection:
        """Build the per-run model-visible projection for capabilities."""
        alias_overrides = alias_overrides or {}
        alias_to_id: dict[str, str] = {}
        descriptors = []
        for requested_id in enabled_ids:
            capability_id = self.resolve(requested_id)
            if capability_id is None:
                continue
            capability = self.get(capability_id)
            if capability is None:
                continue
            alias = alias_overrides.get(capability_id, self.model_alias(capability_id))
            existing = alias_to_id.get(alias)
            if existing is not None and existing != capability_id:
                raise ValueError(
                    f"ambiguous capability alias {alias!r}: "
                    f"{existing!r} and {capability_id!r}"
                )
            alias_to_id[alias] = capability_id
            descriptors.append(model_descriptor(alias, capability))
        return CapabilityProjection(alias_to_id=alias_to_id, descriptors=descriptors)

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
            return invoke_executor(
                capability_id,
                capability,
                params,
                execution_mode,
            ).payload
        if execution_mode == "direct":
            if not capability.policy.supports_direct:
                return error_result(
                    "direct-execution-disallowed",
                    f"capability {capability_id} does not allow direct execution",
                )
            if low_trust_mutating_capability(capability):
                return error_result(
                    "trust-direct-disallowed",
                    f"capability {capability_id} with {capability.policy.trust} trust "
                    "cannot run mutating effects directly",
                )
            return invoke_executor(
                capability_id,
                capability,
                params,
                execution_mode,
            ).payload
        if not capability.policy.supports_staging:
            declared = ", ".join(capability.spec.effects) or "undeclared"
            return error_result(
                "staging-unsupported",
                f"capability {capability_id} has effects ({declared}) that cannot be staged "
                "for review; rerun in the do workflow (,,,)",
            )
        return invoke_executor(
            capability_id,
            capability,
            params,
            execution_mode,
        ).payload


registry = CapabilityRegistry()


def capability_auto_enabled(capability: Capability) -> bool:
    return not low_trust_mutating_capability(capability)


def low_trust_mutating_capability(capability: Capability) -> bool:
    return capability.policy.trust in {"client", "remote"} and capability.spec.mutates()


def invoke_executor(
    capability_id: str,
    capability: Capability,
    params: dict[str, Any],
    mode: ExecutionMode,
) -> CapabilityResult:
    try:
        result = capability.executor.invoke(capability.spec, params, mode=mode)
    except Exception as exc:
        return CapabilityResult.from_mapping(
            error_result(
                "executor-exception",
                f"{type(exc).__name__}: {exc}",
                data={"capability_id": capability_id},
            )
        )
    return CapabilityResult.from_mapping(
        normalize_capability_result_payload(capability_id, result.payload)
    )


def normalize_capability_result_payload(
    capability_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return CapabilityResultPayload.from_mapping(capability_id, payload).to_mapping()


def invalid_capability_result_error(capability_id: str) -> dict[str, Any]:
    return CapabilityError(
        code="invalid-capability-result",
        message="capability result must include boolean ok",
        data={"capability_id": capability_id},
    ).to_mapping()


def _validation_error_sort_key(error: ValidationError) -> tuple[str, str]:
    return (_json_path(error.absolute_path), error.message)


def model_descriptor(alias: str, capability: Capability) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": alias,
            "description": capability.spec.description,
            "parameters": capability.spec.input_schema,
        },
    }


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
