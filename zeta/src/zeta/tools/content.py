"""Runtime tools for revisioned content and content transformations."""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from zeta.addresses import chain_address
from zeta.capabilities.types import Capability, CapabilityId
from zeta.context.builder import PromptBuilder, render_model_input
from zeta.context.components import PromptComponent
from zeta.context.transforms import (
    ContentConflict,
    ContentNode,
    ContentPromotion,
    ContentTransformInput,
    ContentTransformResult,
    ContentValidationError,
    ContentWorkspace,
    put_content_node,
)
from zeta.models.types import ModelInput, ModelOutput
from zeta.substrate import Derivation, Object, ObjectId, Store

QUERY_CONTENT_CAPABILITY_ID = "zeta.query_content"
TRANSFORM_CONTENT_CAPABILITY_ID = "zeta.transform_content"
FINISH_CAPABILITY_ID = "zeta.finish"

QUERY_CONTENT_SPEC = Capability(
    CapabilityId("zeta", "query_content"),
    (
        "Query the current content workspace. The result contains stable object "
        "references and bounded previews."
    ),
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "key_prefix": {"type": "string"},
            "kind": {"type": "string"},
            "source_scope": {"enum": ["run", "session", "agent"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            "cursor": {"type": "integer", "minimum": 0},
        },
    },
)

TRANSFORM_CONTENT_SPEC = Capability(
    CapabilityId("zeta", "transform_content"),
    (
        "Create a new content revision. Use an explicit run, session, or agent "
        "destination. Durable changes become active only after the run succeeds."
    ),
    {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "expected_head",
            "reason",
            "inputs",
            "transformation",
            "destination",
        ],
        "properties": {
            "expected_head": {"type": "string", "minLength": 1},
            "reason": {"type": "string", "minLength": 1},
            "inputs": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "keys": {
                        "type": "array",
                        "items": {"type": "string"},
                        "uniqueItems": True,
                    },
                    "kind": {"type": "string"},
                    "source_scope": {"enum": ["run", "session", "agent"]},
                },
            },
            "transformation": {
                "type": "object",
                "required": ["type"],
                "properties": {
                    "type": {
                        "enum": [
                            "literal",
                            "patch",
                            "drop",
                            "identity",
                            "model",
                            "python",
                        ]
                    },
                    "value": {},
                    "title": {"type": "string"},
                    "attributes": {"type": "object"},
                    "patch": {"type": "object"},
                    "instruction": {"type": "string", "minLength": 1},
                    "mode": {"enum": ["one", "map", "reduce"]},
                    "max_concurrency": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 8,
                    },
                    "source": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 131072,
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "maximum": 300,
                    },
                },
            },
            "destination": {
                "type": "object",
                "additionalProperties": False,
                "required": ["scope", "expected_object_id"],
                "properties": {
                    "key": {"type": "string", "minLength": 1},
                    "kind": {"type": "string", "minLength": 1},
                    "scope": {"enum": ["run", "session", "agent"]},
                    "expected_object_id": {"type": ["string", "null"]},
                },
            },
        },
    },
)

FINISH_SPEC = Capability(
    CapabilityId("zeta", "finish"),
    (
        "Select one object from the current content graph as the final answer. "
        "Use this when copying the complete value into a model message is wasteful."
    ),
    {
        "type": "object",
        "additionalProperties": False,
        "required": ["object_id"],
        "properties": {"object_id": {"type": "string", "minLength": 1}},
    },
)


class ContentTransformBudget(Protocol):
    def reserve_model_calls(self, *, calls: int, input_chars: int) -> int: ...

    def record_model_output(
        self,
        output_chars: int,
        *,
        input_chars: int = 0,
        total_tokens: int | None = None,
    ) -> None: ...


ContentModelRequest = Callable[
    [ModelInput],
    Awaitable[tuple[ModelOutput, dict[str, Any]]],
]
ContentToolFunction = Callable[
    [dict[str, Any]],
    dict[str, Any] | None | Awaitable[dict[str, Any] | None],
]


@dataclass(frozen=True)
class ContentModelIdentity:
    profile: str | None
    name: str | None
    url: str | None
    thinking: str | None
    api: str | None


@dataclass(frozen=True)
class ContentToolRuntime:
    """Expose only the run powers that content tools require."""

    workspace: ContentWorkspace | None
    position: int
    transform_budget: ContentTransformBudget
    source_queue_item_id: str | None
    abort_reason: Callable[[], str | None]
    model: ContentModelIdentity
    request_model: ContentModelRequest
    record_promotions: Callable[[tuple[ContentPromotion, ...]], None]
    select_final: Callable[[ObjectId, str], None]


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


def bind_content_tools(runtime: ContentToolRuntime) -> dict[str, ContentToolFunction]:
    """Bind one run without giving content tools access to the agent loop."""
    return {
        QUERY_CONTENT_CAPABILITY_ID: lambda params: request_content_query(
            params,
            runtime=runtime,
        ),
        TRANSFORM_CONTENT_CAPABILITY_ID: lambda params: request_content_transform(
            params,
            runtime=runtime,
        ),
        FINISH_CAPABILITY_ID: lambda params: request_content_finish(
            params,
            runtime=runtime,
        ),
    }


