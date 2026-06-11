"""The `ask` verb: ask about shell context, continuing the session timeline."""

from __future__ import annotations

import click

from ..workflows.ask import ZETA_ASK_TOOLS, ask
from ._base import cli
from ._shared import piped_stdin_text, question_with_stdin

DEFAULT_QUESTION = "Inspect and summarize the current shell context."


@cli.command("ask")
@click.argument("question", required=False)
@click.option("--json", "json_output", is_flag=True, help="Emit the answer as JSON.")
def cmd_ask(question: str | None, json_output: bool) -> int:
    """Ask a shell question; the session timeline carries the conversation."""
    stdin_text = piped_stdin_text()
    if stdin_text is not None:
        prompt = question_with_stdin(question or "", stdin_text)
    else:
        prompt = question or DEFAULT_QUESTION
    return ask(prompt, glyph="ask", tools=ZETA_ASK_TOOLS, json_output=json_output)
