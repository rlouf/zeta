"""Ask and step commands for shell bindings."""

import sys
from pathlib import Path
from typing import TextIO

import click

from commas import handoff as commas_handoff
from commas.cli._base import cli, examples
from commas.cli._shared import compose_in_editor, piped_stdin_text, question_with_stdin
from commas.display.summarize import shell_result_summary
from commas.protocols import SHELL_HANDOFF_RESULT_SCHEMA
from commas.workflows.ask import ask
from commas.workflows.do import do
from commas.workflows.propose import propose

EDIT_GLYPHS = {"ask": ",", "propose": ",,", "do": ",,,"}

CONTINUE_OBJECTIVE = (
    "Continue the active agent step. Read the latest "
    f"{SHELL_HANDOFF_RESULT_SCHEMA} timeline event as the source of truth for "
    "what the user ran after the last shell handoff. If the outcome is "
    "cancelled, do not assume the proposed command ran; continue from the "
    "recorded shell_turns and explain the cancellation plainly if it matters. "
    "If no relevant shell turn appears, ask for the command result instead of "
    "inventing it."
)


@cli.command(
    "ask",
    epilog=examples(
        'commas ask "what changed in this repo?"',
        'git diff | commas ask "review risky changes"',
        "commas ask",
    ),
)
@click.argument("question", required=False)
def cmd_ask(question: str | None) -> int:
    """Ask a read-only question from local session context.

    The `,` glyph calls this command. The answer comes from the session's
    recorded shell context; piped stdin is used directly as context. With
    no question and no piped stdin, the question is composed in
    $VISUAL/$EDITOR. It never stages or executes commands.

    Exits 69 when the model endpoint is down or fails mid-answer;
    `commas doctor` diagnoses it.
    """
    stdin_text = piped_stdin_text()
    if stdin_text is not None:
        return ask(question_with_stdin(question or "", stdin_text))
    if not question:
        question = composed_or_abort("ask", "question")
    return ask(question)


def composed_or_abort(workflow: str, noun: str) -> str:
    """Compose the prompt for a bare glyph in $EDITOR; abort when left empty."""
    hint = (
        f"# {EDIT_GLYPHS[workflow]} ({workflow}) — compose the {noun} above.\n"
        "# Lines starting with '#' are ignored; save an empty file to abort."
    )
    text = compose_in_editor(hint=hint)
    if text is None:
        raise click.ClickException(f"aborted: empty {noun}")
    return text


@cli.command(
    "step",
    hidden=True,
    epilog=examples(
        'commas step --workflow propose "run the relevant tests"',
        "commas step --workflow propose --continue",
        'commas step --workflow do "fix the failing parser test"',
    ),
)
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
    """Run one agent loop for the shell glyph bindings.

    The `,,` and `,,,` glyphs call this command. `--workflow propose`
    stages reviewed shell work at your prompt; `--workflow do` runs
    auto-approved tool calls; `--continue` resumes the pending handoff
    with the recorded shell results. With no objective and without
    `--continue`, the objective is composed in $VISUAL/$EDITOR.

    Exits 69 when the model endpoint is down or fails mid-answer;
    `commas doctor` diagnoses it.
    """
    objective = " ".join(objective_parts)
    if continue_step:
        if workflow == "ask":
            raise click.UsageError("--continue is only valid for propose/do workflows")
        render_shell_result(commas_handoff.append_shell_result(), output=sys.stderr)
        if not objective:
            objective = CONTINUE_OBJECTIVE
    elif not objective:
        objective = composed_or_abort(
            workflow,
            "question" if workflow == "ask" else "objective",
        )
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
