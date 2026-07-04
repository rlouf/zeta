"""Execute model-requested capability calls."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from zeta.capabilities.host import HostDirectory
from zeta.capabilities.registry import (
    CapabilityRegistry,
    CapabilityToolSchema,
    error_result,
    validated_capability_result_payload,
)
from zeta.capabilities.registry import registry as _default_tool_registry
from zeta.capabilities.types import ExecutionMode
from zeta.models.chat_completions import tool_call_id
from zeta.records.events import (
    DraftEvent,
    draft_from_runtime_event,
    ensure_runtime_event_id,
    normalized_tool_result,
    tool_result_status,
)
from zeta.records.provenance import project_prompt_trace_projection
from zeta.records.stores.object_store import Store


class CapabilityExecutor(Protocol):
    def __call__(
        self,
        params: dict[str, Any],
        *,
        mode: ExecutionMode,
    ) -> dict[str, Any] | Awaitable[dict[str, Any]]: ...


CapabilityFunction = Callable[
    [dict[str, Any]], dict[str, Any] | Awaitable[dict[str, Any]]
]


@dataclass(frozen=True)
class InProcessCapabilityExecutor:
    run: CapabilityFunction
    stage: CapabilityFunction | None = None

    async def __call__(
        self,
        params: dict[str, Any],
        *,
        mode: ExecutionMode,
    ) -> dict[str, Any]:
        if mode == "stage" and self.stage is not None:
            result = self.stage(params)
        else:
            result = self.run(params)
        if inspect.isawaitable(result):
            result = await result
        return dict(cast(dict[str, Any], result))


def diagnostic(
    code: str, message: str, *, severity: str = "unsupported"
) -> dict[str, str]:
    return {"code": code, "message": message, "severity": severity}


def proposed_command_effect(
    command: str, reason: str, *, artifact: str | None = None
) -> dict[str, Any]:
    effect = {
        "kind": "command",
        "status": "proposed",
        "command": command,
        "reason": reason,
    }
    if artifact is not None:
        effect["artifact"] = artifact
    return {"ok": True, "effect": effect}


def proposed_effect(result: dict[str, Any]) -> dict[str, Any] | None:
    if result.get("ok") is not True:
        return None
    effect = result.get("effect")
    if not isinstance(effect, dict) or effect.get("status") != "proposed":
        return None
    return effect


def effect_resolution(result: dict[str, Any]) -> dict[str, Any] | None:
    effect = result.get("effect")
    if not isinstance(effect, dict):
        return None
    status = effect.get("status")
    if status not in {"resolved", "cancelled"}:
        return None
    return effect


def content_hash(data: bytes | str) -> str:
    """Return the sha256 content address of file bytes or UTF-8 text."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return "sha256:" + hashlib.sha256(data).hexdigest()


def short_tag(content_address: str) -> str:
    """Return the short 8-char snapshot tag from a content address."""
    return content_address.split(":", 1)[1][:8]


