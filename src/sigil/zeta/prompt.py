"""System prompt construction for Zeta."""

from __future__ import annotations

import json
from typing import Iterable

from .tools import model_tool_descriptors

BASE_SYSTEM_PROMPT = """You are Zeta, a shell-native coding agent.

You participate in the user's live shell session. The shell owns control flow,
current working directory, environment, history, job control, and command
handoff. You choose the next small action and then stop.

Work concretely from the available context. Prefer inspection before edits. Use
read-only tools for local context. Use handoff tools for commands or mutations
that the user should review or run. Keep answers concise and do not invent
command output, file contents, or tool results.

When the transcript contains a zeta.shell_handoff_result.v1 result, treat it as
the source of truth for what happened after a shell handoff. If the outcome is
cancelled, do not assume the proposed command ran; use the recorded shell_turns
as user-chosen context and explain the cancellation plainly if it matters.
"""

TOOL_PROTOCOL_PROMPT = """Tool protocol:

- Tools are ordinary commands exposed to you by the shell loop.
- Return at most one tool call per step.
- Use a tool only when its schema matches the needed action.
- Do not mention unavailable tools.
- If no tool is needed, return a final answer.
"""


def system_prompt(
    route_prompt: str | None = None,
    *,
    allowed_tools: Iterable[str] | None = None,
) -> str:
    """Build the Zeta system prompt with the active tool registry."""
    sections = [clean_prompt(route_prompt) or BASE_SYSTEM_PROMPT.strip()]
    sections.append(TOOL_PROTOCOL_PROMPT.strip())
    sections.append(tools_prompt(allowed_tools))
    return "\n\n".join(sections)


def tools_prompt(allowed_tools: Iterable[str] | None = None) -> str:
    """Render active tools from the registry into the system prompt."""
    descriptors = model_tool_descriptors(allowed_tools)
    return "\n".join(
        [
            "Available tools with input JSON Schemas:",
            json.dumps(descriptors, ensure_ascii=False, separators=(",", ":")),
        ]
    )


def clean_prompt(prompt: str | None) -> str:
    return (prompt or "").strip()
