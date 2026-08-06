"""Registry for Zeta capabilities."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import subprocess
import sys
import tempfile
from collections.abc import Coroutine, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from zeta.capabilities.profiles import (
    ArgumentAdapter,
    ToolProfile,
    identity_arguments,
    resolve_tool_profile,
)
from zeta.capabilities.types import Capability

__all__ = [
    "CapabilityDirectory",
    "CapabilityError",
    "CapabilityToolRoute",
    "CapabilityToolSchema",
    "CapabilityRegistry",
    "RegisteredCapability",
    "AgentToolDefinition",
    "AgentToolDefinitionError",
    "agent_tool_definition_from_content",
    "load_agent_tool_definition",
    "validate_agent_tool_definition",
    "registry",
]

MAX_AGENT_TOOL_SOURCE_BYTES = 131_072
AGENT_TOOL_IMPORT_TIMEOUT_SECONDS = 5
AGENT_TOOL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
AGENT_TOOL_VALIDATION_MARKER = "__ZETA_AGENT_TOOL_VALID__"


class AgentToolDefinitionError(ValueError):
    """Reject a tool revision before it can enter an agent generation."""


@dataclass(frozen=True)
class AgentToolDefinition:
    owner: str
    key: str
    object_id: str
    name: str
    capability_id: str
    source: str


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
class CapabilityToolRoute:
    capability_id: str
    input_schema: dict[str, Any]
    adapt_arguments: ArgumentAdapter


@dataclass(frozen=True)
class CapabilityToolSchema:
    routes: dict[str, CapabilityToolRoute]
    descriptors: list[dict[str, Any]]

    @property
    def name_to_id(self) -> dict[str, str]:
        return {name: route.capability_id for name, route in self.routes.items()}


@dataclass(frozen=True)
class RegisteredCapability:
    declaration: Capability
    executor: Any


class CapabilityDirectory:
    """Name resolution and model-facing schema over a capability index.

    Shared read surface for any store of `RegisteredCapability` declarations
    keyed by canonical id. Subclasses own how `_capabilities` and `_names` are
    populated.
    """

    _capabilities: dict[str, RegisteredCapability]
    _names: dict[str, list[str]]

    def get(self, capability_id: str) -> RegisteredCapability | None:
        """Get a capability declaration by canonical id."""
        return self._capabilities.get(capability_id)

    def get_by_name(self, name: str) -> RegisteredCapability | None:
        capability_id = self.resolve(name)
        if capability_id is None:
            return None
        return self.get(capability_id)

    def resolve(self, name: str) -> str | None:
        """Resolve a model-facing name or canonical id to a canonical id."""
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
        """List known canonical capability ids."""
        return sorted(self._capabilities)

    def list_auto_enabled_capability_ids(self) -> list[str]:
        return self.list_capability_ids()

    def model_tool_schema(
        self,
        enabled_ids: tuple[str, ...],
        *,
        tool_profile: str | ToolProfile | None = None,
        name_overrides: dict[str, str] | None = None,
    ) -> CapabilityToolSchema:
        """Build the per-run model-visible tool schema for capabilities."""
        profile = resolve_tool_profile(tool_profile)
        name_overrides = name_overrides or {}
        routes: dict[str, CapabilityToolRoute] = {}
        descriptors = []
        for requested_id in enabled_ids:
            capability_id = self.resolve(requested_id)
            if capability_id is None:
                continue
            capability = self.get(capability_id)
            if capability is None:
                continue
            presentation = profile.overrides.get(capability_id)
            native_name = self.model_name(capability_id)
            name = name_overrides.get(
                capability_id,
                presentation.name if presentation is not None else native_name,
            )
            existing = routes.get(name)
            if existing is not None and existing.capability_id != capability_id:
                raise ValueError(
                    f"ambiguous capability name {name!r}: "
                    f"{existing.capability_id!r} and {capability_id!r}"
                )
            description = (
                presentation.description
                if presentation is not None
                else capability.declaration.description
            )
            input_schema = (
                presentation.input_schema
                if presentation is not None
                else capability.declaration.input_schema
            )
            routes[name] = CapabilityToolRoute(
                capability_id=capability_id,
                input_schema=input_schema,
                adapt_arguments=(
                    presentation.adapt_arguments
                    if presentation is not None
                    else identity_arguments
                ),
            )
            descriptors.append(model_descriptor(name, description, input_schema))
        return CapabilityToolSchema(routes=routes, descriptors=descriptors)


class CapabilityRegistry(CapabilityDirectory):
    """In-process registry for Zeta capabilities."""

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

    def copy(self) -> CapabilityRegistry:
        """Keep one generation's executable registry stable while later ones load."""

        copied = CapabilityRegistry()
        for capability_id in self.list_capability_ids():
            capability = self.get(capability_id)
            if capability is not None:
                copied.register(capability)
        return copied

    def invoke(
        self,
        capability_id: str,
        params: dict[str, Any],
        *,
        effect_key: str | None = None,
    ) -> dict[str, Any]:
        """Invoke one registered capability."""
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
            effect_key=effect_key,
        )

    async def invoke_async(
        self,
        capability_id: str,
        params: dict[str, Any],
        *,
        effect_key: str | None = None,
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
            effect_key=effect_key,
        )


registry = CapabilityRegistry()


