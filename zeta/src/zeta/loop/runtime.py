"""Headless native-tool-call run execution for Zeta."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from zeta.capabilities.executors import ToolExecutor
from zeta.capabilities.profiles import identity_arguments
from zeta.capabilities.registry import (
    CapabilityRegistry,
    CapabilityToolRoute,
    CapabilityToolSchema,
    model_descriptor,
)
from zeta.capabilities.registry import registry as _runtime_tool_registry
from zeta.context import prompt_transform_from_policy
from zeta.context.builder import (
    PromptBuilder,
)
from zeta.context.transforms import ContentWorkspace
from zeta.events import DraftEvent, Event
from zeta.journal.drafts import (
    user_message_draft,
)
from zeta.journal.store import EventReader, Filter
from zeta.journal.views import (
    event_timeline_type,
)
from zeta.loop.cancellation import (
    CancellationToken,
    agent_deadline,
    run_abort_reason,
)
from zeta.loop.config import AgentConfig
from zeta.loop.gateway import ModelGateway, model_request_from, run_model_metadata
from zeta.loop.outcomes import (
    AgentRunResult,
    RunState,
)
from zeta.loop.projection import is_runtime_ui_event
from zeta.loop.request import AgentRunRequest, RunDependencies
from zeta.loop.runtime_context import RuntimeContext
from zeta.loop.stages.prompt import agent_allowed_capabilities, registered_capabilities
from zeta.loop.steps import AgentRun
from zeta.loop.types import (
    MODEL_TIMELINE_TYPES,
    AgentEventSink,
    TimelineEvent,
    time_monotonic,
)
from zeta.models import DefaultModelGateway
from zeta.substrate import Store
from zeta.trace import warn_trace_failure_once
from zeta.trace.provenance import (
    project_prompt_trace_projection,
)
from zeta.trace.query import QueryLogReader, bind_query_log_reader


async def run_agent(
    request: AgentRunRequest,
    *,
    run_id: str,
    caused_by: str,
    publish_event: Callable[[Event], None],
    runtime_context: RuntimeContext,
    cancellation_event: CancellationToken | None,
    model_gateway: ModelGateway | None = None,
    tool_executor: ToolExecutor,
) -> AgentRunResult:
    """Run one durable agent turn inside a runtime session."""
    enabled_capabilities = registered_capabilities(
        request.tools or request.config.allowed_capabilities,
        tool_registry=runtime_context.tool_registry,
    )
    prior_timeline = (
        [] if request.fresh else current_timeline(runtime_context=runtime_context)
    )
    user_message: dict[str, Any] = {
        "type": "user_message",
        "content": request.objective,
        "runtime": request.runtime,
        "available_tools": list(enabled_capabilities),
        "run_id": run_id,
    }
    model = run_model_metadata(request.config)
    if model:
        user_message["model"] = model
    user_event = _record_user_message(
        user_message,
        runtime_context=runtime_context,
        run_id=run_id,
    )
    publish_event(user_event)

    def sink(draft: DraftEvent) -> None:
        if is_runtime_ui_event(draft):
            return
        persisted = _record_runtime_event(
            draft,
            runtime_context=runtime_context,
            run_id=run_id,
        )
        publish_event(persisted)

    content_workspace = ContentWorkspace(
        runtime_context.content_store or runtime_context.trace_store,
        run_id=run_id,
        session_id=runtime_context.session_id,
        owner=request.source_agent_id or f"session:{runtime_context.session_id}",
        include_agent_content=request.source_agent_id is not None,
    )
    content_workspace.initialize()

    return await run_agent_loop(
        request.objective,
        prior_timeline,
        replace(
            request.config,
            allowed_capabilities=enabled_capabilities,
            model_session_id=runtime_context.session_id,
        ),
        context=request.context,
        event_sink=sink,
        trace_store=runtime_context.trace_store,
        tool_registry=runtime_context.tool_registry,
        tool_executor=tool_executor,
        model_gateway=model_gateway,
        caused_by=caused_by,
        cancellation_event=cancellation_event,
        query_log_reader=bind_query_log_reader(
            runtime_context.event_sink,
            session_id=runtime_context.session_id,
            current_run_id=run_id,
        ),
        publishable_events=request.publishable_events,
        source_queue_item_id=request.source_queue_item_id,
        source_agent_id=request.source_agent_id,
        source_session_id=runtime_context.session_id,
        content_workspace=content_workspace,
    )


async def run_agent_loop(
    objective: str,
    timeline: Sequence[TimelineEvent],
    config: AgentConfig,
    *,
    context: str = "",
    event_sink: AgentEventSink | None = None,
    prompt_builder: PromptBuilder | None = None,
    trace_store: Store | None = None,
    tool_registry: CapabilityRegistry | None = None,
    tool_executor: ToolExecutor,
    model_gateway: ModelGateway | None = None,
    caused_by: str | None = None,
    cancellation_event: CancellationToken | None = None,
    deadline: float | None = None,
    query_log_reader: QueryLogReader | None = None,
    publishable_events: Mapping[str, dict[str, Any] | None] | None = None,
    source_queue_item_id: str | None = None,
    source_agent_id: str | None = None,
    source_session_id: str | None = None,
    content_workspace: ContentWorkspace | None = None,
) -> AgentRunResult:
    """Run an assistant/tool loop without mutating session state."""
    gateway = model_gateway or DefaultModelGateway()
    if not gateway.available(model_request_from(config)):
        raise RuntimeError("model endpoint is not reachable")
    clock = time_monotonic
    deadline = agent_deadline(config.max_wall_seconds, deadline, clock=clock)
    active_tool_registry = tool_registry or _runtime_tool_registry
    allowed_capabilities = agent_allowed_capabilities(
        config,
        tool_registry=active_tool_registry,
    )
    state = RunState(next_model_caused_by=caused_by)
    builder = prompt_builder or PromptBuilder(
        store=trace_store,
        transform=prompt_transform_from_policy(config.compaction_policy),
    )
    deps = RunDependencies(
        event_sink=event_sink,
        trace_store=trace_store,
        tool_registry=active_tool_registry,
        tool_executor=tool_executor,
        builder=builder,
        model_gateway=gateway,
        abort_reason=run_abort_reason(cancellation_event, deadline, clock=clock),
        query_log_reader=query_log_reader,
        publishable_events=publishable_events or {},
        source_queue_item_id=source_queue_item_id,
        source_agent_id=source_agent_id,
        source_session_id=source_session_id,
        content_workspace=content_workspace,
    )
    tool_schema = active_tool_registry.model_tool_schema(
        allowed_capabilities,
        tool_profile=config.tool_profile,
    )
    if publishable_events and source_queue_item_id is not None:
        tool_schema = publish_event_tool_schema(tool_schema)
        allowed_capabilities = (*allowed_capabilities, "zeta.publish_event")
    if source_queue_item_id is not None:
        tool_schema = wait_for_tool_schema(tool_schema)
        allowed_capabilities = (*allowed_capabilities, "zeta.wait_for")
    if (
        source_queue_item_id is not None
        and source_agent_id is not None
        and source_session_id is not None
    ):
        tool_schema = cancel_tool_schema(tool_schema)
        allowed_capabilities = (*allowed_capabilities, "zeta.cancel")
    tools = tool_schema.descriptors
    return await AgentRun(
        objective=objective,
        timeline=timeline,
        config=config,
        context=context,
        deps=deps,
        allowed_capabilities=allowed_capabilities,
        tool_schema=tool_schema,
        tools=tools,
        state=state,
    ).run()


def publish_event_tool_schema(
    tool_schema: CapabilityToolSchema,
) -> CapabilityToolSchema:
    existing = tool_schema.routes.get("publish_event")
    if existing is not None:
        raise ValueError(
            "reserved tool name 'publish_event' is already in use by "
            f"{existing.capability_id!r}"
        )
    input_schema = {
        "type": "object",
        "required": ["event_type", "payload"],
        "properties": {
            "event_type": {"type": "string"},
            "payload": {"type": "object"},
            "at": {"type": "string"},
        },
        "additionalProperties": False,
    }
    return CapabilityToolSchema(
        routes={
            **tool_schema.routes,
            "publish_event": CapabilityToolRoute(
                capability_id="zeta.publish_event",
                input_schema=input_schema,
                adapt_arguments=identity_arguments,
            ),
        },
        descriptors=[
            *tool_schema.descriptors,
            model_descriptor(
                "publish_event",
                "Request an event when this agent attempt completes successfully.",
                input_schema,
            ),
        ],
    )


def wait_for_tool_schema(
    tool_schema: CapabilityToolSchema,
) -> CapabilityToolSchema:
    existing = tool_schema.routes.get("wait_for")
    if existing is not None:
        raise ValueError(
            "reserved tool name 'wait_for' is already in use by "
            f"{existing.capability_id!r}"
        )
    input_schema = {
        "type": "object",
        "required": ["event_type"],
        "properties": {
            "event_type": {"type": "string", "minLength": 1},
            "fields": {"type": "object"},
            "deadline": {"type": "string"},
        },
        "additionalProperties": False,
    }
    return CapabilityToolSchema(
        routes={
            **tool_schema.routes,
            "wait_for": CapabilityToolRoute(
                capability_id="zeta.wait_for",
                input_schema=input_schema,
                adapt_arguments=identity_arguments,
            ),
        },
        descriptors=[
            *tool_schema.descriptors,
            model_descriptor(
                "wait_for",
                "End this run and resume when a matching event arrives.",
                input_schema,
            ),
        ],
    )


def cancel_tool_schema(
    tool_schema: CapabilityToolSchema,
) -> CapabilityToolSchema:
    existing = tool_schema.routes.get("cancel")
    if existing is not None:
        raise ValueError(
            "reserved tool name 'cancel' is already in use by "
            f"{existing.capability_id!r}"
        )
    input_schema = {
        "type": "object",
        "required": ["handle"],
        "properties": {
            "handle": {
                "type": "string",
                "pattern": "^(?:wait|pub)_.+$",
            },
            "reason": {"type": "string", "minLength": 1},
        },
        "additionalProperties": False,
    }
    return CapabilityToolSchema(
        routes={
            **tool_schema.routes,
            "cancel": CapabilityToolRoute(
                capability_id="zeta.cancel",
                input_schema=input_schema,
                adapt_arguments=identity_arguments,
            ),
        },
        descriptors=[
            *tool_schema.descriptors,
            model_descriptor(
                "cancel",
                "Cancel an active wait or pending scheduled event from this session.",
                input_schema,
            ),
        ],
    )


def current_timeline(*, runtime_context: RuntimeContext) -> list[Event]:
    try:
        if not isinstance(runtime_context.event_sink, EventReader):
            return []
        events = runtime_context.event_sink.list_events(
            Filter(
                session_id=runtime_context.session_id,
                event_type_prefix="zeta.",
            )
        )
        return [
            event
            for event in events
            if event_timeline_type(event) in MODEL_TIMELINE_TYPES
        ]
    except Exception as exc:
        warn_trace_failure_once("current_timeline", exc)
        return []


def _record_user_message(
    event: dict[str, Any],
    *,
    runtime_context: RuntimeContext,
    run_id: str | None = None,
) -> Event:
    payload = {key: value for key, value in event.items() if key != "type"}
    outcome = runtime_context.event_sink.accept(
        user_message_draft(
            payload,
            session_id=runtime_context.session_id,
            run_id=run_id,
            turn_id=event.get("turn_id")
            if isinstance(event.get("turn_id"), str)
            else None,
        )
    )
    return outcome.event


def _record_runtime_event(
    draft: DraftEvent,
    *,
    runtime_context: RuntimeContext,
    run_id: str,
) -> Event:
    tagged = replace(
        draft,
        payload={**draft.payload, "run_id": run_id},
        session_id=runtime_context.session_id,
        run_id=run_id,
    )
    outcome = runtime_context.event_sink.accept(tagged)
    _record_trace_for_run(runtime_context, outcome.event.run_id)
    return outcome.event


def _record_trace_for_run(runtime_context: RuntimeContext, run_id: str | None) -> None:
    if run_id is None or not isinstance(runtime_context.event_sink, EventReader):
        return
    try:
        project_prompt_trace_projection(
            runtime_context.event_sink.list_events(
                Filter(
                    session_id=runtime_context.session_id,
                    run_id=run_id,
                    event_type_prefix="zeta.",
                )
            ),
            runtime_context.trace_store,
        )
    except Exception as exc:
        warn_trace_failure_once("record_trace_for_run", exc)


def session_trace_result(
    runtime_context: RuntimeContext,
    run_id: str,
) -> dict[str, list[str]]:
    if not isinstance(runtime_context.event_sink, EventReader):
        return empty_session_trace_result()
    trace = empty_session_trace_result()
    events = runtime_context.event_sink.list_events(
        Filter(
            session_id=runtime_context.session_id,
            run_id=run_id,
            event_type_prefix="zeta.",
        )
    )
    projection = project_prompt_trace_projection(events, runtime_context.trace_store)
    for event in events:
        event_type = event_timeline_type(event)
        if event_type == "model":
            _add_unique(trace["model_event_ids"], event.id)
            _add_unique(trace["prompt_ids"], projection.prompt_object_ids.get(event.id))
            _add_unique(
                trace["assistant_message_ids"],
                projection.assistant_message_ids.get(event.id),
            )
            continue
        if event_type == "tool_call":
            _add_unique(trace["tool_event_ids"], event.id)
            _add_unique(
                trace["tool_call_ids"], projection.tool_call_object_ids.get(event.id)
            )
            continue
        if event_type == "tool_result":
            _add_unique(trace["tool_event_ids"], event.id)
            _add_unique(
                trace["tool_result_ids"],
                projection.tool_result_object_ids.get(event.id),
            )
    return trace


def empty_session_trace_result() -> dict[str, list[str]]:
    return {
        "prompt_ids": [],
        "assistant_message_ids": [],
        "model_event_ids": [],
        "tool_event_ids": [],
        "tool_call_ids": [],
        "tool_result_ids": [],
    }


def _add_unique(values: list[str], value: Any) -> None:
    if isinstance(value, str) and value and value not in values:
        values.append(value)


def final_event_cursor(runtime_context: RuntimeContext, run_id: str) -> str | None:
    if not isinstance(runtime_context.event_sink, EventReader):
        return None
    events = runtime_context.event_sink.list_events(
        Filter(session_id=runtime_context.session_id, run_id=run_id)
    )
    if not events:
        return None
    return str(events[-1].cursor) if events[-1].cursor is not None else None
