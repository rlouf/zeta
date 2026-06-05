"""Read-only shell answer routes.

This module owns discussion continuity. A fresh `sigil ask` resets the session
answer transcript. Comma glyphs and named ask routes use explicit source
authorization.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Iterable

from ..session import recent_turns_context
from ..state import (
    ANSWER_TRANSCRIPT,
    append_event,
    append_jsonl,
    read_jsonl,
    write_jsonl,
)
from ..display import render_tool_start, render_zeta_status
from ..zeta import runtime
from ..zeta.agent import AgentConfig, AgentTurnResult, run_agent_turn
from ..zeta.model import chat_text
from ..zeta.server import ensure_server


ANSWER_ROUTE = "answer"
ANSWER_REQUEST_EVENT = "answer_requested"

ANSWER_SYSTEM_PROMPT = (
    "Answer concisely. You are responding to a quick question typed at a shell "
    "prompt. The available tools are read, grep, and ls only. Use read for "
    "files, ls for directory contents, file sizes, and recursive size-filtered "
    "listings, and grep to search local text. Do not "
    "propose shell commands just to inspect files or directories; inspect them "
    "through the available tools. If a 'Recent shell activity' block appears "
    "in the user message, it already shows the last few commands. For older "
    "sessions or audit history, the read tool can access ~/.sigil/events.jsonl. "
    "Do not mutate files or execute commands."
)

ZETA_ANSWER_TOOLS = "read,grep,ls"
ANSWER_TOOLS = ("read", "grep", "ls")


def parse_tools(tools: str) -> tuple[str, ...]:
    """Parse a comma-separated tool allowlist."""
    return tuple(tool.strip() for tool in tools.split(",") if tool.strip())


def discussion_turns() -> list[dict[str, object]]:
    """Load user/assistant turns for explicit follow-up commands."""
    return [
        turn
        for turn in read_jsonl(ANSWER_TRANSCRIPT)
        if turn.get("role") in {"user", "assistant"} and turn.get("content")
    ]


def prepend_recent_turns(user_input: str) -> str:
    """Attach recent shell activity to a fresh question prompt."""
    from ..failure import active_failure_context

    sections = []
    context = recent_turns_context()
    if context:
        sections.append(context)
    failure = active_failure_context()
    if failure:
        sections.append(failure)
    if not sections:
        return user_input
    sections.append(f"Question:\n{user_input}")
    return "\n\n".join(sections)


RECENT_ANSWER_TURNS_LIMIT = 4
RECENT_ANSWER_TURN_CHARS = 500


def recent_answer_context(
    limit: int = RECENT_ANSWER_TURNS_LIMIT,
    per_turn_chars: int = RECENT_ANSWER_TURN_CHARS,
) -> str:
    """Return a compact summary of the most recent answer exchange, if any."""
    turns = discussion_turns()
    if not turns:
        return ""
    tail = turns[-limit:]
    lines = ["Recent answer transcript:"]
    for turn in tail:
        role = str(turn.get("role", "?"))
        content = str(turn.get("content", "")).strip()
        if len(content) > per_turn_chars:
            content = content[:per_turn_chars] + "…"
        lines.append(f"  {role}: {content}")
    return "\n".join(lines)


def ask(
    question: str,
    *,
    glyph: str = "ask",
    tools: str = ZETA_ANSWER_TOOLS,
    append_transcript: bool = False,
    json_output: bool = False,
    history: Iterable[dict[str, object]] = (),
) -> int:
    """Run Zeta for a shell answer while recording transcript state."""
    user_input = question
    expanded_input = runtime.expand_skill_directive(user_input)
    prompt = (
        expanded_input if append_transcript else prepend_recent_turns(expanded_input)
    )
    history_turns = list(history)
    request_event = append_event(
        {
            "type": ANSWER_REQUEST_EVENT,
            "input": user_input,
            "prompt": prompt,
            "follow_up": append_transcript,
            "glyph": glyph,
            "history_turns": len(history_turns),
        }
    )
    user_turn = {
        "role": "user",
        "content": user_input,
        "prompt": prompt,
        "follow_up": append_transcript,
        "event_id": request_event["id"],
        "glyph": glyph,
    }
    if append_transcript:
        append_jsonl(ANSWER_TRANSCRIPT, user_turn)
    else:
        write_jsonl(ANSWER_TRANSCRIPT, [user_turn])
    write_jsonl("last-tools.jsonl", [])
    enabled_tools = parse_tools(tools)
    render_zeta_status(
        glyph,
        enabled_tools,
        "no execute path",
        output=sys.stderr,
        color_enabled=False,
    )
    return run_tool_answer(
        ANSWER_SYSTEM_PROMPT,
        prompt,
        input_text=user_input,
        follow_up=append_transcript,
        json_output=json_output,
        allowed_tools=enabled_tools,
        history=history_turns,
    )


def run_tool_answer(
    system: str,
    prompt: str,
    *,
    input_text: str = "",
    follow_up: bool = False,
    json_output: bool = False,
    max_steps: int = 4,
    allowed_tools: Iterable[str] = ANSWER_TOOLS,
    history: Iterable[dict[str, object]] = (),
) -> int:
    """Run a read-only Zeta answer turn and persist answer state."""
    if not ensure_server():
        return 1
    enabled_tools = tuple(allowed_tools)
    user_event: dict[str, Any] = {
        "type": "user_message",
        "content": prompt,
        "runtime": "zeta",
        "route": ANSWER_ROUTE,
        "system": system,
        "available_tools": list(enabled_tools),
    }
    append_jsonl(runtime.TRANSCRIPT, user_event)
    turn_events: list[dict[str, Any]] = [
        dict(turn) for turn in history if turn.get("role") in {"user", "assistant"}
    ]
    turn_events.append(user_event)
    result = run_agent_turn(
        prompt,
        turn_events,
        AgentConfig(
            system_prompt=system,
            allowed_tools=enabled_tools,
            max_turns=max_steps,
            stop_on_handoff=True,
        ),
        context=runtime.load_project_context(),
    )
    turn_events.extend(result.events)
    tool_events = replay_answer_events(result, json_output=json_output)
    answer = result.final_text
    if not answer:
        answer = fallback_answer(system, prompt, turn_events)
    record_answer(
        input_text=input_text,
        prompt=prompt,
        answer=answer,
        follow_up=follow_up,
        tools=tool_events,
        json_output=json_output,
    )
    return 0


def replay_answer_events(
    result: AgentTurnResult,
    *,
    json_output: bool,
) -> list[dict[str, Any]]:
    tool_events: list[dict[str, Any]] = []
    for event in result.events:
        event_type = str(event.get("type") or "")
        fields = {
            key: value for key, value in event.items() if key not in {"type", "route"}
        }
        trace = append_zeta_event(event_type, **fields, route=ANSWER_ROUTE)
        if event_type == "tool_call":
            name = str(trace.get("name") or "")
            params = trace.get("input")
            args = params if isinstance(params, dict) else {}
            append_jsonl(
                "last-tools.jsonl", {"type": "tool_start", "tool": name, "args": args}
            )
            if not json_output:
                render_tool_start(name, args, output=sys.stdout)
            continue
        if event_type != "tool_result":
            continue
        name = str(trace.get("name") or "")
        result_payload = trace.get("result")
        if not isinstance(result_payload, dict):
            result_payload = {}
        tool_event = {"type": "tool_end", "tool": name, "result": result_payload}
        append_jsonl("last-tools.jsonl", tool_event)
        tool_events.append(tool_event)
    return tool_events


def fallback_answer(
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


def record_answer(
    *,
    input_text: str,
    prompt: str,
    answer: str,
    follow_up: bool,
    tools: list[dict[str, Any]],
    json_output: bool,
) -> None:
    append_event(
        {
            "type": "answer",
            "input": input_text,
            "prompt": prompt,
            "answer": answer,
            "runtime": "zeta",
        }
    )
    append_jsonl(
        ANSWER_TRANSCRIPT,
        {
            "role": "assistant",
            "content": answer,
            "input": input_text,
            "prompt": prompt,
            "follow_up": follow_up,
            "runtime": "zeta",
        },
    )
    if json_output:
        print(
            json.dumps(
                {
                    "question": input_text,
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
    print()
    print(answer)


def append_zeta_event(event_type: str, **fields: Any) -> dict[str, Any]:
    return append_jsonl(runtime.TRANSCRIPT, {"type": event_type, **fields})
