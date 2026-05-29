"""Question flow for read-only shell answer routes.

This module owns discussion continuity. A fresh `sigil ask` resets the session
question transcript. Glyph routes use explicit source authorization: `?` can
read local context, and `??` can also search the web.
"""

from __future__ import annotations

import sys

from .ansi import MUTED, RESET
from .security import create_trust_metadata
from .model import ensure_model_for_pi
from .pi_stream import pi_trust_env, run_pi_pipeline
from .session import recent_turns_context
from .state import append_event, append_jsonl, read_jsonl, write_jsonl


QUESTION_SYSTEM_PROMPT = (
    "Answer concisely. You are responding to a quick question typed at a shell "
    "prompt. Use at most one tool call total. If one tool call is not enough, "
    "answer with the best available uncertainty and say what single follow-up "
    "would help. If a 'Recent shell activity' block appears in the user "
    "message, it already shows the last few commands. For older sessions or "
    "audit history, the read tool can access ~/.sigil/events.jsonl. If a "
    "shell command would help, answer with the command text instead of calling "
    "a tool for it."
)

PI_QUESTION_TOOLS = "read,grep,find,ls"
PI_QUESTION_TOOLS_WITH_WEB = f"{PI_QUESTION_TOOLS},web_search"


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
    stream_filter: str | None = None,
    *,
    glyph: str = "?",
    tools: str = PI_QUESTION_TOOLS,
    use_web: bool = False,
    append_transcript: bool = False,
    json_output: bool = False,
) -> int:
    """Run Pi for a question while recording transcript and tool trace state."""
    if not ensure_model_for_pi():
        return 1

    prompt = question if append_transcript else prepend_recent_turns(question)
    security = create_trust_metadata(
        glyph=glyph,
        mode="read-only",
        labels=["network"] if use_web else [],
    )
    question_event = append_event(
        {
            "type": "question",
            "question": question,
            "prompt": prompt,
            "follow_up": append_transcript,
            **security,
        }
    )
    question_turn = {
        "role": "user",
        "content": question,
        "prompt": prompt,
        "follow_up": append_transcript,
        "event_id": question_event["id"],
        **security,
    }
    if append_transcript:
        append_jsonl("last-question.jsonl", question_turn)
    else:
        write_jsonl("last-question.jsonl", [question_turn])
    write_jsonl("last-tools.jsonl", [])
    tool_label = "read+search+web" if use_web else "read+search"
    print(
        f"{MUTED}❯ pi {glyph:<5} · {tool_label} · no execute path{RESET}",
        file=sys.stderr,
    )

    pi_cmd = [
        "pi",
        "-p",
        "--mode",
        "json",
        "--no-session",
        "--tools",
        tools,
    ]
    pi_cmd.extend(
        [
            "--append-system-prompt",
            QUESTION_SYSTEM_PROMPT,
            prompt,
        ]
    )
    filter_env = pi_trust_env(
        security,
        question=question,
        prompt=prompt,
        follow_up="1" if append_transcript else "0",
        inputs=question_event["id"] or ",".join(security["inputs"]),
    )
    exit_code = run_pi_pipeline(
        pi_cmd,
        env=filter_env,
        json_output=json_output,
        stream_filter=stream_filter,
    )
    if not json_output:
        print()
    return exit_code
