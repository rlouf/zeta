"""Shared I/O plumbing for Zeta-backed agent workflows.

Both workflows persist agent events to the Zeta run timeline, render tool traces
and a context-usage footer while the loop runs, and replay any events the
recorder missed. This module owns that skeleton; workflow modules own
workflow-specific tagging, logging, and handoff handling.
"""

import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TextIO

from sigil.display.render import render_tool_start
from sigil.display.state import (
    PROGRESS_MODE_TRACE,
    ContextUsageFooter,
    TerminalDigestRenderer,
    TraceAwareStreamRenderer,
    TraceRenderState,
    create_stream_renderer,
    progress_mode_from_env,
)
from sigil.protocols import (
    TURN_OUTCOME_ABORTED,
    TURN_OUTCOME_ANSWERED,
    TURN_OUTCOME_EXECUTED,
    TURN_OUTCOME_FAILED,
    TURN_OUTCOME_STAGED,
)
from sigil.sessions import session_id
from sigil.state import append_prompt_submitted_event
from sigil.tools import ensure_builtin_tools_registered
from sigil.turn import TurnRecorder
from zeta.agents.capabilities import AgentConfig
from zeta.capabilities.base import ExecutionMode
from zeta.context.instructions import load_project_instructions
from zeta.events import AppendOutcome, DraftEvent, Event
from zeta.loop import (
    AgentTurnAborted,
    AgentTurnResult,
    model_called_draft,
    model_durable_payload,
    registered_capabilities,
    run_agent_turn,
    tool_called_draft,
    tool_durable_payload,
)
from zeta.models import (
    CODEX_RESPONSES_API,
    ModelSelection,
    active_model_selection,
    model_selection_event,
)
from zeta.models.chat_completions import ensure_server
from zeta.session import Session
from zeta.timeline import (
    current_timeline,
    timeline_event_from_durable_event,
)


def draft_event_id(draft: DraftEvent) -> str | None:
    key = draft.idempotency_key
    prefix = f"{draft.event_type}:"
    if key is None or not key.startswith(prefix):
        return None
    event_id = key[len(prefix) :].strip()
    return event_id or None


class DirectRuntimeEventSink:
    def __init__(
        self,
        runtime_context: Session,
        tag_fields: Callable[[], dict[str, Any]],
    ) -> None:
        self.runtime_context = runtime_context
        self._tag_fields = tag_fields
        self.projected_events: dict[str, dict[str, Any]] = {}

    def accept(self, draft: DraftEvent) -> AppendOutcome:
        tagged = DraftEvent(
            event_type=draft.event_type,
            source=draft.source,
            payload={**draft.payload, **self.tag_fields()},
            idempotency_key=draft.idempotency_key,
            caused_by=draft.caused_by,
            session_id=draft.session_id,
            turn_id=draft.turn_id,
        )
        event_id = draft_event_id(tagged)
        append = getattr(self.runtime_context.event_sink, "append", None)
        if callable(append) and event_id is not None:
            event = Event(
                id=event_id,
                event_type=tagged.event_type,
                source=tagged.source,
                payload=tagged.payload,
                idempotency_key=tagged.idempotency_key,
                caused_by=tagged.caused_by,
                session_id=tagged.session_id,
                turn_id=tagged.turn_id,
                timestamp_micros=time_micros(),
            )
            outcome = append(event)
        else:
            outcome = self.runtime_context.event_sink.accept(tagged)
        projected = timeline_event_from_durable_event(outcome.event)
        runtime_id = event_id or projected.get("id")
        if isinstance(runtime_id, str) and projected:
            self.projected_events[runtime_id] = projected
        return outcome

    def projected_event(self, event: dict[str, Any]) -> dict[str, Any] | None:
        event_id = event.get("id")
        if not isinstance(event_id, str):
            return None
        projected = self.projected_events.get(event_id)
        return dict(projected) if projected is not None else None

    def tag_fields(self) -> dict[str, Any]:
        return self._tag_fields()


