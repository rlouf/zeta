"""Run one capability call.

This stage executes a model-requested tool call and records its result,
including the terminal statuses that end a call.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import sys
import time
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from typing import Any, cast

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
    ContentNode,
    ContentTransformInput,
    ContentTransformResult,
    ContentValidationError,
    put_content_node,
)
from zeta.events import DraftEvent
from zeta.journal.views import (
    draft_timeline_type,
)
from zeta.loop.cancellation import AbortReason
from zeta.loop.config import AgentConfig
from zeta.loop.gateway import request_assistant_message
from zeta.loop.outcomes import (
    RunState,
)
from zeta.loop.request import RunDependencies
from zeta.loop.stages.abort import check_run_abort
from zeta.models.types import tool_call_id
from zeta.substrate import Derivation, Object, ObjectId, Store
from zeta.tools.events import (
    EventToolBindings,
    bind_event_tools,
)
from zeta.tools.events import (
    event_tool_error as publish_event_error,
)

TERMINAL_TOOL_STATUSES = {"completed", "failed", "refused", "cancelled", "timed_out"}
QUERY_CONTENT_CAPABILITY_ID = "zeta.query_content"
TRANSFORM_CONTENT_CAPABILITY_ID = "zeta.transform_content"
FINISH_CAPABILITY_ID = "zeta.finish"


@dataclass(frozen=True)
class _ChildModelResult:
    content: str
    assistant_id: ObjectId


@dataclass(frozen=True)
class _DerivedModelResult:
    value: Any
    assistant_ids: tuple[ObjectId, ...]
    instruction: str
    mode: str


@dataclass(frozen=True)
class _PythonContentValue:
    key: str
    object_id: ObjectId
    node: ContentNode


class _PythonContentContext:
    """Give a Python transform only the immutable values selected by its caller."""

    def __init__(self, values: tuple[_PythonContentValue, ...]) -> None:
        self._values = values

    def select(
        self,
        *,
        keys: list[str] | tuple[str, ...] | None = None,
        kind: str | None = None,
    ) -> list[_PythonContentValue]:
        selected_keys = None if keys is None else set(keys)
        return [
            value
            for value in self._values
            if (selected_keys is None or value.key in selected_keys)
            and (kind is None or value.node.kind == kind)
        ]

    def get(self, key: str) -> _PythonContentValue:
        for value in self._values:
            if value.key == key:
                return value
        raise KeyError(key)


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
    event_tools = bind_event_tools(
        EventToolBindings(
            position=position,
            publishable_events=ctx.publishable_events,
            source_queue_item_id=ctx.source_queue_item_id,
            source_agent_id=ctx.source_agent_id,
            source_session_id=ctx.source_session_id,
            publish_event_requests=state.publish_event_requests,
            wait_requests=state.wait_requests,
            cancel_requests=state.cancel_requests,
        )
    )

    async def run_internal_tool(
        capability_id: str,
        params: dict[str, Any],
    ) -> dict[str, Any] | None:
        event_tool = event_tools.get(capability_id)
        if event_tool is not None:
            event_result = event_tool(params)
            if inspect.isawaitable(event_result):
                return await cast(Awaitable[dict[str, Any]], event_result)
            return event_result
        if capability_id == QUERY_CONTENT_CAPABILITY_ID:
            return request_content_query(params, ctx=ctx)
        if capability_id == TRANSFORM_CONTENT_CAPABILITY_ID:
            return await request_content_transform(
                params,
                position=position,
                config=config,
                state=state,
                ctx=ctx,
            )
        if capability_id == FINISH_CAPABILITY_ID:
            return request_content_finish(params, state=state, ctx=ctx)
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


def request_content_finish(
    params: dict[str, Any],
    *,
    state: RunState,
    ctx: RunDependencies,
) -> dict[str, Any] | None:
    workspace = ctx.content_workspace
    if workspace is None:
        return None
    try:
        result = workspace.finish(params["object_id"])
    except ContentValidationError as exc:
        return publish_event_error("invalid-finish-object", str(exc))
    state.final_object_id = result.object_id
    state.selected_final_answer = result.content
    return {
        "ok": True,
        "stop": True,
        "object_id": result.object_id,
    }


async def request_content_transform(
    params: dict[str, Any],
    *,
    position: int,
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
                position=position,
                config=config,
                ctx=ctx,
            )
        elif isinstance(operation, Mapping) and operation.get("type") == "python":
            result = await python_content_transform(
                params,
                position=position,
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
    position: int,
    config: AgentConfig,
    ctx: RunDependencies,
) -> ContentTransformResult:
    workspace = ctx.content_workspace
    if workspace is None:
        raise ContentValidationError("content workspace is unavailable")
    operation = params.get("transformation")
    if not isinstance(operation, Mapping):
        raise ContentValidationError("content transformation must be an object")
    inputs = workspace.transform_inputs(params)
    destination = params.get("destination")
    if not isinstance(destination, Mapping):
        raise ContentValidationError("content destination must be an object")
    derived = await _derive_model_content(
        inputs,
        operation,
        retry_seed=_content_transform_retry_seed(
            inputs,
            operation,
            destination,
            position=position,
            config=config,
            ctx=ctx,
        ),
        config=config,
        ctx=ctx,
    )
    if derived.mode == "map":
        if destination.get("kind") != "collection":
            raise ContentValidationError(
                "model map destination kind must be collection"
            )
    return workspace.store_transformed_value(
        params,
        derived.value,
        source_ids=derived.assistant_ids,
        producer="ModelTransform:v1",
        producer_params={
            "instruction": derived.instruction,
            "mode": derived.mode,
        },
    )


async def _derive_model_content(
    inputs: tuple[ContentTransformInput, ...],
    operation: Mapping[str, Any],
    *,
    retry_seed: str,
    config: AgentConfig,
    ctx: RunDependencies,
) -> _DerivedModelResult:
    workspace = ctx.content_workspace
    if workspace is None:
        raise ContentValidationError("content workspace is unavailable")
    instruction = operation.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip():
        raise ContentValidationError("model transformation requires an instruction")
    mode = operation.get("mode", "one")
    if mode not in {"one", "map", "reduce"}:
        raise ContentValidationError(f"unsupported model transformation mode {mode!r}")
    groups = _model_transform_groups(inputs, str(mode))
    cache_refs = tuple(
        _child_model_cache_ref(retry_seed, index) for index in range(len(groups))
    )
    cached = tuple(
        _cached_child_model_result(workspace.store, cache_ref)
        for cache_ref in cache_refs
    )
    concurrency, requested_concurrency = _model_transform_concurrency(
        groups,
        cached,
        operation,
        ctx=ctx,
    )
    semaphore = asyncio.Semaphore(min(concurrency, requested_concurrency))

    async def transform_one(
        index: int,
        group: tuple[ContentTransformInput, ...],
    ) -> _ChildModelResult:
        reused = cached[index]
        if reused is not None:
            return reused
        async with semaphore:
            return await _request_child_model_transform(
                group,
                instruction=instruction,
                mode=str(mode),
                cache_ref=cache_refs[index],
                config=config,
                ctx=ctx,
            )

    children = await asyncio.gather(
        *(transform_one(index, group) for index, group in enumerate(groups))
    )
    assistant_ids = tuple(child.assistant_id for child in children)
    if mode == "map":
        value = {"object_ids": list(assistant_ids)}
    else:
        value = children[0].content
    return _DerivedModelResult(
        value=value,
        assistant_ids=assistant_ids,
        instruction=instruction,
        mode=str(mode),
    )


def _model_transform_concurrency(
    groups: tuple[tuple[ContentTransformInput, ...], ...],
    cached: tuple[_ChildModelResult | None, ...],
    operation: Mapping[str, Any],
    *,
    ctx: RunDependencies,
) -> tuple[int, int]:
    missing = tuple(index for index, child in enumerate(cached) if child is None)
    input_chars = sum(
        len(_content_transform_input_text(item))
        for index in missing
        for item in groups[index]
    )
    concurrency = (
        ctx.content_transform_budget.reserve_model_calls(
            calls=len(missing),
            input_chars=input_chars,
        )
        if missing
        else 1
    )
    requested = operation.get("max_concurrency", concurrency)
    if not isinstance(requested, int) or isinstance(requested, bool) or requested < 1:
        raise ContentValidationError("model max_concurrency must be a positive integer")
    return concurrency, requested


async def python_content_transform(
    params: Mapping[str, Any],
    *,
    position: int,
    config: AgentConfig,
    ctx: RunDependencies,
) -> ContentTransformResult:
    workspace = ctx.content_workspace
    if workspace is None:
        raise ContentValidationError("content workspace is unavailable")
    operation = params.get("transformation")
    if not isinstance(operation, Mapping):
        raise ContentValidationError("content transformation must be an object")
    source = operation.get("source")
    if not isinstance(source, str) or not source.strip():
        raise ContentValidationError("python transformation requires source")
    if len(source.encode("utf-8")) > 131_072:
        raise ContentValidationError("python transformation source is too large")
    timeout = operation.get("timeout_seconds", 30)
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or timeout <= 0
        or timeout > 300
    ):
        raise ContentValidationError(
            "python timeout_seconds must be greater than 0 and at most 300"
        )
    selected = workspace.transform_inputs(params)
    destination = params.get("destination")
    if not isinstance(destination, Mapping):
        raise ContentValidationError("content destination must be an object")
    retry_seed = _content_transform_retry_seed(
        selected,
        operation,
        destination,
        position=position,
        config=config,
        ctx=ctx,
    )
    values = tuple(
        _PythonContentValue(item.key, item.object_id, item.node) for item in selected
    )
    program_id = workspace.store.put_object(
        Object(
            kind="python_program",
            schema="zeta.python_transform.v1",
            data={"source": source},
            links=tuple(item.object_id for item in selected),
        )
    )
    parent_loop = asyncio.get_running_loop()

    async def nested_transform(request: dict[str, Any]) -> _PythonContentValue:
        return await _nested_python_transform(
            request,
            parent_retry_seed=retry_seed,
            config=config,
            ctx=ctx,
        )

    output = await asyncio.wait_for(
        asyncio.to_thread(
            _run_python_program,
            source,
            _PythonContentContext(values),
            nested_transform,
            parent_loop,
            float(timeout),
            ctx.abort_reason,
        ),
        timeout=float(timeout) + 1,
    )
    source_ids: tuple[ObjectId, ...] = (program_id,)
    if isinstance(output, _PythonContentValue):
        value = output.node.content
        source_ids = (program_id, output.object_id)
    else:
        value = output
    return workspace.store_transformed_value(
        params,
        value,
        source_ids=source_ids,
        producer="PythonTransform:v1",
        producer_params={"program_id": program_id, "timeout_seconds": timeout},
    )


def _run_python_program(
    source: str,
    content_ctx: _PythonContentContext,
    nested_transform: Any,
    parent_loop: asyncio.AbstractEventLoop,
    timeout: float,
    should_stop: AbortReason,
) -> Any:
    deadline = time.monotonic() + timeout

    def trace_python(_frame: Any, _event: str, _argument: Any) -> Any:
        reason = should_stop()
        if reason is not None:
            raise ContentValidationError(
                f"python transformation stopped because the run is {reason}"
            )
        if time.monotonic() >= deadline:
            raise ContentValidationError("python transformation timed out")
        return trace_python

    sys.settrace(trace_python)
    try:
        namespace: dict[str, Any] = {"__name__": "zeta_content_transform"}
        exec(compile(source, "<zeta-content-transform>", "exec"), namespace)
        main = namespace.get("main")
        if not callable(main):
            raise ContentValidationError(
                "python transformation must define a callable main(ctx, transform)"
            )

        async def transform(**request: Any) -> _PythonContentValue:
            future = asyncio.run_coroutine_threadsafe(
                nested_transform(dict(request)),
                parent_loop,
            )
            return await asyncio.wrap_future(future)

        async def invoke() -> Any:
            result = main(content_ctx, transform)
            return await result if inspect.isawaitable(result) else result

        return asyncio.run(asyncio.wait_for(invoke(), timeout=timeout))
    finally:
        sys.settrace(None)


async def _nested_python_transform(
    request: Mapping[str, Any],
    *,
    parent_retry_seed: str,
    config: AgentConfig,
    ctx: RunDependencies,
) -> _PythonContentValue:
    workspace = ctx.content_workspace
    if workspace is None:
        raise ContentValidationError("content workspace is unavailable")
    values = _python_transform_values(request.get("inputs"))
    operation = request.get("transformation")
    if not isinstance(operation, Mapping) or operation.get("type") != "model":
        raise ContentValidationError(
            "nested Python transform must use a model transformation"
        )
    destination = request.get("destination")
    if not isinstance(destination, Mapping):
        raise ContentValidationError("nested Python destination must be an object")
    key = destination.get("key")
    kind = destination.get("kind")
    if (
        not isinstance(key, str)
        or not key
        or not isinstance(kind, str)
        or not kind
        or destination.get("scope") != "run"
    ):
        raise ContentValidationError(
            "nested Python destination requires a key, kind, and run scope"
        )
    inputs = tuple(
        ContentTransformInput(value.key, value.object_id, value.node)
        for value in values
    )
    derived = await _derive_model_content(
        inputs,
        operation,
        retry_seed=_content_transform_retry_seed(
            inputs,
            operation,
            destination,
            position=None,
            config=config,
            ctx=ctx,
            parent_retry_seed=parent_retry_seed,
        ),
        config=config,
        ctx=ctx,
    )
    if derived.mode == "map" and kind != "collection":
        raise ContentValidationError(
            "nested model map destination kind must be collection"
        )
    links = tuple(
        dict.fromkeys(
            (*tuple(value.object_id for value in values), *derived.assistant_ids)
        )
    )
    node = ContentNode(key, kind, derived.value)
    object_id = put_content_node(workspace.store, node, links=links)
    workspace.store.record_derivation(
        Derivation(
            producer="ModelTransform:v1",
            output_id=object_id,
            input_ids=links,
            params={
                "instruction": derived.instruction,
                "mode": derived.mode,
                "nested": True,
            },
        )
    )
    return _PythonContentValue(key, object_id, node)


def _python_transform_values(value: Any) -> tuple[_PythonContentValue, ...]:
    if isinstance(value, _PythonContentValue):
        return (value,)
    if isinstance(value, (list, tuple)) and all(
        isinstance(item, _PythonContentValue) for item in value
    ):
        return tuple(value)
    raise ContentValidationError(
        "nested Python inputs must come from ctx or an earlier transform"
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


def _content_transform_retry_seed(
    inputs: tuple[ContentTransformInput, ...],
    operation: Mapping[str, Any],
    destination: Mapping[str, Any],
    *,
    position: int | None,
    config: AgentConfig,
    ctx: RunDependencies,
    parent_retry_seed: str | None = None,
) -> str:
    workspace = ctx.content_workspace
    if workspace is None:
        raise ContentValidationError("content workspace is unavailable")
    parent = (
        parent_retry_seed
        or ctx.source_queue_item_id
        or f"run:{workspace.run_head.scope_id}"
    )
    payload = {
        "parent": parent,
        "position": position,
        "input_ids": [item.object_id for item in inputs],
        "transformation": dict(operation),
        "destination": dict(destination),
        "model": {
            "profile": config.model_profile,
            "name": config.model_name,
            "url": config.model_url,
            "thinking": config.thinking,
            "api": config.model_api,
        },
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _child_model_cache_ref(retry_seed: str, position: int) -> str:
    return f"content-transform/retry/{retry_seed}/{position}"


def _cached_child_model_result(
    store: Store,
    cache_ref: str,
) -> _ChildModelResult | None:
    ref = store.get_ref(cache_ref)
    if ref is None:
        return None
    obj = store.get_object(ref.object_id)
    if obj is None or obj.kind != "assistant_message":
        raise ContentValidationError("recorded model transformation is unavailable")
    message = obj.data.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, str):
        raise ContentValidationError("recorded model transformation has no text")
    return _ChildModelResult(content, ref.object_id)


def _cache_child_model_result(
    store: Store,
    cache_ref: str,
    result: _ChildModelResult,
) -> _ChildModelResult:
    update = store.move_ref(cache_ref, None, result.assistant_id)
    if update.updated:
        return result
    accepted = _cached_child_model_result(store, cache_ref)
    if accepted is None:
        raise ContentValidationError("model transformation retry result was lost")
    return accepted


async def _request_child_model_transform(
    inputs: tuple[ContentTransformInput, ...],
    *,
    instruction: str,
    mode: str,
    cache_ref: str,
    config: AgentConfig,
    ctx: RunDependencies,
) -> _ChildModelResult:
    workspace = ctx.content_workspace
    if workspace is None:
        raise ContentValidationError("content workspace is unavailable")
    cached = _cached_child_model_result(workspace.store, cache_ref)
    if cached is not None:
        return cached
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
    ctx.content_transform_budget.record_model_output(
        len(content),
        input_chars=sum(len(_content_transform_input_text(item)) for item in inputs),
        total_tokens=_model_total_tokens(telemetry),
    )
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
            params={"mode": mode, "retry_ref": cache_ref},
        )
    )
    return _cache_child_model_result(
        workspace.store,
        cache_ref,
        _ChildModelResult(content, assistant_id),
    )


def _model_total_tokens(telemetry: Mapping[str, Any]) -> int | None:
    usage = telemetry.get("usage")
    if not isinstance(usage, Mapping):
        return None
    total = usage.get("total_tokens")
    if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
        return total
    prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
    completion = usage.get("completion_tokens", usage.get("output_tokens"))
    if (
        isinstance(prompt, int)
        and not isinstance(prompt, bool)
        and prompt >= 0
        and isinstance(completion, int)
        and not isinstance(completion, bool)
        and completion >= 0
    ):
        return prompt + completion
    return None


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
