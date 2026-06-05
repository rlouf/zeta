"""Python fallback runner for Zeta-backed agent steps.

The sourced shell bindings own the primary interactive loop. This module keeps
CLI-routed act steps on the same Zeta service layer without an external agent.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Iterable, Literal, TextIO

from ..state import append_jsonl
from ..display import (
    render_handoff_lines,
    render_tool_start,
    render_zeta_status,
    tool_result_summary,
)
from ..zeta import runtime
from ..zeta.agent import AgentConfig, AgentTurnResult, run_agent_turn
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
    max_steps: int = 8,
    allowed_tools: Iterable[str] | None = None,
    handoff_path: str | Path | None = None,
    handoff_output: HandoffOutput = "detail",
    trace_output: TextIO | None = None,
    edit_mode: EditMode | None = None,
) -> int:
    """Run a bounded Zeta agent step for CLI routes."""
    if not ensure_server():
        return 1
    output = trace_output or sys.stderr
    prompt = agent_prompt(objective, glyph=glyph, stdin_text=stdin_text)
    enabled_tools = enabled_tool_tuple(allowed_tools)
    render_zeta_status(
        glyph,
        enabled_tools,
        "auto loop" if glyph in {",,", ",,,"} else "one step",
        output=output,
        color_enabled=True,
    )
    append_jsonl(
        runtime.TRANSCRIPT,
        {
            "type": "user_message",
            "content": prompt,
            "glyph": glyph,
            "runtime": "zeta",
            "system": runtime.zeta_system_prompt(system, allowed_tools=enabled_tools),
            "available_tools": list(enabled_tools),
        },
    )
    context = runtime.load_project_context()
    result = run_agent_turn(
        prompt,
        runtime.transcript_tail(),
        AgentConfig(
            system_prompt=system,
            allowed_tools=enabled_tools,
            max_turns=max_steps,
            stop_on_handoff=True,
            edit_mode=edit_mode or edit_mode_for_glyph(glyph),
            execution_mode=execution_mode_for_glyph(glyph),
        ),
        context=context,
    )
    status = replay_agent_events(
        result,
        glyph=glyph,
        handoff_path=handoff_path,
        handoff_output=handoff_output,
        output=output,
    )
    if status is not None:
        return status
    if result.final_text:
        record_agent_final(result.final_text, glyph=glyph)
        return 0
    print("Zeta stopped after reaching the step budget.", file=sys.stderr)
    return 1


def enabled_tool_tuple(allowed_tools: Iterable[str] | None) -> tuple[str, ...]:
    if allowed_tools is None:
        return tuple(runtime.TOOL_SPECS)
    return tuple(allowed_tools)


def record_agent_final(content: str, *, glyph: str) -> None:
    del glyph
    if not content:
        return
    print()
    print(content)


def replay_agent_events(
    result: AgentTurnResult,
    *,
    glyph: str,
    handoff_path: str | Path | None = None,
    handoff_output: HandoffOutput = "detail",
    output: TextIO = sys.stderr,
) -> int | None:
    status: int | None = None
    for event in result.events:
        event_type = str(event.get("type") or "")
        fields = {key: value for key, value in event.items() if key != "type"}
        persisted = append_zeta_event(event_type, **fields, glyph=glyph)
        if event_type == "tool_call":
            params = persisted.get("input")
            render_tool_start(
                str(persisted.get("name") or ""),
                params if isinstance(params, dict) else {},
                output=output,
            )
            continue
        if event_type != "tool_result":
            continue
        name = str(persisted.get("name") or "")
        result_payload = persisted.get("result")
        if not isinstance(result_payload, dict):
            continue
        render_result_summary(name, result_payload, output=output)
        handoff = result_payload.get("handoff")
        if not isinstance(handoff, dict):
            continue
        write_handoff(handoff_path, handoff)
        print_handoff(handoff, mode=handoff_output)
        status = 0
    return status


def render_result_summary(
    name: str,
    result: dict[str, Any],
    *,
    output: TextIO,
) -> None:
    for line in tool_result_summary(name, result):
        print(f"  {line}", file=output)


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


def edit_mode_for_glyph(glyph: str) -> EditMode:
    if glyph in {",,", ",,,"}:
        return "direct_replace"
    return "review_patch"


def execution_mode_for_glyph(glyph: str) -> ExecutionMode:
    if glyph in {",,", ",,,"}:
        return "direct"
    return "handoff"


def agent_prompt(objective: str, *, glyph: str, stdin_text: str) -> str:
    instruction = (
        "Run the bounded automatic tool loop until no more tool calls are needed."
        if glyph in {",,", ",,,"}
        else "Run one bounded edit step."
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
