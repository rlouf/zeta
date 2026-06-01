"""Question flow for read-only shell answer routes.

This module owns discussion continuity. A fresh `sigil ask` resets the session
question transcript. Glyph routes use explicit source authorization: `?` can
read local context, and `??` can also search the web.
"""

from __future__ import annotations

import sys

from .session import recent_turns_context
from .state import append_event, append_jsonl, read_jsonl, write_jsonl
from .zeta.runner import run_question_answer


QUESTION_SYSTEM_PROMPT = (
    "Answer concisely. You are responding to a quick question typed at a shell "
    "prompt. The available tools are read, grep, and ls only. Use read for "
    "files, ls for directory contents, and grep to search local text. Do not "
    "propose shell commands just to inspect files or directories; inspect them "
    "through the available tools. If a 'Recent shell activity' block appears "
    "in the user message, it already shows the last few commands. For older "
    "sessions or audit history, the read tool can access ~/.sigil/events.jsonl. "
    "Do not mutate files or execute commands."
)

ZETA_QUESTION_TOOLS = "read,grep,ls"
ZETA_QUESTION_TOOLS_WITH_WEB = ZETA_QUESTION_TOOLS


def parse_tools(tools: str) -> tuple[str, ...]:
    """Parse a comma-separated tool allowlist."""
    return tuple(tool.strip() for tool in tools.split(",") if tool.strip())


def discussion_turns() -> list[dict[str, object]]:
    """Load user/assistant turns for explicit follow-up commands."""
    return [
        turn
        for turn in read_jsonl("last-question.jsonl")
        if turn.get("role") in {"user", "assistant"} and turn.get("content")
    ]


def continuation_prompt(question: str, turns: list[dict[str, object]]) -> str:
    """Build the follow-up prompt from the prior shell discussion."""
    if not turns:
        return question
    transcript = "\n\n".join(f"{turn['role']}:\n{turn['content']}" for turn in turns)
    return "\n\n".join(
        [
            "Continue the previous shell discussion.",
            f"Transcript so far:\n{transcript}",
            f"Follow-up question:\n{question}",
        ]
    )


def prepend_recent_turns(question: str) -> str:
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
        return question
    sections.append(f"Question:\n{question}")
    return "\n\n".join(sections)


RECENT_QUESTION_TURNS_LIMIT = 4
RECENT_QUESTION_TURN_CHARS = 500


def recent_question_context(
    limit: int = RECENT_QUESTION_TURNS_LIMIT,
    per_turn_chars: int = RECENT_QUESTION_TURN_CHARS,
) -> str:
    """Return a compact summary of the most recent ? / ?? exchange, if any."""
    turns = discussion_turns()
    if not turns:
        return ""
    tail = turns[-limit:]
    lines = ["Recent question transcript:"]
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
    glyph: str = "?",
    tools: str = ZETA_QUESTION_TOOLS,
    use_web: bool = False,
    append_transcript: bool = False,
    json_output: bool = False,
) -> int:
    """Run Zeta for a question while recording transcript state."""
    prompt = question if append_transcript else prepend_recent_turns(question)
    question_event = append_event(
        {
            "type": "question",
            "question": question,
            "prompt": prompt,
            "follow_up": append_transcript,
            "glyph": glyph,
        }
    )
    question_turn = {
        "role": "user",
        "content": question,
        "prompt": prompt,
        "follow_up": append_transcript,
        "event_id": question_event["id"],
        "glyph": glyph,
    }
    if append_transcript:
        append_jsonl("last-question.jsonl", question_turn)
    else:
        write_jsonl("last-question.jsonl", [question_turn])
    write_jsonl("last-tools.jsonl", [])
    enabled_tools = parse_tools(tools)
    tool_note = "+".join(enabled_tools) if enabled_tools else "no tools"
    if use_web:
        prompt = (
            f"{prompt}\n\nNote: this Zeta v1 route has no web_search tool; "
            "answer from available local/context knowledge and say when current "
            "web verification would be needed."
        )
    print(f"❯ zeta {glyph:<5} · {tool_note} · no execute path", file=sys.stderr)
    return run_question_answer(
        QUESTION_SYSTEM_PROMPT,
        prompt,
        question=question,
        follow_up=append_transcript,
        json_output=json_output,
        allowed_tools=enabled_tools,
    )