def projected_direct_event(
    sink: DirectRuntimeEventSink,
    event: dict[str, Any],
) -> dict[str, Any] | None:
    direct_event = sink.projected_event(event)
    if direct_event is None:
        return None
    if "effects" in event:
        direct_event["effects"] = event["effects"]
    return direct_event


def record_user_message(
    event: dict[str, Any],
    *,
    runtime_context: Session,
) -> dict[str, Any]:
    payload = {key: value for key, value in event.items() if key != "type"}
    payload["_timeline_type"] = "user_message"
    outcome = runtime_context.event_sink.accept(
        DraftEvent(
            event_type="zeta.user_message",
            source="zeta",
            payload=payload,
            idempotency_key=None,
            caused_by=None,
            session_id=runtime_context.session_id,
            turn_id=event.get("turn_id")
            if isinstance(event.get("turn_id"), str)
            else None,
        )
    )
    return timeline_event_from_durable_event(outcome.event)


def record_runtime_event(
    event: dict[str, Any],
    *,
    runtime_context: Session,
    tag_fields: dict[str, Any] | None = None,
    strip_fields: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    tagged = {key: value for key, value in event.items() if key not in strip_fields}
    if tag_fields is not None:
        tagged.update(tag_fields)
    event_type = str(tagged.get("type") or "")
    caused_by = (
        tagged.get("caused_by") if isinstance(tagged.get("caused_by"), str) else None
    )
    event_id = tagged.get("id") if isinstance(tagged.get("id"), str) else None
    turn_id = tagged.get("turn_id") if isinstance(tagged.get("turn_id"), str) else None
    if event_type == "model":
        draft = model_called_draft(
            payload=model_durable_payload(tagged),
            turn_id=turn_id,
            session_id=runtime_context.session_id,
            caused_by=caused_by,
            event_id=event_id,
        )
    elif event_type in {"tool_call", "tool_result"}:
        draft = tool_called_draft(
            payload=tool_durable_payload(tagged),
            turn_id=turn_id,
            session_id=runtime_context.session_id,
            caused_by=caused_by,
            event_id=event_id,
        )
    elif event_type == "turn_aborted":
        payload = {
            key: value
            for key, value in tagged.items()
            if key not in {"id", "type", "time", "session", "source", "caused_by"}
        }
        payload["_timeline_type"] = "turn_aborted"
        payload.setdefault("reason", "aborted")
        draft = DraftEvent(
            event_type="zeta.turn.failed",
            source="zeta",
            payload=payload,
            idempotency_key=None,
            caused_by=caused_by,
            session_id=runtime_context.session_id,
            turn_id=turn_id,
        )
    else:
        raise ValueError(f"unsupported runtime event type: {event_type}")
    outcome = DirectRuntimeEventSink(runtime_context, lambda: {}).accept(draft)
    projected = timeline_event_from_durable_event(outcome.event)
    if "effects" in tagged:
        projected["effects"] = tagged["effects"]
    return projected


def time_micros() -> int:
    import time

    return time.time_ns() // 1_000


def model_server_ready(selected_model: ModelSelection | None) -> bool:
    """Check endpoint reachability for the active model selection."""
    if selected_model is not None and selected_model.api == CODEX_RESPONSES_API:
        return True
    if selected_model is not None:
        return ensure_server(
            selected_url=selected_model.url,
            selected_model=selected_model.model,
        )
    return ensure_server()


@dataclass(frozen=True)
class TurnRenderer:
    """Rendering state shared across one workflow turn."""

    trace_state: TraceRenderState
    context_footer: ContextUsageFooter | None
    stream_renderer: TraceAwareStreamRenderer | None
    progress_renderer: TerminalDigestRenderer | None


def build_turn_renderer(
    footer_output: TextIO,
    *,
    objective: str = "",
) -> TurnRenderer:
    """Build the trace state, context footer, and stream renderer for a turn."""
    trace_state = TraceRenderState()
    context_footer = ContextUsageFooter(footer_output)
    progress_mode = progress_mode_from_env()
    progress_renderer = TerminalDigestRenderer(
        footer_output,
        mode=progress_mode,
        objective=objective,
    )
    base_stream_renderer = create_stream_renderer(sys.stdout)
    stream_renderer = TraceAwareStreamRenderer(
        base_stream_renderer,
        trace_state,
        sys.stdout,
        before_output=context_footer.clear,
    )
    return TurnRenderer(trace_state, context_footer, stream_renderer, progress_renderer)


class TurnEventRecorder:
    """Persist and render agent events as the loop produces them.

    Subclasses set ``tag_fields``/``strip_fields`` for timeline tagging and
    override ``handle_tool_call``/``handle_tool_result`` for workflow behavior.
    ``handle_tool_result`` may return an exit status; the last one wins.
    A turn recorder, when given, tags every persisted event with the turn id and
    receives tool results for effect recording.
    """

    tag_fields: dict[str, Any] = {}
    strip_fields: frozenset[str] = frozenset()

    def __init__(
        self,
        renderer: TurnRenderer,
        *,
        render_output: TextIO,
        turn_recorder: TurnRecorder | None = None,
        runtime_context: Session,
    ) -> None:
        self.renderer = renderer
        self.render_output = render_output
        self.turn_recorder = turn_recorder
        self.runtime_context = runtime_context
        self.recorded_event_ids: set[int] = set()
        self.direct_event_sink = DirectRuntimeEventSink(
            runtime_context,
            lambda: self.tag_fields,
        )
        self.status: int | None = None

    def record(self, event: dict[str, Any]) -> None:
        self.recorded_event_ids.add(id(event))
        self.record_event(event)

    def replay(self, result: AgentTurnResult) -> None:
        """Record any turn events the live sink did not see."""
        for event in result.events:
            if id(event) in self.recorded_event_ids:
                continue
            self.record_event(event)

    def record_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        if event_type == "tool_result" and self.turn_recorder is not None:
            self.turn_recorder.attach_tool_result_effect(event)
        persisted = self.persist(event_type, event)
        if self.turn_recorder is not None:
            self.turn_recorder.note_runtime_event(persisted)
        if event_type == "tool_call":
            params = persisted.get("input")
            self.handle_tool_call(
                str(persisted.get("name") or ""),
                params if isinstance(params, dict) else {},
            )
            return
        if event_type != "tool_result":
            return
        status = self.handle_tool_result(persisted)
        if status is not None:
            self.status = status

    def persist(self, event_type: str, event: dict[str, Any]) -> dict[str, Any]:
        direct_event = projected_direct_event(self.direct_event_sink, event)
        if direct_event is not None:
            return direct_event
        tagged = dict(event)
        if self.turn_recorder is not None:
            tagged["turn_id"] = self.turn_recorder.turn_id
        return record_runtime_event(
            tagged,
            runtime_context=self.runtime_context,
            tag_fields=self.tag_fields,
            strip_fields=self.strip_fields,
        )

    def handle_tool_call(self, name: str, args: dict[str, Any]) -> None:
        self.render_tool_call(name, args)

    def render_tool_call(self, name: str, args: dict[str, Any]) -> None:
        if (
            self.renderer.progress_renderer is not None
            and self.renderer.progress_renderer.mode != PROGRESS_MODE_TRACE
        ):
            self.renderer.progress_renderer.observe_tool_call(name, args)
            return
        if self.renderer.context_footer is not None:
            self.renderer.context_footer.clear()
        if self.renderer.stream_renderer is not None:
            self.renderer.stream_renderer.ensure_trace_boundary()
        render_tool_start(name, args, output=self.render_output, newline=False)

    def handle_tool_result(self, persisted: dict[str, Any]) -> int | None:
        raise NotImplementedError


def render_final_text(
    content: str,
    *,
    streamed: bool,
    renderer: TurnRenderer,
) -> None:
    """Print a buffered final answer, or close out an already-streamed one."""
    if not content:
        return
    if streamed:
        if renderer.stream_renderer is not None:
            renderer.stream_renderer.finish()
        return
    if not renderer.trace_state.render_text_separator(sys.stdout):
        print()
    print(content)
    print()


def record_turn_abort(
    error: BaseException,
    *,
    runtime_context: Session,
    reason: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Resolve an aborted turn in the timeline instead of dangling its question."""
    message = str(error) or type(error).__name__
    if reason is not None:
        fields["reason"] = reason
    payload = {
        "error": message,
        "content": f"(turn aborted: {message})",
        **fields,
        "_timeline_type": "turn_aborted",
    }
    payload.setdefault("reason", "aborted")
    outcome = runtime_context.event_sink.accept(
        DraftEvent(
            event_type="zeta.turn.failed",
            source="zeta",
            payload=payload,
            idempotency_key=None,
            caused_by=fields.get("caused_by")
            if isinstance(fields.get("caused_by"), str)
            else None,
            session_id=runtime_context.session_id,
            turn_id=fields.get("turn_id")
            if isinstance(fields.get("turn_id"), str)
            else None,
        )
    )
    return timeline_event_from_durable_event(outcome.event)


def run_zeta_rpc_session(
    params: dict[str, Any],
    *,
    publish_event: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    from sigil import zeta_session_for_sigil

    runtime_context = zeta_session_for_sigil()
    objective = str(params.get("objective") or "")
    if not objective:
        raise ValueError("session.run requires objective")
    workflow = str(params.get("workflow") or "propose")
    if workflow not in {"ask", "propose", "do"}:
        raise ValueError("workflow must be ask, propose, or do")
    requested_tools = params.get("tools")
    allowed_capabilities = (
        tuple(str(tool) for tool in requested_tools if isinstance(tool, str))
        if isinstance(requested_tools, list)
        else None
    )
    ensure_builtin_tools_registered()
    selected_model = active_model_selection(session_dir=runtime_context.session_dir)
    enabled_capabilities = registered_capabilities(
        allowed_capabilities,
        tool_registry=runtime_context.tool_registry,
    )
    enabled_tool_aliases = tuple(
        runtime_context.tool_registry.model_alias(capability_id)
        for capability_id in enabled_capabilities
    )
    execution_mode: ExecutionMode = "direct" if workflow == "do" else "stage"
    turn_recorder = TurnRecorder(
        runtime_context=runtime_context,
        workflow=workflow,
        objective=objective,
        allowed_tools=enabled_tool_aliases,
        staged=any(
            capability.spec.mutates()
            for name in enabled_capabilities
            if (capability := runtime_context.tool_registry.get(name)) is not None
        )
        and execution_mode == "stage",
        agent=model_selection_event(selected_model) if selected_model else None,
    )
    prior_timeline = current_timeline(runtime_context=runtime_context)
    user_event = record_user_message(
        {
            "type": "user_message",
            "content": objective,
            "workflow": workflow,
            "runtime": "zeta-rpc",
            "turn_id": turn_recorder.turn_id,
            "available_tools": list(enabled_tool_aliases),
        },
        runtime_context=runtime_context,
    )
    turn_recorder.note_root_event(user_event)
    append_prompt_submitted_event(user_event)
    publish_event(user_event)
    direct_event_sink = DirectRuntimeEventSink(runtime_context, lambda: {})

    def sink(event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        if event_type == "tool_result":
            turn_recorder.attach_tool_result_effect(event)
        event["turn_id"] = turn_recorder.turn_id
        persisted = projected_direct_event(direct_event_sink, event)
        if persisted is None:
            persisted = record_runtime_event(event, runtime_context=runtime_context)
        turn_recorder.note_runtime_event(persisted)
        publish_event(persisted)

    try:
        result = run_agent_turn(
            objective,
            prior_timeline,
            AgentConfig(
                system_prompt=params.get("system")
                if isinstance(params.get("system"), str)
                else None,
                allowed_capabilities=enabled_capabilities,
                max_turns=params.get("max_steps")
                if isinstance(params.get("max_steps"), int)
                else None,
                stop_on_staged_effect=True,
                execution_mode=execution_mode,
                model_profile=selected_model.profile
                if selected_model is not None
                else None,
                model_name=selected_model.model if selected_model is not None else None,
                model_url=selected_model.url if selected_model is not None else None,
                model_session_id=runtime_context.session_id,
                thinking=selected_model.thinking
                if selected_model is not None
                else None,
                model_api=selected_model.api if selected_model is not None else None,
                max_wall_seconds=optional_float_param(params, "max_wall_seconds"),
            ),
            context=(
                str(params.get("context"))
                if isinstance(params.get("context"), str)
                else load_project_instructions()
            ),
            event_sink=sink,
            durable_event_sink=direct_event_sink,
            session_id=runtime_context.session_id,
            turn_id=turn_recorder.turn_id,
            trace_store=runtime_context.trace_store,
            tool_registry=runtime_context.tool_registry,
            caused_by=turn_recorder.root_event_id,
        )
    except AgentTurnAborted as error:
        turn_recorder.add_model_calls(error.result.model_telemetry_calls)
        abort_event = error.result.events[-1] if error.event_recorded else None
        if isinstance(abort_event, dict):
            turn_recorder.note_runtime_event(abort_event)
        turn = turn_recorder.finish(
            TURN_OUTCOME_ABORTED,
            prompt_traces=error.result.prompt_traces,
        )
        publish_event(turn)
        return {
            "turn_id": turn_recorder.turn_id,
            "session_id": session_id(),
            "outcome": TURN_OUTCOME_ABORTED,
            "final_text": "",
        }
    except KeyboardInterrupt as error:
        abort_event = record_turn_abort(
            error,
            runtime_context=runtime_context,
            workflow=workflow,
            caused_by=turn_recorder.causal_parent_event_id(),
            reason="keyboard_interrupt",
        )
        turn_recorder.note_runtime_event(abort_event)
        turn = turn_recorder.finish(TURN_OUTCOME_ABORTED)
        publish_event(turn)
        raise
    except RuntimeError as error:
        abort_event = record_turn_abort(
            error,
            runtime_context=runtime_context,
            workflow=workflow,
            caused_by=turn_recorder.causal_parent_event_id(),
        )
        turn_recorder.note_runtime_event(abort_event)
        turn = turn_recorder.finish(TURN_OUTCOME_ABORTED)
        publish_event(turn)
        raise
    turn_recorder.add_model_calls(result.model_telemetry_calls)
    if result.staged_effect is not None:
        outcome = TURN_OUTCOME_STAGED
    elif result.final_text:
        outcome = (
            TURN_OUTCOME_EXECUTED if turn_recorder.effect_ids else TURN_OUTCOME_ANSWERED
        )
    else:
        outcome = TURN_OUTCOME_FAILED
    turn = turn_recorder.finish(outcome, prompt_traces=result.prompt_traces)
    publish_event(turn)
    return {
        "turn_id": turn_recorder.turn_id,
        "session_id": session_id(),
        "outcome": outcome,
        "final_text": result.final_text,
    }


def optional_float_param(params: dict[str, Any], key: str) -> float | None:
    value = params.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def model_telemetry_fields(
    model_telemetry: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(model_telemetry, dict):
        return {}
    fields: dict[str, Any] = {}
    usage = model_telemetry.get("usage")
    if isinstance(usage, dict):
        fields["usage"] = usage
    context_tokens = model_telemetry.get("model_context_tokens")
    if isinstance(context_tokens, int) and not isinstance(context_tokens, bool):
        fields["model_context_tokens"] = context_tokens
    return fields


def event_model_telemetry(event: dict[str, Any]) -> dict[str, Any] | None:
    model_telemetry = event.get("model_telemetry")
    if isinstance(model_telemetry, dict):
        return model_telemetry
    return None
