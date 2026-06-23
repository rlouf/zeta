"""Shared I/O plumbing for Zeta-backed agent workflows.

Both workflows persist agent events to the Zeta run timeline, render tool traces
and a context-usage footer while the loop runs, and replay any events the
recorder missed. This module owns that skeleton; workflow modules own
workflow-specific tagging, logging, and handoff handling.
"""

import sys
from dataclasses import dataclass, replace
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
from sigil.display.tty import is_interactive
from sigil.turn import TurnRecorder
from zeta.models import (
    CODEX_RESPONSES_API,
    ModelSelection,
)
from zeta.models.chat_completions import ensure_server
from zeta.records.events import (
    DraftEvent,
    Event,
    draft_event_id,
    draft_event_view,
    event_view,
    exact_event_time,
    user_message_draft,
)
from zeta.records.provenance import project_prompt_trace_projection
from zeta.records.stores import (
    EventReader,
    Filter,
    SqliteEventStore,
    Store,
    warn_trace_failure_once,
)
from zeta.run.context import RuntimeContext
from zeta.run.runtime import (
    AgentRunResult,
    is_runtime_ui_event,
)

RuntimePublishedEvent = Event | DraftEvent
STAGING_TOOL_NAMES = frozenset({"bash", "edit", "write"})


def current_timeline(*, runtime_context: RuntimeContext) -> list[Event]:
    try:
        if not isinstance(runtime_context.event_sink, EventReader):
            return []
        return runtime_context.event_sink.list_events(
            Filter(
                session_id=runtime_context.session_id,
                event_type_prefix="zeta.",
            )
        )
    except Exception as exc:
        warn_trace_failure_once("current_timeline", exc)
        return []


def record_trace_for_turn(runtime_context: RuntimeContext, turn_id: str | None) -> None:
    if turn_id is None or not isinstance(runtime_context.event_sink, EventReader):
        return
    try:
        project_prompt_trace_projection(
            runtime_context.event_sink.list_events(
                Filter(
                    session_id=runtime_context.session_id,
                    turn_id=turn_id,
                    event_type_prefix="zeta.",
                )
            ),
            runtime_context.trace_store,
        )
    except Exception as exc:
        warn_trace_failure_once("record_trace_for_turn", exc)


def last_event_time(*, store: Store, run_id: str | None = None) -> float | None:
    try:
        path = getattr(store, "path", None)
        if path is None:
            return None
        events = SqliteEventStore(path).list_events(Filter(session_id=run_id))
        zeta_events = [
            event for event in events if event.event_type.startswith("zeta.")
        ]
        if not zeta_events:
            return None
        return exact_event_time(zeta_events[-1])
    except Exception as exc:
        warn_trace_failure_once("last_event_time", exc)
        return None


def record_user_message(
    event: dict[str, Any],
    *,
    runtime_context: RuntimeContext,
) -> Event:
    payload = {key: value for key, value in event.items() if key != "type"}
    outcome = runtime_context.event_sink.accept(
        user_message_draft(
            payload,
            session_id=runtime_context.session_id,
            turn_id=event.get("turn_id")
            if isinstance(event.get("turn_id"), str)
            else None,
        )
    )
    return outcome.event


def record_runtime_event(
    draft: DraftEvent,
    *,
    runtime_context: RuntimeContext,
    tag_fields: dict[str, Any] | None = None,
    strip_fields: frozenset[str] = frozenset(),
    turn_id: str | None = None,
) -> Event:
    tagged = {
        key: value for key, value in draft.payload.items() if key not in strip_fields
    }
    if tag_fields is not None:
        tagged.update(tag_fields)
    tagged_draft = replace(
        draft,
        payload=tagged,
        session_id=runtime_context.session_id,
        turn_id=turn_id or draft.turn_id,
    )
    append = getattr(runtime_context.event_sink, "append", None)
    event_id = draft_event_id(tagged_draft)
    if callable(append) and event_id is not None:
        outcome = append(
            Event(
                id=event_id,
                event_type=tagged_draft.event_type,
                source=tagged_draft.source,
                payload=tagged_draft.payload,
                idempotency_key=tagged_draft.idempotency_key,
                caused_by=tagged_draft.caused_by,
                session_id=tagged_draft.session_id,
                turn_id=tagged_draft.turn_id,
                timestamp_ms=time_ms(),
            )
        )
    else:
        outcome = runtime_context.event_sink.accept(tagged_draft)
    record_trace_for_turn(runtime_context, outcome.event.turn_id)
    return outcome.event


