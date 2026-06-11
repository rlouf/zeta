"""Ask and step commands for shell bindings."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TextIO

import click

from .. import handoff as sigil_handoff
from ..display.summarize import shell_result_summary
from ..protocols import SHELL_HANDOFF_RESULT_SCHEMA
from ..workflows.ask import ask
from ..workflows.do import do
from ..workflows.propose import propose
from ._base import cli
from ._shared import piped_stdin_text, question_with_stdin

DEFAULT_QUESTION = "Inspect and summarize the current shell context."

CONTINUE_OBJECTIVE = (
    "Continue the active agent step. Read the latest "
    f"{SHELL_HANDOFF_RESULT_SCHEMA} timeline event as the source of truth for "
    "what the user ran after the last shell handoff. If the outcome is "
    "cancelled, do not assume the proposed command ran; continue from the "
    "recorded shell_turns and explain the cancellation plainly if it matters. "
    "If no relevant shell turn appears, ask for the command result instead of "
    "inventing it."
)


@cli.command("ask")
@click.argument("question", required=False)
def cmd_ask(question: str | None) -> int:
    """Ask a shell question; the session timeline carries the conversation."""
    stdin_text = piped_stdin_text()
    if stdin_text is not None:
        prompt = question_with_stdin(question or "", stdin_text)
    else:
        prompt = question or DEFAULT_QUESTION
    return ask(prompt)


@cli.command("step", hidden=True)
@click.option(
    "--workflow",
    type=click.Choice(["ask", "propose", "do"]),
    default="propose",
    show_default=True,
    help="Workflow the step runs as.",
)
@click.option(
    "--handoff-file",
    type=click.Path(path_type=Path, dir_okay=False),
    help="File that receives the staged shell handoff as JSON.",
)
@click.option(
    "--continue",
    "continue_step",
    is_flag=True,
    help="Resume the pending handoff with recorded shell results.",
)
@click.argument("objective_parts", nargs=-1)
def cmd_step(
    workflow: str,
    handoff_file: Path | None,
    continue_step: bool,
    objective_parts: tuple[str, ...],
) -> int:
    """Run one Python-owned agent loop for shell bindings."""
    objective = " ".join(objective_parts)
    if continue_step:
        if workflow == "ask":
            raise click.UsageError("--continue is only valid for propose/do workflows")
        render_shell_result(sigil_handoff.append_shell_result(), output=sys.stderr)
        if not objective:
            objective = CONTINUE_OBJECTIVE
    match workflow:
        case "ask":
            return ask(objective)
        case "do":
            return do(
                objective,
                handoff_path=handoff_file,
                handoff_output="summary",
                trace_output=sys.stderr,
            )
        case "propose":
            return propose(
                objective,
                handoff_path=handoff_file,
                handoff_output="summary",
                trace_output=sys.stderr,
            )
        case _:
            raise click.UsageError("--workflow must be ask, propose, or do")


def render_shell_result(
    event: dict[str, object],
    *,
    output: TextIO = sys.stdout,
) -> None:
    for line in shell_result_summary(event):
        print(line, file=output)
