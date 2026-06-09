"""The `ask` verb: answer a shell question, optionally continuing the prior one."""

from __future__ import annotations

import click

from ._base import cli
from ._shared import piped_stdin_text, question_with_stdin
from ..routes.ask import (
    ZETA_ANSWER_TOOLS,
    ask,
    discussion_turns,
)

DEFAULT_QUESTION = "Inspect and summarize the current shell context."


@cli.command("ask")
@click.argument("question", required=False)
@click.option("--follow-up", is_flag=True)
@click.option("--json", "json_output", is_flag=True)
def cmd_ask(question: str | None, follow_up: bool, json_output: bool) -> int:
    """Answer a shell question, optionally continuing the prior answer."""
    stdin_text = piped_stdin_text()
    if follow_up:
        prompt = question_with_stdin(question or "", stdin_text or "")
        history = discussion_turns()
        return ask(
            prompt,
            glyph="ask",
            tools=ZETA_ANSWER_TOOLS,
            follow_up=True,
            json_output=json_output,
            history=history,
        )
    if stdin_text is not None:
        prompt = question_with_stdin(question or "", stdin_text)
        return ask(
            prompt,
            glyph="ask",
            tools=ZETA_ANSWER_TOOLS,
            json_output=json_output,
        )
    return ask(
        question or DEFAULT_QUESTION,
        glyph="ask",
        tools=ZETA_ANSWER_TOOLS,
        json_output=json_output,
    )
