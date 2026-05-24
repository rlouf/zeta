"""Question and follow-up flow for `?` and `??`.

This module owns discussion continuity. A fresh `?` resets the session question
transcript; `??` expands the prompt from that transcript before calling Pi.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from .ansi import MUTED, RESET
from .security import (
    inherit_security,
    inherited_label,
    make_security,
    normalize_security,
)
from .server import start_qwen_for_pi
from .state import append_event, append_jsonl, read_jsonl, write_jsonl


QUESTION_SYSTEM_PROMPT = (
    "Answer concisely. You are responding to a quick question typed at a shell prompt."
)
DEFAULT_GLOW_STYLE = "notty"
DEFAULT_GLOW_WIDTH = "88"


def renderer_command() -> list[str]:
    """Return the Markdown renderer command for interactive question answers."""
    if not shutil.which("glow"):
        return ["cat"]
    style = os.environ.get("SIGIL_GLOW_STYLE") or DEFAULT_GLOW_STYLE
    width = os.environ.get("SIGIL_GLOW_WIDTH") or DEFAULT_GLOW_WIDTH
    return ["glow", "--style", style, "--width", width, "-"]


def discussion_turns() -> list[dict[str, object]]:
    """Load user/assistant turns that should be visible to `??`."""
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


def ask(
    question: str,
    stream_filter: str | None = None,
    *,
    follow_up: bool = False,
    json_output: bool = False,
) -> int:
    """Run Pi for a question while recording transcript and tool trace state."""
    if not start_qwen_for_pi():
        return 1

    previous_turns = discussion_turns() if follow_up else []
    prompt = continuation_prompt(question, previous_turns) if follow_up else question
    if follow_up:
        input_records = [normalize_security(turn) for turn in previous_turns]
        security = inherit_security(
            glyph="??",
            input_records=input_records,
            capability="read",
            extra_taint=["web"],
            provisional=True,
        )
    else:
        security = make_security(
            glyph="?",
            integrity="web",
            capability="read",
            taint=["web"],
            provisional=True,
            fresh_human=True,
        )
    question_event = append_event(
        {
            "type": "question",
            "question": question,
            "prompt": prompt,
            "follow_up": follow_up,
            **security,
        }
    )
    question_turn = {
        "role": "user",
        "content": question,
        "prompt": prompt,
        "follow_up": follow_up,
        "event_id": question_event["id"],
        **security,
    }
    if follow_up:
        append_jsonl("last-question.jsonl", question_turn)
    else:
        write_jsonl("last-question.jsonl", [question_turn])
    write_jsonl("last-tools.jsonl", [])
    if follow_up:
        print(
            f"{MUTED}❯ pi ??    · inherited: {inherited_label(security)} · provisional{RESET}",
            file=sys.stderr,
        )
    else:
        print(f"{MUTED}❯ pi ?     · read+web · no execute path{RESET}", file=sys.stderr)

    pi_cmd = [
        "pi",
        "-p",
        "--mode",
        "json",
        "--no-session",
        "--tools",
        "read,web_search",
        "--append-system-prompt",
        QUESTION_SYSTEM_PROMPT,
        prompt,
    ]
    filter_cmd = (
        [stream_filter, "render-pi-stream"]
        if stream_filter
        else [sys.argv[0], "render-pi-stream"]
    )
    if json_output:
        filter_cmd.append("--json")
    renderer_cmd = renderer_command()
    filter_env = {
        **os.environ,
        "SIGIL_CAPTURE_ANSWER": "1",
        "SIGIL_CAPTURE_TRACE": "1",
        "SIGIL_SECURITY_GLYPH": str(security["glyph"]),
        "SIGIL_SECURITY_INTEGRITY": str(security["integrity"]),
        "SIGIL_SECURITY_CAPABILITY": str(security["capability"]),
        "SIGIL_SECURITY_TAINT": ",".join(security["taint"]),
        "SIGIL_SECURITY_PROVISIONAL": "1" if security["provisional"] else "0",
        "SIGIL_SECURITY_INPUTS": question_event["id"] or ",".join(security["inputs"]),
        "SIGIL_QUESTION": question,
        "SIGIL_PROMPT": prompt,
        "SIGIL_FOLLOW_UP": "1" if follow_up else "0",
    }

    pi_proc = subprocess.Popen(pi_cmd, stdout=subprocess.PIPE)
    filter_stdout = None if json_output else subprocess.PIPE
    filter_proc = subprocess.Popen(
        filter_cmd, stdin=pi_proc.stdout, stdout=filter_stdout, env=filter_env
    )
    assert pi_proc.stdout is not None
    pi_proc.stdout.close()
    if json_output:
        renderer_code = 0
    else:
        renderer_proc = subprocess.Popen(renderer_cmd, stdin=filter_proc.stdout)
        assert filter_proc.stdout is not None
        filter_proc.stdout.close()
        renderer_code = renderer_proc.wait()

    filter_code = filter_proc.wait()
    pi_code = pi_proc.wait()
    if not json_output:
        print()
    if pi_code:
        return pi_code
    if filter_code:
        return filter_code
    return renderer_code
