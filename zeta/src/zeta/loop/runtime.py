"""Headless native-tool-call run execution for Zeta."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any

from zeta.capabilities.executors import ToolExecutor
from zeta.capabilities.registry import (
    CapabilityRegistry,
)
from zeta.capabilities.registry import registry as _runtime_tool_registry
from zeta.context import prompt_transform_from_policy
from zeta.context.builder import (
    PromptBuilder,
)
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
from zeta.loop.gateway import ModelGateway, run_model_metadata
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
) -> AgentRunResult:
    """Run an assistant/tool loop without mutating session state."""
    gateway = model_gateway or DefaultModelGateway()
    if not gateway.available(config):
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
    )
    tool_schema = active_tool_registry.model_tool_schema(allowed_capabilities)
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
