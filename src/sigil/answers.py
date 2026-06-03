"""Read-only shell answer routes.

This module owns discussion continuity. A fresh `sigil ask` resets the session
answer transcript. Comma glyphs and named ask routes use explicit source
authorization.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Iterable

from .model import ensure_server
from .session import recent_turns_context
from .state import (
    ANSWER_TRANSCRIPT,
    append_event,
    append_jsonl,
    read_jsonl,
    write_jsonl,
)
from .zeta.model import chat_text
from .zeta import runtime
from .display import render_tool_start


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


def continuation_prompt(user_input: str, turns: list[dict[str, object]]) -> str:
    """Build the follow-up prompt from the prior shell discussion."""
    if not turns:
        return user_input
    transcript = "\n\n".join(f"{turn['role']}:\n{turn['content']}" for turn in turns)
    return "\n\n".join(
        [
            "Continue the previous shell discussion.",
            f"Transcript so far:\n{transcript}",
            f"Follow-up question:\n{user_input}",
        ]
    )


def prepend_recent_turns(user_input: str) -> str:
    """Attach recent shell activity to a fresh question prompt."""
    from .failure import active_failure_context

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
) -> int:
    """Run Zeta for a shell answer while recording transcript state."""
    user_input = question
    prompt = user_input if append_transcript else prepend_recent_turns(user_input)
    request_event = append_event(
        {
            "type": ANSWER_REQUEST_EVENT,
            "input": user_input,
            "prompt": prompt,
            "follow_up": append_transcript,
            "glyph": glyph,
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
    tool_note = "+".join(enabled_tools) if enabled_tools else "no tools"
    print(f"❯ zeta {glyph:<5} · {tool_note} · no execute path", file=sys.stderr)
    return run_tool_answer(
        ANSWER_SYSTEM_PROMPT,
        prompt,
        input_text=user_input,
        follow_up=append_transcript,
        json_output=json_output,
        allowed_tools=enabled_tools,
    )


def run_text_answer(
    system: str,
    prompt: str,
    *,
    input_text: str = "",
    follow_up: bool = False,
    json_output: bool = False,
    max_tokens: int = 1200,
) -> int:
    """Run a plain Zeta model answer and persist answer state."""
    if not ensure_server():
        return 1
    answer = chat_text(system, prompt, max_tokens=max_tokens)
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


def run_tool_answer(
    system: str,
    prompt: str,
    *,
    input_text: str = "",
    follow_up: bool = False,
    json_output: bool = False,
    max_steps: int = 4,
    allowed_tools: Iterable[str] = ANSWER_TOOLS,
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
            "route": ANSWER_ROUTE,
        }
        trace = append_zeta_event(
            "tool_call", name=name, input=params, route=ANSWER_ROUTE
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
            route=ANSWER_ROUTE,
        )
        result = runtime.run_tool(name, params)
        result_event = {
            "type": "tool_result",
            "tool_call_id": trace["id"],
            "name": name,
            "result": result,
            "route": ANSWER_ROUTE,
        }
        append_zeta_event(
            "tool_result",
            tool_call_id=trace["id"],
            name=name,
            result=result,
            route=ANSWER_ROUTE,
        )
        turn_events.append(result_event)
        event = {"type": "tool_end", "tool": name, "result": result}
        append_jsonl("last-tools.jsonl", event)
        tool_events.append(event)
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
    print(answer)


def append_zeta_event(event_type: str, **fields: Any) -> dict[str, Any]:
    return append_jsonl(runtime.TRANSCRIPT, {"type": event_type, **fields})
