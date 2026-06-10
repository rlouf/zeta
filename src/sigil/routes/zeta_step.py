"""Python runner for Zeta-backed agent steps.

The sourced shell bindings own the primary interactive loop. This module keeps
CLI-routed glyph steps on the same Zeta service layer without an external agent.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Iterable, Literal, TextIO

from ._turn import (
    TurnEventRecorder,
    TurnRenderer,
    record_turn_abort,
    record_zeta_event,
    build_turn_renderer,
    event_model_telemetry,
    model_server_ready,
    model_telemetry_fields,
    render_final_text,
)
from ..display import (
    render_handoff_lines,
    render_tool_result_summary,
    thinking_status_factory,
)
from ..zeta.agent import AgentConfig, run_agent_turn
from ..zeta.context import load_project_context
from ..zeta.models import active_model_selection, model_selection_event
from ..zeta.prompt import system_prompt
from ..zeta.skills import expand_skill_directive
from ..zeta.timeline import current_timeline, record_event
from ..zeta.tools import allowed_tool_names
from ..zeta.trace import latest_prompt_trace_fields

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
    if not model_server_ready(selected_model):
        return 1
    output = trace_output or sys.stderr
    prompt = agent_prompt(
        expand_skill_directive(objective),
        glyph=glyph,
        stdin_text=stdin_text,
    )
    enabled_tools = enabled_tool_tuple(allowed_tools)
    prior_timeline = current_timeline()
    user_event: dict[str, Any] = {
        "type": "user_message",
        "content": prompt,
        "glyph": glyph,
        "runtime": "zeta",
        "system": system_prompt(system, allowed_tools=enabled_tools),
        "available_tools": list(enabled_tools),
    }
    if selected_model is not None:
        user_event["model"] = model_selection_event(selected_model)
    record_event(user_event)
    context = load_project_context()
    renderer = build_turn_renderer(output)
    recorder = AgentStepEventRecorder(
        renderer,
        glyph=glyph,
        handoff_path=handoff_path,
        handoff_output=handoff_output,
        render_output=output,
    )
    context_footer = renderer.context_footer
    try:
        result = run_agent_turn(
            prompt,
            prior_timeline,
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
                before_start=(
                    context_footer.clear if context_footer is not None else None
                ),
                detail=(
                    context_footer.current_line if context_footer is not None else None
                ),
            ),
            stream_sink=renderer.stream_renderer,
        )
    except RuntimeError as error:
        record_turn_abort(error, glyph=glyph)
        raise
    recorder.replay(result)
    status = recorder.status
    if status is not None:
        record_agent_model_telemetry(
            result.model_telemetry,
            glyph=glyph,
            prompt_traces=result.prompt_traces,
        )
        if context_footer is not None:
            context_footer.finalize(result.model_telemetry)
        return status
    if result.final_text:
        record_agent_model_telemetry(
            result.model_telemetry,
            glyph=glyph,
            prompt_traces=result.prompt_traces,
        )
        if context_footer is not None:
            context_footer.clear()
        render_final_text(
            result.final_text,
            streamed=result.final_text_streamed,
            renderer=renderer,
        )
        if context_footer is not None:
            context_footer.finalize(result.model_telemetry)
        return 0
    print("Zeta stopped without a final answer.", file=sys.stderr)
    return 1


def enabled_tool_tuple(allowed_tools: Iterable[str] | None) -> tuple[str, ...]:
    if allowed_tools is None:
        return tuple(allowed_tool_names())
    return tuple(allowed_tools)


class AgentStepEventRecorder(TurnEventRecorder):
    """Persist and render agent-step events, staging shell handoffs."""

    def __init__(
        self,
        renderer: TurnRenderer,
        *,
        glyph: str,
        handoff_path: str | Path | None,
        handoff_output: HandoffOutput,
        render_output: TextIO,
    ) -> None:
        super().__init__(renderer, render_output=render_output)
        self.tag_fields = {"glyph": glyph}
        self.handoff_path = handoff_path
        self.handoff_output = handoff_output

    def handle_tool_result(self, persisted: dict[str, Any]) -> int | None:
        name = str(persisted.get("name") or "")
        result_payload = persisted.get("result")
        if not isinstance(result_payload, dict):
            if self.renderer.context_footer is not None:
                self.renderer.context_footer.clear()
            print(file=self.render_output)
            self.renderer.trace_state.mark_trace_finished()
            return None
        render_tool_result_summary(
            name,
            result_payload,
            output=self.render_output,
            mark_text_separator=self.renderer.trace_state,
        )
        handoff = result_payload.get("handoff")
        status = None
        if isinstance(handoff, dict):
            write_handoff(self.handoff_path, handoff)
            print_handoff(handoff, mode=self.handoff_output)
            status = 0
        if self.renderer.context_footer is not None:
            self.renderer.context_footer.update_for_tool_result(
                event_model_telemetry(persisted),
                result_payload,
            )
        return status


def write_handoff(path: str | Path | None, handoff: dict[str, Any]) -> None:
    """Write the staged command verbatim for the shell binding to insert."""
    if path is None:
        return
    command = handoff.get("command")
    if not isinstance(command, str) or not command:
        return
    Path(path).write_text(command + "\n", encoding="utf-8")


def print_handoff(
    handoff: dict[str, Any],
    *,
    mode: HandoffOutput = "detail",
) -> None:
    if mode != "detail":
        return
    for line in render_handoff_lines(handoff):
        print(line)


def record_agent_model_telemetry(
    model_telemetry: dict[str, Any] | None,
    *,
    glyph: str,
    prompt_traces: list[Any] | tuple[Any, ...] = (),
) -> None:
    fields = model_telemetry_fields(model_telemetry)
    if not fields:
        return
    fields.update(latest_prompt_trace_fields(prompt_traces))
    record_zeta_event("model_usage", **fields, glyph=glyph)


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
