"""Python fallback runner for Zeta-backed Sigil routes.

The sourced shell bindings own the primary interactive loop. This module keeps
non-sourced CLI routes on the same Zeta service layer without an external agent.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Iterable, TextIO

from ..ansi import MUTED, RESET
from ..model import chat_text, ensure_server
from ..state import append_event, append_jsonl
from . import runtime
from .stream import TRACE_LABEL_WIDTH, muted, should_color, summarize

QUESTION_TOOLS = ("read", "grep", "ls")


def run_text_answer(
    system: str,
    prompt: str,
    *,
    question: str = "",
    follow_up: bool = False,
    json_output: bool = False,
    max_tokens: int = 1200,
) -> int:
    """Run a plain Zeta model answer and persist question state."""
    if not ensure_server():
        return 1
    answer = chat_text(system, prompt, max_tokens=max_tokens)
    append_event(
        {
            "type": "answer",
            "question": question,
            "prompt": prompt,
            "answer": answer,
            "runtime": "zeta",
        }
    )
    append_jsonl(
        "last-question.jsonl",
        {
            "role": "assistant",
            "content": answer,
            "question": question,
            "prompt": prompt,
            "follow_up": follow_up,
            "runtime": "zeta",
        },
    )
    if json_output:
        print(
            json.dumps(
                {
                    "question": question,
                    "prompt": prompt,
                    "answer": answer,
                    "runtime": "zeta",
                    "tools": [],
                    "malformed_events": 0,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    else:
        print(answer)
    return 0


def run_question_answer(
    system: str,
    prompt: str,
    *,
    question: str = "",
    follow_up: bool = False,
    json_output: bool = False,
    max_steps: int = 4,
    allowed_tools: Iterable[str] = QUESTION_TOOLS,
) -> int:
    """Run a read-only Zeta question turn and persist question state."""
    if not ensure_server():
        return 1
    enabled_tools = tuple(allowed_tools)
    user_event: dict[str, Any] = {
        "type": "user_message",
        "content": prompt,
        "runtime": "zeta",
        "route": "question",
        "system": system,
        "available_tools": list(enabled_tools),
    }
    append_jsonl(runtime.TRANSCRIPT, user_event)
    turn_events: list[dict[str, Any]] = [user_event]
    tool_events: list[dict[str, Any]] = []
    answer = ""
    for _ in range(max_steps):
        action = runtime.next_model_action(
            prompt,
            turn_events,
            system=system,
            allowed_tools=enabled_tools,
        )
        if action["type"] == "final":
            answer = str(action.get("content") or "")
            if not answer:
                turn_events.append({"type": "empty_final"})
                continue
            break
        name = str(action["name"])
        params = action.get("input")
        if not isinstance(params, dict):
            print("zeta: invalid tool input", file=sys.stderr)
            return 1
        tool_call = {
            "type": "tool_call",
            "name": name,
            "input": params,
            "route": "question",
        }
        trace = append_zeta_event(
            "tool_call", name=name, input=params, route="question"
        )
        turn_events.append({**tool_call, "id": trace["id"]})
        append_jsonl(
            "last-tools.jsonl", {"type": "tool_start", "tool": name, "args": params}
        )
        if not json_output:
            render_tool_start(name, params, output=sys.stdout)
        analysis = runtime.analyze_tool(name, params)
        append_zeta_event(
            "tool_analysis",
            tool_call_id=trace["id"],
            name=name,
            analysis=analysis,
            route="question",
        )
        result = runtime.run_tool(name, params)
        result_event = {
            "type": "tool_result",
            "tool_call_id": trace["id"],
            "name": name,
            "result": result,
            "route": "question",
        }
        append_zeta_event(
            "tool_result",
            tool_call_id=trace["id"],
            name=name,
            result=result,
            route="question",
        )
        turn_events.append(result_event)
        event = {"type": "tool_end", "tool": name, "result": result}
        append_jsonl("last-tools.jsonl", event)
        tool_events.append(event)
    if not answer:
        answer = fallback_question_answer(system, prompt, turn_events)
    record_question_answer(
        question=question,
        prompt=prompt,
        answer=answer,
        follow_up=follow_up,
        tools=tool_events,
        json_output=json_output,
    )
    return 0


def fallback_question_answer(
    system: str,
    prompt: str,
    turn_events: list[dict[str, Any]],
) -> str:
    """Answer from the current turn transcript when structured stepping stalls."""
    fallback_prompt = "\n\n".join(
        [
            "Answer the user's question using only this current Zeta turn transcript.",
            "Do not request tools. If the transcript is insufficient, say what is missing.",
            f"Question:\n{prompt}",
            f"Current turn transcript JSON:\n{json.dumps(turn_events, ensure_ascii=False)}",
        ]
    )
    answer = chat_text(system, fallback_prompt, max_tokens=1200).strip()
    if answer:
        return answer
    return "I could not answer from the available local context."


def record_question_answer(
    *,
    question: str,
    prompt: str,
    answer: str,
    follow_up: bool,
    tools: list[dict[str, Any]],
    json_output: bool,
) -> None:
    append_event(
        {
            "type": "answer",
            "question": question,
            "prompt": prompt,
            "answer": answer,
            "runtime": "zeta",
        }
    )
    append_jsonl(
        "last-question.jsonl",
        {
            "role": "assistant",
            "content": answer,
            "question": question,
            "prompt": prompt,
            "follow_up": follow_up,
            "runtime": "zeta",
        },
    )
    if json_output:
        print(
            json.dumps(
                {
                    "question": question,
                    "prompt": prompt,
                    "answer": answer,
                    "runtime": "zeta",
                    "tools": tools,
                    "malformed_events": 0,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return
    print(answer)


def run_agent_step(
    objective: str,
    *,
    glyph: str,
    system: str | None = None,
    stdin_text: str = "",
    goal: bool = False,
    max_steps: int = 8,
    allowed_tools: Iterable[str] | None = None,
) -> int:
    """Run a bounded Zeta agent step for CLI routes."""
    if not ensure_server():
        return 1
    prompt = agent_prompt(objective, stdin_text=stdin_text, goal=goal)
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
            "system": system or runtime.zeta_system_prompt(),
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
    append_jsonl(
        "last-question.jsonl",
        {
            "role": "assistant",
            "content": content,
            "runtime": "zeta",
            "glyph": glyph,
        },
    )


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


def render_tool_start(name: str, params: dict[str, Any], *, output: TextIO) -> None:
    """Print a visible tool-start line using the same shape as the stream renderer."""
    detail = summarize(name, params)
    status = f"❯ {name:<{TRACE_LABEL_WIDTH}}  {detail}" if detail else f"❯ {name}"
    print(muted(status, enabled=should_color(output)), file=output, flush=True)


def append_zeta_event(event_type: str, **fields: Any) -> dict[str, Any]:
    return append_jsonl(runtime.TRANSCRIPT, {"type": event_type, **fields})


def agent_prompt(objective: str, *, stdin_text: str, goal: bool) -> str:
    sections = [
        "Run one bounded Sigil goal step."
        if goal
        else "Run one bounded Sigil edit step.",
        f"Objective: {objective}",
    ]
    if stdin_text:
        sections.append(f"Confirmed piped input:\n{stdin_text}")
    if goal:
        sections.append(
            "After the step, stop. End with exactly one SIGIL_STATUS line set "
            "to continue, complete, or blocked, followed by one SIGIL_NEXT line."
        )
    else:
        sections.append("After the step, stop. Do not commit.")
    return "\n\n".join(sections)
