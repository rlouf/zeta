"""Generic Python workflow for Zeta-backed assistant steps.

The sourced shell bindings own the primary interactive loop. This module keeps
CLI workflow steps on the same Zeta service layer without an external agent.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any, Literal, TextIO

from zeta.agent import AgentConfig, registered_tools, run_agent_turn
from zeta.context import ZetaContext, load_project_context
from zeta.models import (
    active_model_selection,
    model_selection_event,
)
from zeta.prompt import system_prompt
from zeta.skills import expand_skill_directive
from zeta.timeline import current_timeline, record_event
from zeta.tools.base import proposed_effect
from zeta.tools.registry import ExecutionMode, ToolRegistry
from zeta.tools.registry import registry as _default_tool_registry
from zeta.trace import latest_prompt_trace_fields

from ..agent_io import (
    TurnEventRecorder,
    TurnLedger,
    TurnRenderer,
    build_turn_renderer,
    event_model_telemetry,
    model_server_ready,
    model_telemetry_fields,
    record_turn_abort,
    record_zeta_event,
    render_final_text,
)
from ..display.render import (
    PROGRESS_MODE_TRACE,
    render_tool_result_summary,
    thinking_status_factory,
)
from ..display.summarize import render_handoff_lines
from ..protocols import (
    SHELL_HANDOFF_RESULT_SCHEMA,
    TURN_OUTCOME_ABORTED,
    TURN_OUTCOME_ANSWERED,
    TURN_OUTCOME_EXECUTED,
    TURN_OUTCOME_FAILED,
    TURN_OUTCOME_STAGED,
    shell_prompt_handoff,
)
from ..state import append_prompt_submitted_event
from ..tools import ensure_builtin_tools_registered

HandoffOutput = Literal["detail", "summary", "none"]
Workflow = Literal["ask", "propose", "do"]

STEP_SYSTEM_PROMPT = f"""You are Zeta, a shell-native coding agent.

You participate in the user's live shell session. The shell owns control flow,
current working directory, environment, history, job control, and command
handoff. You choose the next small action and then stop.

Work concretely from the available context. Prefer inspection before edits. Use
read-only tools for local context. Follow the active workflow instructions for
whether commands and mutations are staged for review or run directly. Keep
answers concise and do not invent command output, file contents, or tool
results.

Preserve user changes. Do not overwrite files you did not inspect. Avoid
destructive commands unless explicitly requested. Do not commit unless asked.
After direct mutations, run focused verification when practical; if verification
is skipped, say so.

Project context is ordered from broad to local; later, more local instructions
override earlier ones when they conflict.

