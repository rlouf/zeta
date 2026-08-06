"""Run one capability call.

This stage executes a model-requested tool call and records its result,
including the terminal statuses that end a call.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from jsonschema import Draft202012Validator

from zeta.capabilities.execution import (
    CapabilityCallResult,
    CapabilityExecutionContext,
    handle_tool_call,
)
from zeta.capabilities.registry import (
    CapabilityToolSchema,
)
from zeta.context.builder import PromptBuilder, render_model_input
from zeta.context.components import PromptComponent
from zeta.context.transforms import (
    ContentConflict,
    ContentTransformInput,
    ContentTransformResult,
    ContentValidationError,
)
from zeta.events import DraftEvent
from zeta.ids import publish_event_handle, wait_handle
from zeta.journal.views import (
    draft_timeline_type,
)
from zeta.loop.config import AgentConfig
from zeta.loop.gateway import request_assistant_message
from zeta.loop.outcomes import (
    CancelRequest,
    PublishEventRequest,
    RunState,
    WaitRequest,
)
from zeta.loop.request import RunDependencies
from zeta.loop.stages.abort import check_run_abort
from zeta.models.types import tool_call_id
from zeta.substrate import Derivation, Object, ObjectId

TERMINAL_TOOL_STATUSES = {"completed", "failed", "refused", "cancelled", "timed_out"}
PUBLISH_EVENT_CAPABILITY_ID = "zeta.publish_event"
WAIT_FOR_CAPABILITY_ID = "zeta.wait_for"
CANCEL_CAPABILITY_ID = "zeta.cancel"
QUERY_CONTENT_CAPABILITY_ID = "zeta.query_content"
TRANSFORM_CONTENT_CAPABILITY_ID = "zeta.transform_content"


@dataclass(frozen=True)
class _ChildModelResult:
    content: str
    assistant_id: ObjectId


def terminal_capability_result_event(
    events: list[DraftEvent],
    call_id: str,
) -> DraftEvent | None:
    for draft in reversed(events):
        if draft_timeline_type(draft) != "tool_result":
            continue
        if draft.payload.get("tool_call_id") != call_id:
            continue
        if draft.payload.get("status") in TERMINAL_TOOL_STATUSES:
            return draft
    return None


async def run_capability_step(
    tool_call: dict[str, Any],
    *,
    index: int,
    config: AgentConfig,
    allowed_capabilities: tuple[str, ...],
    tool_schema: CapabilityToolSchema,
    model_telemetry: dict[str, Any] | None,
    assistant_event_id: str | None,
    state: RunState,
    ctx: RunDependencies,
    position: int | None = None,
) -> CapabilityCallResult:
    position = index if position is None else position
    state.note_step("check_budget")
    check_run_abort(
        state,
        ctx=ctx,
    )
    if (
        terminal_capability_result_event(
            state.events,
            tool_call_id(tool_call, index=index),
        )
        is not None
    ):
        state.note_step("record_capability_result")
        return CapabilityCallResult(events=[])
    state.note_step("record_capability_call")
    state.note_step("execute_capability")

    async def run_internal_tool(
        capability_id: str,
        params: dict[str, Any],
    ) -> dict[str, Any] | None:
        if capability_id == PUBLISH_EVENT_CAPABILITY_ID:
            return request_published_event(
                params,
                position=position,
                state=state,
                ctx=ctx,
            )
        if capability_id == WAIT_FOR_CAPABILITY_ID:
            return request_wait(
                params,
                position=position,
                state=state,
                ctx=ctx,
            )
        if capability_id == CANCEL_CAPABILITY_ID:
            return request_cancellation(
                params,
                position=position,
                state=state,
                ctx=ctx,
            )
        if capability_id == QUERY_CONTENT_CAPABILITY_ID:
            return request_content_query(params, ctx=ctx)
        if capability_id == TRANSFORM_CONTENT_CAPABILITY_ID:
            return await request_content_transform(
                params,
                config=config,
                state=state,
                ctx=ctx,
            )
        return None

    capability_ctx = CapabilityExecutionContext(
        event_sink=ctx.event_sink,
        trace_store=ctx.builder.store(),
        tool_registry=ctx.tool_registry,
        tool_executor=ctx.tool_executor,
        base_dir=config.base_dir,
        effect_scope=config.effect_scope,
        query_log_reader=ctx.query_log_reader,
        internal_tool_executor=run_internal_tool,
    )
    handled = handle_tool_call(
        tool_call,
        allowed_capabilities=allowed_capabilities,
        tool_schema=tool_schema,
        index=index,
        model_telemetry=model_telemetry,
        caused_by=assistant_event_id,
        ctx=capability_ctx,
    )
    result = await handled if inspect.isawaitable(handled) else handled
    state.note_step("record_capability_result")
    return result


def request_content_query(
    params: dict[str, Any],
    *,
    ctx: RunDependencies,
) -> dict[str, Any] | None:
    workspace = ctx.content_workspace
    if workspace is None:
        return None
    try:
        result = workspace.query(
            key_prefix=params.get("key_prefix"),
            kind=params.get("kind"),
            source_scope=params.get("source_scope"),
            limit=params.get("limit", 20),
            cursor=params.get("cursor"),
        )
    except ContentValidationError as exc:
        return publish_event_error("invalid-content-query", str(exc))
    return {"ok": True, **result}


async def request_content_transform(
    params: dict[str, Any],
    *,
    config: AgentConfig,
    state: RunState,
    ctx: RunDependencies,
) -> dict[str, Any] | None:
    workspace = ctx.content_workspace
    if workspace is None:
        return None
    try:
        operation = params.get("transformation")
        if isinstance(operation, Mapping) and operation.get("type") == "model":
            result = await model_content_transform(
                params,
                config=config,
                ctx=ctx,
            )
        else:
            result = workspace.transform(params)
    except ContentConflict as exc:
        return publish_event_error("content-conflict", str(exc))
    except ContentValidationError as exc:
        return publish_event_error("invalid-content-transform", str(exc))
    state.content_promotions.extend(result.promotions)
    return {
        "ok": True,
        "status": "applied",
        "active_scope": "run",
        "head": result.head,
        "object_ids": list(result.output_ids),
        "promotions": [
            {
                "scope": promotion.scope,
                "key": promotion.key,
                "status": "requested",
            }
            for promotion in result.promotions
        ],
    }


async def model_content_transform(
    params: Mapping[str, Any],
    *,
    config: AgentConfig,
    ctx: RunDependencies,
) -> ContentTransformResult:
    workspace = ctx.content_workspace
    if workspace is None:
        raise ContentValidationError("content workspace is unavailable")
    operation = params.get("transformation")
    if not isinstance(operation, Mapping):
        raise ContentValidationError("content transformation must be an object")
    instruction = operation.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip():
        raise ContentValidationError("model transformation requires an instruction")
    mode = operation.get("mode", "one")
    if mode not in {"one", "map", "reduce"}:
        raise ContentValidationError(f"unsupported model transformation mode {mode!r}")
    inputs = workspace.transform_inputs(params)
    groups = _model_transform_groups(inputs, str(mode))
    input_chars = sum(
        len(_content_transform_input_text(item)) for group in groups for item in group
    )
    concurrency = ctx.content_transform_budget.reserve_model_calls(
        calls=len(groups),
        input_chars=input_chars,
    )
    requested_concurrency = operation.get("max_concurrency", concurrency)
    if (
        not isinstance(requested_concurrency, int)
        or isinstance(requested_concurrency, bool)
        or requested_concurrency < 1
    ):
        raise ContentValidationError("model max_concurrency must be a positive integer")
    semaphore = asyncio.Semaphore(min(concurrency, requested_concurrency))

    async def transform_one(
        group: tuple[ContentTransformInput, ...],
    ) -> _ChildModelResult:
        async with semaphore:
            return await _request_child_model_transform(
                group,
                instruction=instruction,
                mode=str(mode),
                config=config,
                ctx=ctx,
            )

    children = await asyncio.gather(*(transform_one(group) for group in groups))
    assistant_ids = tuple(child.assistant_id for child in children)
    value: Any
    if mode == "map":
        destination = params.get("destination")
        if (
            not isinstance(destination, Mapping)
            or destination.get("kind") != "collection"
        ):
            raise ContentValidationError(
                "model map destination kind must be collection"
            )
        value = {"object_ids": list(assistant_ids)}
    else:
        value = children[0].content
    return workspace.store_transformed_value(
        params,
        value,
        source_ids=assistant_ids,
        producer="ModelTransform:v1",
        producer_params={"instruction": instruction, "mode": mode},
    )


def _model_transform_groups(
    inputs: tuple[ContentTransformInput, ...],
    mode: str,
) -> tuple[tuple[ContentTransformInput, ...], ...]:
    if mode == "map":
        if not inputs:
            raise ContentValidationError("model map requires content inputs")
        return tuple((item,) for item in inputs)
    return (inputs,)


async def _request_child_model_transform(
    inputs: tuple[ContentTransformInput, ...],
    *,
    instruction: str,
    mode: str,
    config: AgentConfig,
    ctx: RunDependencies,
) -> _ChildModelResult:
    workspace = ctx.content_workspace
    if workspace is None:
        raise ContentValidationError("content workspace is unavailable")
    builder = PromptBuilder(store=workspace.store)
    components = tuple(
        PromptComponent(
            kind="content_transform_input",
            data={
                "key": item.key,
                "kind": item.node.kind,
                "object_id": item.object_id,
            },
            message={"role": "system", "content": _content_transform_input_text(item)},
            source_object_id=item.object_id,
            links=(item.object_id,),
        )
        for item in inputs
    )
    plan = builder.plan_prompt(
        instruction,
        [],
        system=(
            "Transform only the supplied content. Return only the transformed content."
        ),
        content_components=components,
        tools=[],
        selected_model=config.model_name,
        thinking=config.thinking,
    )
    stored = await builder.commit_prompt_plan(plan)
    model_output, _streamed, telemetry = await request_assistant_message(
        render_model_input(stored),
        config=config,
        model_gateway=ctx.model_gateway,
        should_stop=ctx.abort_reason,
    )
    content = model_output.message.get("content")
    if not isinstance(content, str):
        raise ContentValidationError("model transformation returned no text")
    ctx.content_transform_budget.record_model_output(len(content))
    if stored.prompt_object_id is None:
        raise ContentValidationError("model transformation prompt was not stored")
    message = dict(model_output.message)
    assistant_id = workspace.store.put_object(
        Object(
            kind="assistant_message",
            schema="zeta.model_output.v1",
            data={
                "message": message,
                "model_output": {"message": message},
                "telemetry": telemetry,
            },
            links=(stored.prompt_object_id,),
        )
    )
    workspace.store.record_derivation(
        Derivation(
            producer="ModelResponse",
            output_id=assistant_id,
            input_ids=(stored.prompt_object_id,),
            params={"mode": mode},
        )
    )
    return _ChildModelResult(content, assistant_id)


def _content_transform_input_text(item: ContentTransformInput) -> str:
    content = item.node.content
    rendered = (
        content
        if isinstance(content, str)
        else json.dumps(
            content,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )
    return f"Content key: {item.key}\nKind: {item.node.kind}\n{rendered}"


def request_published_event(
    params: dict[str, Any],
    *,
    position: int,
    state: RunState,
    ctx: RunDependencies,
) -> dict[str, Any]:
    event_type = params["event_type"]
    schema = ctx.publishable_events.get(event_type)
    if event_type not in ctx.publishable_events:
        return publish_event_error(
            "undeclared-event-type",
            f"the agent does not list event {event_type!r} in publishes",
        )
    payload = params["payload"]
    if schema is not None:
        errors = sorted(
            Draft202012Validator(schema).iter_errors(payload),
            key=lambda error: list(error.path),
        )
        if errors:
            return publish_event_error("invalid-event-payload", errors[0].message)
    at = normalized_publish_time(params.get("at"))
    if isinstance(at, dict):
        return at
    if ctx.source_queue_item_id is None:
        return publish_event_error(
            "missing-event-source",
            "the run does not have a source queue item",
        )
    handle = publish_event_handle(ctx.source_queue_item_id, position)
    state.publish_event_requests.append(
        PublishEventRequest(
            handle=handle,
            event_type=event_type,
            payload=dict(payload),
            at=at,
            position=position,
        )
    )
    return {"ok": True, "handle": handle}


def request_wait(
    params: dict[str, Any],
    *,
    position: int,
    state: RunState,
    ctx: RunDependencies,
) -> dict[str, Any]:
    if ctx.source_queue_item_id is None:
        return publish_event_error(
            "missing-wait-source",
            "the run does not have a source queue item",
        )
    deadline = normalized_wait_deadline(params.get("deadline"))
    if isinstance(deadline, dict):
        return deadline
    handle = wait_handle(ctx.source_queue_item_id, position)
    state.wait_requests.append(
        WaitRequest(
            handle=handle,
            event_type=params["event_type"],
            fields=dict(params.get("fields") or {}),
            deadline=deadline,
            position=position,
        )
    )
    return {"ok": True, "handle": handle, "stop": True}


def request_cancellation(
    params: dict[str, Any],
    *,
    position: int,
    state: RunState,
    ctx: RunDependencies,
) -> dict[str, Any]:
    if ctx.source_agent_id is None or ctx.source_session_id is None:
        return publish_event_error(
            "missing-cancel-source",
            "the run does not have an authored agent session",
        )
    handle = params["handle"]
    state.cancel_requests.append(
        CancelRequest(
            handle=handle,
            reason=params.get("reason"),
            source_agent_id=ctx.source_agent_id,
            source_session_id=ctx.source_session_id,
            position=position,
        )
    )
    return {"ok": True, "handle": handle, "status": "requested"}


def normalized_wait_deadline(value: Any) -> str | None | dict[str, Any]:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return publish_event_error(
            "invalid-wait-deadline",
            "deadline must be an ISO 8601 date-time with a UTC offset",
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return publish_event_error(
            "invalid-wait-deadline",
            "deadline must include a UTC offset",
        )
    return parsed.astimezone(UTC).isoformat()


def normalized_publish_time(value: Any) -> str | None | dict[str, Any]:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return publish_event_error(
            "invalid-publish-time",
            "at must be an ISO 8601 date-time with a UTC offset",
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return publish_event_error(
            "invalid-publish-time",
            "at must include a UTC offset",
        )
    return parsed.astimezone(UTC).isoformat()


def publish_event_error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}