def file_content_hash(path: str | Path) -> str | None:
    """Return the content address of a file, or None if it cannot be read."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    return content_hash(data)


def change_hashes(path: str, content: str) -> dict[str, str]:
    """Hash the file as it stands (when readable) and the content replacing it."""
    hashes = {"after_hash": content_hash(content)}
    before_hash = file_content_hash(path)
    if before_hash is not None:
        hashes["before_hash"] = before_hash
    return hashes


def write_temp(prefix: str, suffix: str, content: str) -> Path:
    fd, raw_path = tempfile.mkstemp(prefix=prefix, suffix=suffix)
    path = Path(raw_path)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    return path


CapabilityEventSink = Callable[[DraftEvent], None]


@dataclass(frozen=True)
class CapabilityExecutionContext:
    event_sink: CapabilityEventSink | None
    trace_store: Store | None
    tool_registry: CapabilityRegistry
    tool_hosts: HostDirectory | None = None


@dataclass(frozen=True)
class CapabilityCallResult:
    events: list[DraftEvent]
    staged_effect: dict[str, Any] | None = None
    stop: bool = False


def model_tool_call_event_payload(
    tool_call: dict[str, Any],
    *,
    index: int,
    caused_by: str | None,
) -> dict[str, Any]:
    record = ModelToolCall.from_provider(tool_call, index=index)
    if record is None:
        return {}
    return record.event(caused_by=caused_by)


@dataclass(frozen=True)
class ModelToolCall:
    call_id: str
    name: str
    raw_arguments: str
    params: dict[str, Any]
    parse_error: str = ""

    @classmethod
    def from_provider(
        cls,
        tool_call: dict[str, Any],
        *,
        index: int,
    ) -> ModelToolCall | None:
        call_id = tool_call_id(tool_call, index=index)
        function = tool_call.get("function")
        if not isinstance(function, dict):
            return None
        name = str(function.get("name") or "")
        arguments = function.get("arguments")
        params, parse_error = parse_tool_arguments(arguments)
        raw_arguments = arguments if isinstance(arguments, str) else json.dumps(params)
        return cls(
            call_id=call_id,
            name=name,
            raw_arguments=raw_arguments,
            params=params,
            parse_error=parse_error,
        )

    def event(self, *, caused_by: str | None) -> dict[str, Any]:
        event: dict[str, Any] = {
            "type": "tool_call",
            "id": self.call_id,
            "tool_call_id": self.call_id,
            "status": "pending",
            "name": self.name,
            "input": self.params,
            "arguments": self.raw_arguments,
        }
        if caused_by is not None:
            event["caused_by"] = caused_by
        return event


@dataclass(frozen=True)
class CapabilityCallInvocation:
    tool_call: ModelToolCall
    call_event: dict[str, Any]

    @property
    def call_id(self) -> str:
        return self.tool_call.call_id

    @property
    def name(self) -> str:
        return self.tool_call.name

    @property
    def params(self) -> dict[str, Any]:
        return self.tool_call.params

    @property
    def parse_error(self) -> str:
        return self.tool_call.parse_error


@dataclass(frozen=True)
class ToolCallValidation:
    capability_id: str = ""
    error: tuple[str, str] | None = None


async def handle_tool_call(
    tool_call: dict[str, Any],
    *,
    allowed_capabilities: tuple[str, ...],
    tool_schema: CapabilityToolSchema,
    index: int,
    execution_mode: ExecutionMode = "stage",
    model_telemetry: dict[str, Any] | None = None,
    caused_by: str | None = None,
    ctx: CapabilityExecutionContext,
) -> CapabilityCallResult:
    call_id = tool_call_id(tool_call, index=index)
    invocation = tool_call_invocation(tool_call, index=index, caused_by=caused_by)
    if invocation is None:
        return invalid_tool_result(
            call_id,
            "",
            {},
            "invalid-tool-call",
            "tool call did not include a function payload",
            model_telemetry=model_telemetry,
            caused_by=caused_by,
            ctx=ctx,
        )
    validation = validate_tool_call(
        invocation,
        allowed_capabilities=allowed_capabilities,
        tool_schema=tool_schema,
        tool_registry=ctx.tool_registry,
    )
    if validation.error is not None:
        code, message = validation.error
        return reject_tool_call(
            invocation,
            code,
            message,
            model_telemetry=model_telemetry,
            ctx=ctx,
        )
    return await run_valid_tool_call(
        invocation,
        capability_id=validation.capability_id,
        execution_mode=execution_mode,
        model_telemetry=model_telemetry,
        ctx=ctx,
    )


def tool_call_invocation(
    tool_call: dict[str, Any],
    *,
    index: int,
    caused_by: str | None,
) -> CapabilityCallInvocation | None:
    record = ModelToolCall.from_provider(tool_call, index=index)
    if record is None:
        return None
    return CapabilityCallInvocation(
        tool_call=record,
        call_event=record.event(caused_by=caused_by),
    )


def validate_tool_call(
    invocation: CapabilityCallInvocation,
    *,
    allowed_capabilities: tuple[str, ...],
    tool_schema: CapabilityToolSchema,
    tool_registry: CapabilityRegistry,
) -> ToolCallValidation:
    if invocation.parse_error:
        return ToolCallValidation(error=("invalid-json-args", invocation.parse_error))
    capability_id = tool_schema.name_to_id.get(invocation.name)
    if capability_id is None:
        if tool_registry.resolve(invocation.name) is not None:
            return ToolCallValidation(
                error=(
                    "disallowed-tool",
                    f"tool is not allowed in this workflow: {invocation.name}",
                )
            )
        return ToolCallValidation(
            error=("unknown-tool", f"unknown tool: {invocation.name}")
        )
    if capability_id not in allowed_capabilities:
        return ToolCallValidation(
            error=(
                "disallowed-tool",
                f"tool is not allowed in this workflow: {invocation.name}",
            )
        )
    capability = tool_registry.get(capability_id)
    if capability is not None:
        schema_error = tool_args_schema_error(
            invocation.params, capability.declaration.input_schema
        )
        if schema_error is not None:
            return ToolCallValidation(error=("invalid-tool-args", schema_error))
    return ToolCallValidation(capability_id=capability_id)


def tool_args_schema_error(
    params: dict[str, Any], schema: dict[str, Any]
) -> str | None:
    """Return the first schema violation in a tool call's arguments, or None.

    A missing or empty schema imposes no constraints, and a schema that is
    itself malformed is skipped rather than rejecting every call.
    """
    if not schema:
        return None
    try:
        validator = Draft202012Validator(schema)
        errors = sorted(
            validator.iter_errors(params), key=lambda error: list(error.path)
        )
    except SchemaError:
        return None
    if not errors:
        return None
    first = errors[0]
    location = "/".join(str(part) for part in first.path)
    return f"{location}: {first.message}" if location else first.message


def reject_tool_call(
    invocation: CapabilityCallInvocation,
    code: str,
    message: str,
    *,
    model_telemetry: dict[str, Any] | None,
    ctx: CapabilityExecutionContext,
) -> CapabilityCallResult:
    return invalid_tool_result(
        invocation.call_id,
        invocation.name,
        invocation.params,
        code,
        message,
        call_event=invocation.call_event,
        model_telemetry=model_telemetry,
        ctx=ctx,
    )


async def run_valid_tool_call(
    invocation: CapabilityCallInvocation,
    *,
    capability_id: str,
    execution_mode: ExecutionMode,
    model_telemetry: dict[str, Any] | None,
    ctx: CapabilityExecutionContext,
) -> CapabilityCallResult:
    events: list[DraftEvent] = []
    call_event = invocation.call_event
    call_event["capability_id"] = capability_id
    emit_capability_tool_event(
        events,
        call_event,
        ctx=ctx,
    )
    try:
        invoked = invoke_hosted_capability(
            capability_id,
            invocation.params,
            execution_mode=execution_mode,
            ctx=ctx,
        )
        result = await invoked if inspect.isawaitable(invoked) else invoked
    except Exception as exc:
        result = tool_error("tool-crashed", f"{type(exc).__name__}: {exc}")
    staged_effect = proposed_effect(result)
    stop = bool(
        execution_mode == "stage"
        and staged_effect is not None
        and result.get("ok") is True
    )
    result_event = tool_result_event_payload(
        invocation.call_id,
        invocation.name,
        result,
        capability_id=capability_id,
        model_telemetry=model_telemetry,
    )
    if isinstance(call_event.get("caused_by"), str):
        result_event["caused_by"] = call_event["caused_by"]
    emit_capability_tool_event(events, result_event, ctx=ctx)
    return CapabilityCallResult(
        events=events,
        staged_effect=staged_effect,
        stop=stop,
    )


async def invoke_capability(
    capability_id: str,
    params: dict[str, Any],
    *,
    execution_mode: ExecutionMode = "stage",
    tool_registry: CapabilityRegistry | None = None,
) -> dict[str, Any]:
    active_tool_registry = tool_registry or _default_tool_registry
    return await active_tool_registry.invoke_async(
        capability_id,
        params,
        execution_mode=execution_mode,
    )


async def invoke_hosted_capability(
    capability_id: str,
    params: dict[str, Any],
    *,
    execution_mode: ExecutionMode = "stage",
    ctx: CapabilityExecutionContext,
) -> dict[str, Any]:
    tool_hosts = ctx.tool_hosts or HostDirectory.from_registry(ctx.tool_registry)
    host = tool_hosts.host_for(capability_id)
    if host is None:
        return error_result(
            "unknown-tool",
            f"unknown tool: {capability_id}",
            data={"capability_id": capability_id},
        )
    result = await host.call(capability_id, params, execution_mode, ctx)
    return validated_capability_result_payload(capability_id, result)


def parse_tool_arguments(arguments: Any) -> tuple[dict[str, Any], str]:
    if isinstance(arguments, dict):
        return cast(dict[str, Any], arguments), ""
    if not isinstance(arguments, str):
        return {}, "function arguments were not a JSON object string"
    try:
        params = json.loads(arguments or "{}")
    except json.JSONDecodeError as exc:
        return {}, str(exc)
    if not isinstance(params, dict):
        return {}, "function arguments JSON was not an object"
    return cast(dict[str, Any], params), ""


def invalid_tool_result(
    call_id: str,
    name: str,
    params: dict[str, Any],
    code: str,
    message: str,
    *,
    call_event: dict[str, Any] | None = None,
    model_telemetry: dict[str, Any] | None = None,
    caused_by: str | None = None,
    ctx: CapabilityExecutionContext,
) -> CapabilityCallResult:
    event = call_event or {
        "type": "tool_call",
        "id": call_id,
        "tool_call_id": call_id,
        "name": name,
        "input": params,
    }
    if caused_by is not None:
        event["caused_by"] = caused_by
    events: list[DraftEvent] = []
    result_event = tool_result_event_payload(
        call_id,
        name,
        tool_error(code, message),
        model_telemetry=model_telemetry,
    )
    if isinstance(event.get("caused_by"), str):
        result_event["caused_by"] = event["caused_by"]
    emit_capability_tool_event(
        events,
        event,
        ctx=ctx,
    )
    emit_capability_tool_event(
        events,
        result_event,
        ctx=ctx,
    )
    return CapabilityCallResult(events=events)


def tool_result_event_payload(
    call_id: str,
    name: str,
    result: dict[str, Any],
    *,
    capability_id: str = "",
    model_telemetry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "tool_result",
        "tool_call_id": call_id,
        "status": tool_result_status(result),
        "name": name,
        "result": normalized_tool_result(name, result),
    }
    ensure_runtime_event_id(event)
    if capability_id:
        event["capability_id"] = capability_id
    if model_telemetry:
        event["model_telemetry"] = dict(model_telemetry)
    return event


def emit_capability_tool_event(
    events: list[DraftEvent],
    event: dict[str, Any],
    *,
    ctx: CapabilityExecutionContext,
) -> None:
    emit_capability_event_draft(
        events, draft_from_runtime_event(event, session_id=None, turn_id=None), ctx
    )


def emit_capability_event_draft(
    events: list[DraftEvent],
    draft: DraftEvent,
    ctx: CapabilityExecutionContext,
) -> DraftEvent:
    events.append(draft)
    if ctx.event_sink is not None:
        ctx.event_sink(draft)
    else:
        project_prompt_trace_projection(events, ctx.trace_store)
    return draft


def tool_error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}
