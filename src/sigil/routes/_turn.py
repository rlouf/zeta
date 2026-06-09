"""Shared turn plumbing for the Zeta-backed routes.

 Both routes persist agent events to the Zeta run timeline, render tool traces
and a context-usage footer while the loop runs, and replay any events the
recorder missed. This module owns that skeleton; the route modules own
route-specific tagging, logging, and handoff handling.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any, TextIO

from ..display import (
    ContextUsageFooter,
    TraceAwareStreamRenderer,
    TraceRenderState,
    create_stream_renderer,
    render_tool_start,
)
from ..zeta.agent import AgentTurnResult
from ..zeta.model import ensure_server
from ..zeta.models import ModelSelection
from ..zeta.runtime import record_event


def model_server_ready(selected_model: ModelSelection | None) -> bool:
    """Check endpoint reachability for the active model selection."""
    if selected_model is not None:
        return ensure_server(
            selected_url=selected_model.url,
            selected_model=selected_model.model,
        )
    return ensure_server()


@dataclass(frozen=True)
class TurnRenderer:
    """Rendering state shared across one route turn."""

    trace_state: TraceRenderState
    context_footer: ContextUsageFooter | None
    stream_renderer: TraceAwareStreamRenderer | None


def build_turn_renderer(
    footer_output: TextIO,
    *,
    json_output: bool = False,
) -> TurnRenderer:
    """Build the trace state, context footer, and stream renderer for a turn."""
    trace_state = TraceRenderState()
    context_footer = None if json_output else ContextUsageFooter(footer_output)
    base_stream_renderer = create_stream_renderer(sys.stdout, json_output=json_output)
    stream_renderer = (
        TraceAwareStreamRenderer(
            base_stream_renderer,
            trace_state,
            sys.stdout,
            before_output=context_footer.clear if context_footer is not None else None,
        )
        if base_stream_renderer is not None
        else None
    )
    return TurnRenderer(trace_state, context_footer, stream_renderer)


class TurnEventRecorder:
    """Persist and render agent events as the loop produces them.

    Subclasses set ``tag_fields``/``strip_fields`` for timeline tagging and
    override ``handle_tool_call``/``handle_tool_result`` for route behavior.
    ``handle_tool_result`` may return an exit status; the last one wins.
    """

    tag_fields: dict[str, Any] = {}
    strip_fields: frozenset[str] = frozenset()

    def __init__(self, renderer: TurnRenderer, *, render_output: TextIO) -> None:
        self.renderer = renderer
        self.render_output = render_output
        self.recorded_event_ids: set[int] = set()
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
        persisted = self.persist(event_type, event)
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
        fields = {
            key: value
            for key, value in event.items()
            if key != "type" and key not in self.strip_fields
        }
        return record_zeta_event(event_type, **fields, **self.tag_fields)

    def handle_tool_call(self, name: str, args: dict[str, Any]) -> None:
        self.render_tool_call(name, args)

    def render_tool_call(self, name: str, args: dict[str, Any]) -> None:
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


def record_zeta_event(event_type: str, **fields: Any) -> dict[str, Any]:
    return record_event({"type": event_type, **fields})


def record_turn_abort(error: BaseException, **fields: Any) -> dict[str, Any]:
    """Resolve an aborted turn in the timeline instead of dangling its question."""
    return record_zeta_event(
        "turn_aborted",
        error=str(error),
        content=f"(turn aborted: {error})",
        **fields,
    )


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
