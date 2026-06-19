"""Read-only shell ask workflow.

Discussion continuity comes from the session timeline: every ask turn reads
the prior timeline and records its own events, so asks remember exactly what
the other glyphs remember. A new shell session starts a fresh thread.

"""

from __future__ import annotations

from agents.skills import expand_skill_directive
from sigil.agent_io import last_event_time
from sigil.sessions import active_failure_context, recent_turns_context
from sigil.workflows.step import step

ASK_SYSTEM_PROMPT = (
    "Answer concisely. You are responding to a quick question typed at a shell "
    "prompt. The available tools are read, grep, ls, query_log, and web_search "
    "only. Use "
    "read for files, ls for directory contents, file sizes, and recursive "
    "size-filtered listings, grep to search local text, web_search to find "
    "public web pages, and read to inspect known public URLs. Do not "
    "propose shell commands just to inspect files or directories; inspect them "
    "through the available tools. If a 'Recent shell activity' block appears "
    "in the user message, it already shows the last few commands. For older "
    "sessions, past delegations, or audit history, use query_log. "
    "Do not mutate files or execute commands."
)

ASK_TOOLS = ("read", "grep", "ls", "query_log", "web_search")


def prepend_recent_turns(user_input: str, *, since: float | None) -> str:
    """Attach shell activity newer than the last model response to a question.

    Older activity is already in the timeline the model sees; only the delta
    since the previous turn is news.
    """
    sections = []
    context = recent_turns_context(since=since)
    if context:
        sections.append(context)
    failure = active_failure_context(since=since)
    if failure:
        sections.append(failure)
    if not sections:
        return user_input
    sections.append(f"Question:\n{user_input}")
    return "\n\n".join(sections)


def ask(
    question: str,
    *,
    tools: tuple[str, ...] = ASK_TOOLS,
) -> int:
    """Run Zeta for a shell ask continuing the session timeline."""
    from sigil import zeta_session_for_sigil

    runtime_context = zeta_session_for_sigil()
    return step(
        question,
        workflow="ask",
        system=ASK_SYSTEM_PROMPT,
        prompt=prepend_recent_turns(
            expand_skill_directive(question),
            since=last_event_time(
                store=runtime_context.trace_store,
                run_id=runtime_context.session_id,
            ),
        ),
        allowed_tools=tools,
    )
