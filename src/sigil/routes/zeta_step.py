"""Python runner for Zeta-backed agent steps.

The sourced shell bindings own the primary interactive loop. This module keeps
CLI-routed glyph steps on the same Zeta service layer without an external agent.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Iterable, Literal, TextIO

from ..state import append_jsonl
from ..display import (
    ContextUsageFooter,
    StreamRenderer,
    TraceAwareStreamRenderer,
    TraceRenderState,
    create_stream_renderer,
    render_handoff_lines,
    render_tool_result_summary,
    render_tool_start,
    thinking_status_factory,
)
from ..zeta import runtime
from ..zeta.agent import AgentConfig, AgentTurnResult, run_agent_turn
from ..zeta.models import active_model_selection, model_selection_event
from ..zeta.server import ensure_server

HandoffOutput = Literal["detail", "summary", "none"]
EditMode = Literal["review_patch", "direct_replace"]
ExecutionMode = Literal["handoff", "direct"]


def run_agent_step(
    objective: str,
    *,
    glyph: str,
    system: str | None = None,
    stdin_text: str = "",
    max_steps: int | None = None,
    allowed_tools: Iterable[str] | None = None,
    handoff_path: str | Path | None = None,
    handoff_output: HandoffOutput = "detail",
    trace_output: TextIO | None = None,
    edit_mode: EditMode | None = None,
) -> int:
    """Run a Zeta agent step for CLI routes."""
    selected_model = active_model_selection()
    server_ready = (
        ensure_server(
            selected_url=selected_model.url,
            selected_model=selected_model.model,
        )
        if selected_model is not None
        else ensure_server()
    )
    if not server_ready:
        return 1
    output = trace_output or sys.stderr
    prompt = agent_prompt(
        runtime.expand_skill_directive(objective),
        glyph=glyph,
        stdin_text=stdin_text,
    )
    enabled_tools = enabled_tool_tuple(allowed_tools)
    user_event: dict[str, Any] = {
        "type": "user_message",
        "content": prompt,
        "glyph": glyph,
        "runtime": "zeta",
        "system": runtime.zeta_system_prompt(system, allowed_tools=enabled_tools),
        "available_tools": list(enabled_tools),
    }
    if selected_model is not None:
        user_event["model"] = model_selection_event(selected_model)
    prior_transcript = runtime.transcript_tail()
    append_jsonl(runtime.TRANSCRIPT, user_event)
    context = runtime.load_project_context()
    trace_state = TraceRenderState()
    context_footer = ContextUsageFooter(output)
    base_stream_renderer = create_stream_renderer(sys.stdout)
    stream_renderer = (
        TraceAwareStreamRenderer(
            base_stream_renderer,
            trace_state,
            sys.stdout,
            before_output=context_footer.clear,
        )
        if base_stream_renderer is not None
        else None
    )
    recorder = AgentStepEventRecorder(
        glyph=glyph,
        handoff_path=handoff_path,
        handoff_output=handoff_output,
        output=output,
        stream_renderer=stream_renderer,
        trace_state=trace_state,
        context_footer=context_footer,
    )
    result = run_agent_turn(
        prompt,
        prior_transcript,
        AgentConfig(
            system_prompt=system,
            allowed_tools=enabled_tools,
            max_turns=max_steps,
            stop_on_handoff=True,
            edit_mode=edit_mode or edit_mode_for_glyph(glyph),
            execution_mode=execution_mode_for_glyph(glyph),
            model_profile=(
                selected_model.profile if selected_model is not None else None
            ),
            model_name=selected_model.model if selected_model is not None else None,
            model_url=selected_model.url if selected_model is not None else None,
        ),
        context=context,
        event_sink=recorder.record,
        model_status=thinking_status_factory(
            output,
            before_start=context_footer.clear,
        ),
        stream_sink=stream_renderer,
    )
    status = replay_agent_events(
        result,
        glyph=glyph,
        handoff_path=handoff_path,
        handoff_output=handoff_output,
        output=output,
        skip_event_ids=recorder.recorded_event_ids,
        stream_renderer=stream_renderer,
        trace_state=trace_state,
        context_footer=context_footer,
    )
    if status is None:
        status = recorder.status
    if status is not None:
        record_agent_model_telemetry(result.model_telemetry, glyph=glyph)
        context_footer.finalize(result.model_telemetry)
        return status
    if result.final_text:
        record_agent_model_telemetry(result.model_telemetry, glyph=glyph)
        context_footer.clear()
        record_agent_final(
            result.final_text,
            glyph=glyph,
            answer_streamed=result.final_text_streamed,
            stream_renderer=stream_renderer,
            trace_state=trace_state,
        )
        context_footer.finalize(result.model_telemetry)
        return 0
    print("Zeta stopped without a final answer.", file=sys.stderr)
    return 1


def enabled_tool_tuple(allowed_tools: Iterable[str] | None) -> tuple[str, ...]:
    if allowed_tools is None:
        return tuple(runtime.allowed_tool_names())
    return tuple(allowed_tools)


def record_agent_final(
    content: str,
    *,
    glyph: str,
    answer_streamed: bool = False,
    stream_renderer: StreamRenderer | None = None,
    trace_state: TraceRenderState | None = None,
) -> None:
    del glyph
    if not content:
        return
    if answer_streamed:
        if stream_renderer is not None:
            stream_renderer.finish()
        return
    if trace_state is None or not trace_state.render_text_separator(sys.stdout):
        print()
    print(content)
    print()


class AgentStepEventRecorder:
    """Persist and render agent-step events as the agent loop produces them."""

    def __init__(
        self,
        *,
        glyph: str,
        handoff_path: str | Path | None,
        handoff_output: HandoffOutput,
        output: TextIO,
        stream_renderer: StreamRenderer | None = None,
        trace_state: TraceRenderState | None = None,
        context_footer: ContextUsageFooter | None = None,
    ) -> None:
        self.glyph = glyph
        self.handoff_path = handoff_path
        self.handoff_output = handoff_output
        self.output = output
        self.stream_renderer = stream_renderer
        self.trace_state = trace_state
        self.context_footer = context_footer
        self.recorded_event_ids: set[int] = set()
        self.status: int | None = None

    def record(self, event: dict[str, Any]) -> None:
        self.recorded_event_ids.add(id(event))
        status = record_agent_event(
            event,
            glyph=self.glyph,
            handoff_path=self.handoff_path,
            handoff_output=self.handoff_output,
            output=self.output,
            stream_renderer=self.stream_renderer,
            trace_state=self.trace_state,
            context_footer=self.context_footer,
        )
        if status is not None:
            self.status = status


def replay_agent_events(
    result: AgentTurnResult,
    *,
    glyph: str,
    handoff_path: str | Path | None = None,
    handoff_output: HandoffOutput = "detail",
    output: TextIO = sys.stderr,
    skip_event_ids: set[int] | frozenset[int] = frozenset(),
    stream_renderer: StreamRenderer | None = None,
    trace_state: TraceRenderState | None = None,
    context_footer: ContextUsageFooter | None = None,
) -> int | None:
    status: int | None = None
    for event in result.events:
        if id(event) in skip_event_ids:
            continue
        event_status = record_agent_event(
            event,
            glyph=glyph,
            handoff_path=handoff_path,
            handoff_output=handoff_output,
            output=output,
            stream_renderer=stream_renderer,
            trace_state=trace_state,
            context_footer=context_footer,
        )
        if event_status is not None:
            status = event_status
    return status


def record_agent_event(
    event: dict[str, Any],
    *,
    glyph: str,
    handoff_path: str | Path | None = None,
    handoff_output: HandoffOutput = "detail",
    output: TextIO = sys.stderr,
    stream_renderer: StreamRenderer | None = None,
    trace_state: TraceRenderState | None = None,
    context_footer: ContextUsageFooter | None = None,
) -> int | None:
    event_type = str(event.get("type") or "")
    fields = {key: value for key, value in event.items() if key != "type"}
    persisted = append_zeta_event(event_type, **fields, glyph=glyph)
    if event_type == "tool_call":
        if context_footer is not None:
            context_footer.clear()
        if stream_renderer is not None:
            stream_renderer.ensure_trace_boundary()
        params = persisted.get("input")
        render_tool_start(
            str(persisted.get("name") or ""),
            params if isinstance(params, dict) else {},
            output=output,
            newline=False,
        )
        return None
    if event_type != "tool_result":
        return None
    name = str(persisted.get("name") or "")
    result_payload = persisted.get("result")
    if not isinstance(result_payload, dict):
        if context_footer is not None:
            context_footer.clear()
        print(file=output)
        if trace_state is not None:
            trace_state.mark_trace_finished()
        return None
    render_tool_result_summary(
        name,
        result_payload,
        output=output,
        mark_text_separator=trace_state,
    )
    handoff = result_payload.get("handoff")
    status = None
    if isinstance(handoff, dict):
        write_handoff(handoff_path, handoff)
        print_handoff(handoff, mode=handoff_output)
        status = 0
    if context_footer is not None:
        context_footer.update(event_model_telemetry(persisted))
    return status


def write_handoff(path: str | Path | None, handoff: dict[str, Any]) -> None:
    if path is None:
        return
    Path(path).write_text(
        json.dumps(handoff, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def print_handoff(
    handoff: dict[str, Any],
    *,
    mode: HandoffOutput = "detail",
) -> None:
    if mode != "detail":
        return
    for line in render_handoff_lines(handoff):
        print(line)


def append_zeta_event(event_type: str, **fields: Any) -> dict[str, Any]:
    return append_jsonl(runtime.TRANSCRIPT, {"type": event_type, **fields})


def record_agent_model_telemetry(
    model_telemetry: dict[str, Any] | None,
    *,
    glyph: str,
) -> None:
    fields = model_telemetry_fields(model_telemetry)
    if fields:
        append_zeta_event("model_usage", **fields, glyph=glyph)


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


def edit_mode_for_glyph(glyph: str) -> EditMode:
    if glyph == ",,,":
        return "direct_replace"
    return "review_patch"


def execution_mode_for_glyph(glyph: str) -> ExecutionMode:
    if glyph == ",,,":
        return "direct"
    return "handoff"


def agent_prompt(objective: str, *, glyph: str, stdin_text: str) -> str:
    instruction = (
        "Run the automatic tool loop until no more tool calls are needed."
        if glyph in {",,", ",,,"}
        else "Run one edit step."
    )
    sections = [instruction, f"Objective: {objective}"]
    if stdin_text:
        sections.append(f"Confirmed piped input:\n{stdin_text}")
    if glyph in {",,", ",,,"}:
        sections.append("When the objective is handled, return a final answer.")
    else:
        sections.append("After the step, stop.")
    sections.append("Do not commit.")
    return "\n\n".join(sections)