When the run timeline contains a {SHELL_HANDOFF_RESULT_SCHEMA} result, treat it
as the source of truth for what happened after a shell handoff. If the outcome is
cancelled, do not assume the proposed command ran; use the recorded shell_turns
as user-chosen context and explain the cancellation plainly if it matters.
"""


def step(
    objective: str,
    *,
    workflow: Workflow,
    system: str | None = None,
    prompt: str | None = None,
    stdin_text: str = "",
    max_steps: int | None = None,
    allowed_tools: Iterable[str] | None = None,
    handoff_path: str | Path | None = None,
    handoff_output: HandoffOutput = "detail",
    trace_output: TextIO | None = None,
) -> int:
    """Run a Zeta agent step for CLI workflows.

    A caller-built `prompt` is sent verbatim; otherwise the objective is
    wrapped in the step instruction scaffolding. The do workflow executes
    directly; every other workflow stages mutations for review.
    """
    from .. import configure_zeta_for_sigil, zeta_context_for_sigil

    configure_zeta_for_sigil(responses=True)
    runtime_context = zeta_context_for_sigil()
    system = system or STEP_SYSTEM_PROMPT
    execution_mode: ExecutionMode = "direct" if workflow == "do" else "stage"
    selected_model = active_model_selection(session_dir=runtime_context.session_dir)
    if not model_server_ready(selected_model):
        return 1
    output = trace_output or sys.stderr
    prompt = prompt or agent_prompt(
        expand_skill_directive(objective),
        stdin_text=stdin_text,
    )
    ensure_builtin_tools_registered()
    enabled_tools = registered_tools(
        allowed_tools,
        tool_registry=runtime_context.tool_registry,
    )
    ledger = TurnLedger(
        workflow=workflow,
        objective=objective,
        allowed_tools=enabled_tools,
        staged=stages_mutations(
            execution_mode,
            enabled_tools,
            tool_registry=runtime_context.tool_registry,
        ),
        agent=model_selection_event(selected_model) if selected_model else None,
    )
    prior_timeline = current_timeline(runtime_context=runtime_context)
    user_event: dict[str, Any] = {
        "type": "user_message",
        "content": prompt,
        "workflow": workflow,
        "runtime": "zeta",
        "system": system_prompt(system, allowed_tools=enabled_tools),
        "available_tools": list(enabled_tools),
        "turn_id": ledger.turn_id,
    }
    if selected_model is not None:
        user_event["model"] = model_selection_event(selected_model)
    prompt_event = record_event(user_event, runtime_context=runtime_context)
    append_prompt_submitted_event(prompt_event)
    ledger.note_root_event(prompt_event)
    context = load_project_context()
    renderer = build_turn_renderer(output, objective=objective)
    recorder = AgentStepEventRecorder(
        renderer,
        workflow=workflow,
        handoff_path=handoff_path,
        handoff_output=handoff_output,
        render_output=output,
        ledger=ledger,
        runtime_context=runtime_context,
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
                stop_on_staged_effect=True,
                execution_mode=execution_mode,
                model_profile=(
                    selected_model.profile if selected_model is not None else None
                ),
                model_name=selected_model.model if selected_model is not None else None,
                model_url=selected_model.url if selected_model is not None else None,
                thinking=(
                    selected_model.thinking if selected_model is not None else None
                ),
                model_api=selected_model.api if selected_model is not None else None,
            ),
            context=context,
            event_sink=recorder.record,
            model_status=thinking_status_factory(
                output,
                before_start=(
                    context_footer.clear if context_footer is not None else None
                ),
                detail=turn_status_detail(renderer),
                reasoning_observer=progress_reasoning_observer(renderer),
            ),
            stream_sink=renderer.stream_renderer,
            tool_registry=runtime_context.tool_registry,
            caused_by=ledger.root_event_id,
        )
    except RuntimeError as error:
        record_turn_abort(
            error,
            runtime_context=runtime_context,
            workflow=workflow,
            caused_by=ledger.causal_parent_event_id(),
        )
        turn = ledger.finish(TURN_OUTCOME_ABORTED)
        finalize_progress(renderer, turn)
        raise
    recorder.replay(result)
    ledger.add_model_calls(result.model_telemetry_calls)
    status = recorder.status
    if status is not None:
        record_agent_model_telemetry(
            result.model_telemetry,
            workflow=workflow,
            prompt_traces=result.prompt_traces,
            runtime_context=runtime_context,
        )
        turn = ledger.finish(TURN_OUTCOME_STAGED, prompt_traces=result.prompt_traces)
        finalize_progress(renderer, turn)
        if context_footer is not None:
            context_footer.finalize(result.model_telemetry)
        return status
    if result.final_text:
        if renderer.stream_renderer is not None:
            renderer.stream_renderer.ensure_trace_boundary()
        record_agent_model_telemetry(
            result.model_telemetry,
            workflow=workflow,
            prompt_traces=result.prompt_traces,
            runtime_context=runtime_context,
        )
        turn = ledger.finish(
            TURN_OUTCOME_EXECUTED if ledger.effect_ids else TURN_OUTCOME_ANSWERED,
            prompt_traces=result.prompt_traces,
        )
        if context_footer is not None:
            context_footer.clear()
        render_final_text(
            result.final_text,
            streamed=result.final_text_streamed,
            renderer=renderer,
        )
        finalize_progress(renderer, turn)
        if context_footer is not None:
            context_footer.finalize(result.model_telemetry)
        return 0
    turn = ledger.finish(TURN_OUTCOME_FAILED, prompt_traces=result.prompt_traces)
    finalize_progress(renderer, turn)
    print("Zeta stopped without a final answer.", file=sys.stderr)
    return 1


class AgentStepEventRecorder(TurnEventRecorder):
    """Persist and render agent-step events, staging shell handoffs."""

    def __init__(
        self,
        renderer: TurnRenderer,
        *,
        workflow: str,
        handoff_path: str | Path | None,
        handoff_output: HandoffOutput,
        render_output: TextIO,
        ledger: TurnLedger | None = None,
        runtime_context: ZetaContext | None = None,
    ) -> None:
        super().__init__(
            renderer,
            render_output=render_output,
            ledger=ledger,
            runtime_context=runtime_context,
        )
        self.tag_fields = {"workflow": workflow}
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
        if (
            self.renderer.progress_renderer is not None
            and self.renderer.progress_renderer.mode != PROGRESS_MODE_TRACE
        ):
            if self.renderer.context_footer is not None:
                self.renderer.context_footer.clear()
            if self.renderer.stream_renderer is not None:
                self.renderer.stream_renderer.ensure_trace_boundary()
            self.renderer.progress_renderer.observe_tool_result(name, result_payload)
            self.renderer.trace_state.mark_trace_finished()
        else:
            render_tool_result_summary(
                name,
                result_payload,
                output=self.render_output,
                mark_text_separator=self.renderer.trace_state,
            )
        handoff = shell_handoff_from_tool_result(result_payload)
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


def shell_handoff_from_tool_result(result: dict[str, Any]) -> dict[str, Any] | None:
    effect = proposed_effect(result)
    if effect is None or effect.get("kind") != "command":
        return None
    command = effect.get("command")
    if not isinstance(command, str) or not command:
        return None
    reason = str(effect.get("reason") or "")
    artifact = effect.get("artifact")
    return shell_prompt_handoff(
        command,
        reason,
        artifact=artifact if isinstance(artifact, str) and artifact else None,
    )


def turn_status_detail(renderer: TurnRenderer) -> Callable[[], str]:
    def detail() -> str:
        if renderer.progress_renderer is not None:
            progress = renderer.progress_renderer.status_detail()
            if progress:
                return progress
        if renderer.context_footer is not None:
            return renderer.context_footer.current_line()
        return ""

    return detail


def progress_reasoning_observer(renderer: TurnRenderer) -> Callable[[str], None] | None:
    if (
        renderer.progress_renderer is None
        or renderer.progress_renderer.mode == PROGRESS_MODE_TRACE
    ):
        return None
    return renderer.progress_renderer.observe_reasoning_delta


def finalize_progress(renderer: TurnRenderer, turn: dict[str, Any]) -> None:
    if (
        renderer.progress_renderer is not None
        and renderer.progress_renderer.mode != PROGRESS_MODE_TRACE
    ):
        renderer.progress_renderer.finalize(turn)


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
    workflow: str,
    prompt_traces: Sequence[Any] = (),
    runtime_context: ZetaContext | None = None,
) -> None:
    fields = model_telemetry_fields(model_telemetry)
    if not fields:
        return
    fields.update(latest_prompt_trace_fields(prompt_traces))
    record_zeta_event(
        "model_usage",
        runtime_context=runtime_context,
        **fields,
        workflow=workflow,
    )


def stages_mutations(
    execution_mode: ExecutionMode,
    enabled_tools: tuple[str, ...],
    *,
    tool_registry: ToolRegistry | None = None,
) -> bool:
    """Whether this turn's contract stages mutations for review.

    Stage mode with a purely read-only allow-list (ask) stages nothing.
    """
    if execution_mode != "stage":
        return False
    active_tool_registry = tool_registry or _default_tool_registry
    return any(
        tool.spec.mutates()
        for name in enabled_tools
        if (tool := active_tool_registry.get(name)) is not None
    )


def agent_prompt(objective: str, *, stdin_text: str) -> str:
    sections = [
        "Run the automatic tool loop until no more tool calls are needed.",
        f"Objective: {objective}",
    ]
    if stdin_text:
        sections.append(f"Confirmed piped input:\n{stdin_text}")
    sections.append("When the objective is handled, return a final answer.")
    sections.append("Do not commit.")
    return "\n\n".join(sections)
