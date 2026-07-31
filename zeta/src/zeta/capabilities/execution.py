"""Execute model-requested capability calls."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from zeta.capabilities.executors import ToolExecutor
from zeta.capabilities.registry import (
    CapabilityRegistry,
    CapabilityToolSchema,
    validated_capability_result_payload,
)
from zeta.effects import DeliverySemantics, effect_key
from zeta.events import DraftEvent
from zeta.journal.drafts import draft_from_runtime_event, ensure_runtime_event_id
from zeta.journal.tool_results import normalized_tool_result, tool_result_status
from zeta.models.types import tool_call_id
from zeta.substrate import Store
from zeta.trace.provenance import project_prompt_trace_projection


def diagnostic(
    code: str, message: str, *, severity: str = "unsupported"
) -> dict[str, str]:
    return {"code": code, "message": message, "severity": severity}


CapabilityEventSink = Callable[[DraftEvent], None]


@dataclass(frozen=True)
class CapabilityExecutionContext:
    event_sink: CapabilityEventSink | None
    trace_store: Store | None
    tool_registry: CapabilityRegistry
    tool_executor: ToolExecutor
    base_dir: Path | None = None
    effect_scope: str | None = None
    effect_key: str | None = None


@dataclass(frozen=True)
class CapabilityCallResult:
    events: list[DraftEvent]
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
                    f"tool is not allowed for this run: {invocation.name}",
                )
            )
        return ToolCallValidation(
            error=("unknown-tool", f"unknown tool: {invocation.name}")
        )
    if capability_id not in allowed_capabilities:
        return ToolCallValidation(
            error=(
                "disallowed-tool",
                f"tool is not allowed for this run: {invocation.name}",
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
        Draft202012Validator.check_schema(schema)
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
    semantics = capability_delivery_semantics(capability_id, ctx=ctx)
    operation_key = None
    invocation_ctx = ctx
    if semantics is not None:
        scope = ctx.effect_scope or invocation.call_id
        operation_key = effect_key(scope, capability_id, invocation.params)
        invocation_ctx = replace(ctx, effect_key=operation_key)
        emit_capability_effect_event(
            events,
            "planned",
            capability_id=capability_id,
            params=invocation.params,
            effect_key=operation_key,
            semantics=semantics,
            scope=scope,
            caused_by=invocation.call_id,
            ctx=ctx,
        )
        emit_capability_effect_event(
            events,
            "started",
            capability_id=capability_id,
            params=invocation.params,
            effect_key=operation_key,
            semantics=semantics,
            scope=scope,
            caused_by=invocation.call_id,
            ctx=ctx,
        )
    try:
        invoked = invoke_tool_executor(
            capability_id,
            invocation.params,
            ctx=invocation_ctx,
        )
        result = await invoked if inspect.isawaitable(invoked) else invoked
    except Exception as exc:
        result = tool_error("tool-crashed", f"{type(exc).__name__}: {exc}")
    stop = bool(result.get("ok") is True and result.get("stop") is True)
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
    if operation_key is not None and semantics is not None:
        if result.get("ok") is True:
            effect_status = "completed"
        elif semantics == "unsafe_to_retry":
            effect_status = "ambiguous"
        else:
            effect_status = "failed"
        emit_capability_effect_event(
            events,
            effect_status,
            capability_id=capability_id,
            params=invocation.params,
            effect_key=operation_key,
            semantics=semantics,
            scope=ctx.effect_scope or invocation.call_id,
            caused_by=invocation.call_id,
            result=result,
            ctx=ctx,
        )
    return CapabilityCallResult(
        events=events,
        stop=stop,
    )


def emit_capability_effect_event(
    events: list[DraftEvent],
    status: str,
    *,
    capability_id: str,
    params: dict[str, Any],
    effect_key: str,
    semantics: DeliverySemantics,
    scope: str,
    caused_by: str,
    ctx: CapabilityExecutionContext,
    result: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "effect_key": effect_key,
        "operation": capability_id,
        "semantics": semantics,
        "scope": scope,
        "queue_item_id": scope if scope.startswith("qi_") else None,
        "params": params,
        "status": status,
    }
    if result is not None:
        payload["result"] = result
    emit_capability_event_draft(
        events,
        DraftEvent(
            f"runtime.effect.{status}",
            f"capability:{capability_id}",
            payload,
            idempotency_key=f"runtime.effect.{status}:{effect_key}",
            caused_by=caused_by,
        ),
        ctx,
    )


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


async def invoke_tool_executor(
    capability_id: str,
    params: dict[str, Any],
    *,
    ctx: CapabilityExecutionContext,
) -> dict[str, Any]:
    result = await ctx.tool_executor.call(
        capability_id,
        params,
        base_dir=ctx.base_dir,
        effect_key=ctx.effect_key,
    )
    return validated_capability_result_payload(capability_id, result)


def capability_delivery_semantics(
    capability_id: str,
    *,
    ctx: CapabilityExecutionContext,
) -> DeliverySemantics | None:
    capability = ctx.tool_registry.get(capability_id)
    if capability is None:
        return None
    return capability.declaration.delivery_semantics