def request_content_query(
    params: dict[str, Any],
    *,
    runtime: ContentToolRuntime,
) -> dict[str, Any] | None:
    workspace = runtime.workspace
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
        return content_tool_error("invalid-content-query", str(exc))
    return {"ok": True, **result}


def request_content_finish(
    params: dict[str, Any],
    *,
    runtime: ContentToolRuntime,
) -> dict[str, Any] | None:
    workspace = runtime.workspace
    if workspace is None:
        return None
    try:
        result = workspace.finish(params["object_id"])
    except ContentValidationError as exc:
        return content_tool_error("invalid-finish-object", str(exc))
    runtime.select_final(result.object_id, result.content)
    return {
        "ok": True,
        "stop": True,
        "object_id": result.object_id,
    }


async def request_content_transform(
    params: dict[str, Any],
    *,
    runtime: ContentToolRuntime,
) -> dict[str, Any] | None:
    workspace = runtime.workspace
    if workspace is None:
        return None
    try:
        operation = params.get("transformation")
        if isinstance(operation, Mapping) and operation.get("type") == "model":
            result = await model_content_transform(params, runtime=runtime)
        elif isinstance(operation, Mapping) and operation.get("type") == "python":
            result = await python_content_transform(params, runtime=runtime)
        else:
            result = workspace.transform(params)
    except ContentConflict as exc:
        return content_tool_error("content-conflict", str(exc))
    except ContentValidationError as exc:
        return content_tool_error("invalid-content-transform", str(exc))
    runtime.record_promotions(result.promotions)
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
    runtime: ContentToolRuntime,
) -> ContentTransformResult:
    workspace = runtime.workspace
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
            position=runtime.position,
            runtime=runtime,
        ),
        runtime=runtime,
    )
    if derived.mode == "map" and destination.get("kind") != "collection":
        raise ContentValidationError("model map destination kind must be collection")
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
    runtime: ContentToolRuntime,
) -> _DerivedModelResult:
    workspace = runtime.workspace
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
        runtime=runtime,
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
                runtime=runtime,
            )

    children = await asyncio.gather(
        *(transform_one(index, group) for index, group in enumerate(groups))
    )
    assistant_ids = tuple(child.assistant_id for child in children)
    value = (
        {"object_ids": list(assistant_ids)} if mode == "map" else children[0].content
    )
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
    runtime: ContentToolRuntime,
) -> tuple[int, int]:
    missing = tuple(index for index, child in enumerate(cached) if child is None)
    input_chars = sum(
        len(_content_transform_input_text(item))
        for index in missing
        for item in groups[index]
    )
    concurrency = (
        runtime.transform_budget.reserve_model_calls(
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
    runtime: ContentToolRuntime,
) -> ContentTransformResult:
    workspace = runtime.workspace
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
        position=runtime.position,
        runtime=runtime,
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
            runtime=runtime,
        )

    output = await asyncio.wait_for(
        asyncio.to_thread(
            _run_python_program,
            source,
            _PythonContentContext(values),
            nested_transform,
            parent_loop,
            float(timeout),
            runtime.abort_reason,
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
    should_stop: Callable[[], str | None],
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
    runtime: ContentToolRuntime,
) -> _PythonContentValue:
    workspace = runtime.workspace
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
            runtime=runtime,
            parent_retry_seed=parent_retry_seed,
        ),
        runtime=runtime,
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
    runtime: ContentToolRuntime,
    parent_retry_seed: str | None = None,
) -> str:
    workspace = runtime.workspace
    if workspace is None:
        raise ContentValidationError("content workspace is unavailable")
    parent = (
        parent_retry_seed
        or runtime.source_queue_item_id
        or f"run:{workspace.run_head.scope_id}"
    )
    payload = {
        "parent": parent,
        "position": position,
        "input_ids": [item.object_id for item in inputs],
        "transformation": dict(operation),
        "destination": dict(destination),
        "model": {
            "profile": runtime.model.profile,
            "name": runtime.model.name,
            "url": runtime.model.url,
            "thinking": runtime.model.thinking,
            "api": runtime.model.api,
        },
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return chain_address(encoded)


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
    runtime: ContentToolRuntime,
) -> _ChildModelResult:
    workspace = runtime.workspace
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
        selected_model=runtime.model.name,
        thinking=runtime.model.thinking,
    )
    stored = await builder.commit_prompt_plan(plan)
    model_output, telemetry = await runtime.request_model(render_model_input(stored))
    content = model_output.message.get("content")
    if not isinstance(content, str):
        raise ContentValidationError("model transformation returned no text")
    runtime.transform_budget.record_model_output(
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


def content_tool_error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def content_workspace_unavailable(_params: dict[str, Any]) -> dict[str, Any]:
    """Refuse content changes when no run owns a content workspace."""
    return {
        "ok": False,
        "error": {
            "code": "content-workspace-unavailable",
            "message": "content tools are unavailable outside a Zeta run",
        },
    }