def time_ms() -> int:
    import time

    return time.time_ns() // 1_000_000


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
        runtime_context: RuntimeContext,
    ) -> None:
        self.renderer = renderer
        self.render_output = render_output
        self.turn_recorder = turn_recorder
        self.runtime_context = runtime_context
        self.recorded_event_ids: set[int] = set()
        self.status: int | None = None

    def record(self, draft: DraftEvent) -> None:
        self.recorded_event_ids.add(id(draft))
        projected_for_effects = dict(draft.payload)
        if (
            projected_for_effects.get("_timeline_type") == "tool_result"
            and self.turn_recorder is not None
        ):
            self.turn_recorder.attach_tool_result_effect(projected_for_effects)
            effects = projected_for_effects.get("effects")
            if effects is not None:
                draft = replace(draft, payload={**draft.payload, "effects": effects})
        if is_runtime_ui_event(draft):
            persisted = self.transient(draft)
        else:
            persisted = self.persist(draft)
        rendered = (
            event_view(persisted)
            if isinstance(persisted, Event)
            else draft_event_view(persisted)
        )
        event_type = str(rendered.get("type") or "")
        if self.turn_recorder is not None:
            self.turn_recorder.note_runtime_event(persisted)
        if event_type == "runtime.stream.chunk":
            text = rendered.get("text")
            if isinstance(text, str) and self.renderer.stream_renderer is not None:
                self.renderer.stream_renderer.content_delta(text)
            return
        if event_type == "runtime.status.update":
            text = rendered.get("text")
            if (
                isinstance(text, str)
                and self.renderer.progress_renderer is not None
                and self.renderer.progress_renderer.mode != PROGRESS_MODE_TRACE
            ):
                self.renderer.progress_renderer.observe_reasoning_delta(text)
            return
        if event_type == "tool_call":
            params = rendered.get("input")
            self.handle_tool_call(
                str(rendered.get("name") or ""),
                params if isinstance(params, dict) else {},
            )
            return
        if event_type != "tool_result":
            return
        status = self.handle_tool_result(rendered)
        if status is not None:
            self.status = status

    def replay(self, result: AgentRunResult) -> None:
        """Record any turn events the live sink did not see."""
        for draft in result.events:
            if id(draft) in self.recorded_event_ids:
                continue
            self.record(draft)

    def persist(self, draft: DraftEvent) -> Event:
        return record_runtime_event(
            draft,
            runtime_context=self.runtime_context,
            tag_fields=self.tag_fields,
            strip_fields=self.strip_fields,
            turn_id=(
                self.turn_recorder.turn_id if self.turn_recorder is not None else None
            ),
        )

    def transient(self, draft: DraftEvent) -> DraftEvent:
        payload = {
            key: value
            for key, value in draft.payload.items()
            if key not in self.strip_fields
        }
        payload.update(self.tag_fields)
        tagged = replace(
            draft,
            payload=payload,
            session_id=self.runtime_context.session_id,
            turn_id=(
                self.turn_recorder.turn_id if self.turn_recorder is not None else None
            )
            or draft.turn_id,
        )
        return tagged

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


def render_final_answer(
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
    if is_interactive(sys.stdout):
        if not renderer.trace_state.render_text_separator(sys.stdout):
            print()
    print(content)
    if is_interactive(sys.stdout):
        print()


def record_turn_abort(
    error: BaseException,
    *,
    runtime_context: RuntimeContext,
    reason: str | None = None,
    **fields: Any,
) -> Event:
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
    return outcome.event


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
