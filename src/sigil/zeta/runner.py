"""Python fallback runner for Zeta-backed agent steps.

The sourced shell bindings own the primary interactive loop. This module keeps
CLI-routed act steps on the same Zeta service layer without an external agent.
"""

from __future__ import annotations

import sys
from typing import Any, Iterable

from ..model import ensure_server
from ..state import append_jsonl
from ..tty import MUTED, RESET
from . import runtime
from ..display import render_tool_start


def run_agent_step(
    objective: str,
    *,
    glyph: str,
    system: str | None = None,
    stdin_text: str = "",
    max_steps: int = 8,
    allowed_tools: Iterable[str] | None = None,
) -> int:
    """Run a bounded Zeta agent step for CLI routes."""
    if not ensure_server():
        return 1
    prompt = agent_prompt(objective, stdin_text=stdin_text)
    enabled_tools = enabled_tool_tuple(allowed_tools)
    tool_label = "+".join(enabled_tools) if enabled_tools else "no tools"
    print(
        f"{MUTED}❯ zeta {glyph:<5} · {tool_label} · one step{RESET}",
        file=sys.stderr,
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
    for _ in range(max_steps):
        action = runtime.next_model_action(
            prompt,
            runtime.transcript_tail(),
            system=system,
            allowed_tools=enabled_tools,
        )
        if action["type"] == "final":
            content = str(action.get("content") or "")
            record_agent_final(content, glyph=glyph)
            return 0
        status = run_agent_tool_action(action, glyph=glyph)
        if status is not None:
            return status
    print("Zeta stopped after reaching the step budget.", file=sys.stderr)
    return 1


def enabled_tool_tuple(allowed_tools: Iterable[str] | None) -> tuple[str, ...]:
    if allowed_tools is None:
        return tuple(runtime.TOOL_SPECS)
    return tuple(allowed_tools)


def record_agent_final(content: str, *, glyph: str) -> None:
    if not content:
        return
    print(content)
    append_zeta_event("assistant_message", content=content, glyph=glyph)


def run_agent_tool_action(action: dict[str, Any], *, glyph: str) -> int | None:
    name = str(action["name"])
    params = action.get("input")
    if not isinstance(params, dict):
        print("zeta: invalid tool input", file=sys.stderr)
        return 1
    call = append_zeta_event("tool_call", name=name, input=params, glyph=glyph)
    render_tool_start(name, params, output=sys.stderr)
    analysis = runtime.analyze_tool(name, params)
    append_zeta_event(
        "tool_analysis",
        tool_call_id=call["id"],
        name=name,
        analysis=analysis,
        glyph=glyph,
    )
    result = runtime.run_tool(name, params)
    append_zeta_event(
        "tool_result",
        tool_call_id=call["id"],
        name=name,
        result=result,
        glyph=glyph,
    )
    handoff = result.get("handoff")
    if not isinstance(handoff, dict):
        return None
    print_handoff(handoff)
    return 0


def print_handoff(handoff: dict[str, Any]) -> None:
    reason = str(handoff.get("reason") or "")
    command = str(handoff.get("command") or "")
    artifact = str(handoff.get("artifact") or "")
    if reason:
        print(reason)
    if artifact:
        print(f"artifact: {artifact}")
    if command:
        print(command)


def append_zeta_event(event_type: str, **fields: Any) -> dict[str, Any]:
    return append_jsonl(runtime.TRANSCRIPT, {"type": event_type, **fields})


def agent_prompt(objective: str, *, stdin_text: str) -> str:
    sections = [
        "Run one bounded edit step.",
        f"Objective: {objective}",
    ]
    if stdin_text:
        sections.append(f"Confirmed piped input:\n{stdin_text}")
    sections.append("After the step, stop. Do not commit.")
    return "\n\n".join(sections)
