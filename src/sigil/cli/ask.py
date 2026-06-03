"""The `ask` verb: answer a shell question, optionally continuing the prior one."""

from __future__ import annotations

import click

from ._base import cli
from ._shared import piped_stdin_text, question_with_stdin
from ..answers import (
    ZETA_ANSWER_TOOLS,
    ZETA_ANSWER_TOOLS_WITH_WEB,
    ask,
    continuation_prompt,
    discussion_turns,
)


@cli.command("ask")
@click.argument("question", required=False)
@click.option("--follow-up", is_flag=True)
@click.option("--json", "json_output", is_flag=True)
def cmd_ask(question: str | None, follow_up: bool, json_output: bool) -> int:
    """Answer a shell question, optionally continuing the prior answer."""
    stdin_text = piped_stdin_text()
    if follow_up:
        prompt = question_with_stdin(question or "", stdin_text or "")
        prompt = continuation_prompt(prompt, discussion_turns())
        return ask(
            prompt,
            glyph="ask",
            tools=ZETA_ANSWER_TOOLS_WITH_WEB,
            use_web=True,
            append_transcript=True,
            json_output=json_output,
        )
    if stdin_text is not None:
        prompt = question_with_stdin(question or "", stdin_text)
        return ask(
            prompt,
            glyph="ask",
            tools=ZETA_ANSWER_TOOLS,
            use_web=False,
            json_output=json_output,
        )
    if question is None:
        raise click.UsageError("QUESTION is required unless stdin is piped.")
    return ask(
        question,
        glyph="ask",
        tools=ZETA_ANSWER_TOOLS,
        use_web=False,
        json_output=json_output,
    )