def agent_tool_definition_from_content(
    content: Any,
    *,
    owner: str,
    key: str,
    object_id: str = "",
) -> AgentToolDefinition:
    if not isinstance(content, Mapping):
        raise AgentToolDefinitionError("tool definition content must be an object")
    unknown = set(content) - {"name", "capability_id", "source"}
    if unknown:
        raise AgentToolDefinitionError(
            f"tool definition has unsupported field {sorted(unknown)[0]!r}"
        )
    name = content.get("name")
    capability_id = content.get("capability_id")
    source = content.get("source")
    if not isinstance(name, str) or AGENT_TOOL_NAME.fullmatch(name) is None:
        raise AgentToolDefinitionError("tool definition name is invalid")
    expected_key = f"tools/{name}"
    if key != expected_key:
        raise AgentToolDefinitionError(f"tool definition key must be {expected_key!r}")
    expected_id = f"agent.{owner}.{name}"
    if capability_id != expected_id:
        raise AgentToolDefinitionError(
            f"tool definition capability_id must be {expected_id!r}"
        )
    if not isinstance(source, str) or not source.strip():
        raise AgentToolDefinitionError("tool definition source must not be empty")
    if len(source.encode("utf-8")) > MAX_AGENT_TOOL_SOURCE_BYTES:
        raise AgentToolDefinitionError("tool definition source is too large")
    return AgentToolDefinition(owner, key, object_id, name, expected_id, source)


def validate_agent_tool_definition(
    content: Any,
    *,
    owner: str,
    key: str,
    object_id: str = "",
) -> AgentToolDefinition:
    """Import authored code away from the worker before a revision can commit."""

    definition = agent_tool_definition_from_content(
        content,
        owner=owner,
        key=key,
        object_id=object_id,
    )
    script = (
        "import json, sys\n"
        "from zeta.capabilities.registry import "
        "AgentToolDefinition, load_agent_tool_definition\n"
        "definition = AgentToolDefinition(**json.loads(sys.stdin.read()))\n"
        "load_agent_tool_definition(definition)\n"
        f"print({AGENT_TOOL_VALIDATION_MARKER!r}, flush=True)\n"
    )
    try:
        with tempfile.TemporaryFile(mode="w+") as output:
            completed = subprocess.run(
                [sys.executable, "-c", script],
                input=json.dumps(asdict(definition)),
                text=True,
                stdout=output,
                stderr=subprocess.DEVNULL,
                timeout=AGENT_TOOL_IMPORT_TIMEOUT_SECONDS,
                check=False,
            )
            output.seek(0)
            imported = AGENT_TOOL_VALIDATION_MARKER in output.read()
    except subprocess.TimeoutExpired as exc:
        raise AgentToolDefinitionError("tool definition import timed out") from exc
    if completed.returncode != 0 or not imported:
        raise AgentToolDefinitionError("tool definition could not be imported")
    return definition


def load_agent_tool_definition(
    definition: AgentToolDefinition,
) -> RegisteredCapability:
    """Compile the exact source stored in an immutable project generation."""

    namespace: dict[str, Any] = {"__name__": f"zeta_agent_tool_{definition.owner}"}
    try:
        exec(
            compile(definition.source, f"<{definition.capability_id}>", "exec"),
            namespace,
        )
    except BaseException as exc:
        raise AgentToolDefinitionError(
            f"tool definition raised {type(exc).__name__} during import"
        ) from exc
    candidate = namespace.get("tool")
    if callable(candidate) and not isinstance(candidate, RegisteredCapability):
        try:
            candidate = candidate()
        except BaseException as exc:
            raise AgentToolDefinitionError(
                f"tool definition factory raised {type(exc).__name__}"
            ) from exc
    if not isinstance(candidate, RegisteredCapability):
        raise AgentToolDefinitionError(
            "tool definition must expose RegisteredCapability as tool or tool()"
        )
    declaration = candidate.declaration
    if declaration.id.canonical() != definition.capability_id:
        raise AgentToolDefinitionError(
            "tool source capability id does not match its definition"
        )
    if declaration.id.name != definition.name:
        raise AgentToolDefinitionError("tool source name does not match its definition")
    if not declaration.description.strip():
        raise AgentToolDefinitionError("tool description must not be empty")
    if not isinstance(declaration.input_schema, dict):
        raise AgentToolDefinitionError("tool input schema must be an object")
    try:
        Draft202012Validator.check_schema(declaration.input_schema)
    except SchemaError as exc:
        raise AgentToolDefinitionError("tool input schema is invalid") from exc
    if not callable(candidate.executor):
        raise AgentToolDefinitionError("tool executor must be callable")
    return candidate


def invoke_executor(
    capability_id: str,
    capability: RegisteredCapability,
    params: dict[str, Any],
    *,
    effect_key: str | None = None,
) -> dict[str, Any]:
    try:
        result = capability.executor(
            params,
            **executor_kwargs(capability.executor, effect_key),
        )
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
    *,
    effect_key: str | None = None,
) -> dict[str, Any]:
    try:
        kwargs = executor_kwargs(capability.executor, effect_key)
        if inspect.iscoroutinefunction(capability.executor):
            result = await capability.executor(params, **kwargs)
        else:
            result = await asyncio.to_thread(
                capability.executor,
                params,
                **kwargs,
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


def executor_kwargs(
    executor: Any,
    effect_key: str | None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if effect_key is None:
        return kwargs
    try:
        parameters = inspect.signature(executor).parameters.values()
    except (TypeError, ValueError):
        return kwargs
    if any(
        parameter.name == "effect_key"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    ):
        kwargs["effect_key"] = effect_key
    return kwargs


def invalid_capability_result_error(capability_id: str) -> dict[str, Any]:
    return CapabilityError(
        code="invalid-capability-result",
        message="capability result must include boolean ok",
        data={"capability_id": capability_id},
    ).to_mapping()


def model_descriptor(
    name: str,
    description: str,
    input_schema: dict[str, Any],
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": input_schema,
        },
    }
