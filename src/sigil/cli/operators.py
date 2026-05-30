"""The `op` command and the operator dispatch + stream runtime."""

from __future__ import annotations

import sys

import click

from ._base import cli
from ._shared import (
    confirm_piped_input,
    print_json_line,
    question_with_stdin,
    should_confirm_piped_input,
    should_run_act_operator,
)
from ..acts import run_act_stepper
from ..goals import run_goal_loop
from ..operators import OperatorInvocation, create_invocation, run_invocation
from ..policy import ExecutionPolicy
from ..question import PI_QUESTION_TOOLS, PI_QUESTION_TOOLS_WITH_WEB, ask


def run_stream_operator(
    glyph: str,
    *,
    prompt: str = "",
    stdin_text: str,
    json_output: bool = False,
) -> int:
    """Run the stream operator runtime behind a verb command."""
    try:
        invocation = create_invocation(
            glyph,
            prompt=prompt,
            stdin=stdin_text,
            mode="pipeline",
        )
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="glyph") from exc

    if invocation.base == "?":
        if json_output:
            print_json_line(invocation.to_dict())
            return 0
        return run_question_operator(invocation)

    if not json_output and should_confirm_piped_input(invocation):
        if not confirm_piped_input(stdin_text):
            print("sigil command: piped input declined", file=sys.stderr)
            raise click.exceptions.Exit(2)

    try:
        result = run_invocation(
            invocation,
            policy=ExecutionPolicy(),
        )
    except RuntimeError as exc:
        print(f"sigil {invocation.name}: {exc}", file=sys.stderr)
        return 1
    if json_output:
        print_json_line(
            {
                "prompt": prompt,
                "command": result.command,
                "labels": list(result.decision.classification.labels),
                "explanation": result.explanation,
            }
        )
        return 0
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    if result.output:
        print(result.output, end="" if result.output.endswith("\n") else "\n")
    if result.exit_code:
        raise click.exceptions.Exit(result.exit_code)
    return 0


@cli.command("op", hidden=True)
@click.argument("glyph")
@click.argument("prompt_parts", nargs=-1)
@click.option("--json", "json_output", is_flag=True)
@click.option("--dry-run", is_flag=True, help="Classify output and skip execution.")
def cmd_op(
    glyph: str,
    prompt_parts: tuple[str, ...],
    json_output: bool,
    dry_run: bool,
) -> int:
    """Parse a semantic operator invocation."""
    stdin_is_tty = sys.stdin.isatty()
    stdin_text = "" if stdin_is_tty else sys.stdin.read()
    prompt = " ".join(prompt_parts)
    mode = "interactive" if stdin_is_tty else "pipeline"
    try:
        invocation = create_invocation(
            glyph,
            prompt=prompt,
            stdin=stdin_text,
            mode=mode,
        )
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="glyph") from exc

    if json_output:
        print_json_line(invocation.to_dict())
        return 0

    if should_run_act_operator(invocation):
        return dispatch_act_operator(invocation, prompt, stdin_text, dry_run=dry_run)

    if should_confirm_piped_input(invocation):
        if not confirm_piped_input(stdin_text):
            print("sigil op: piped input declined", file=sys.stderr)
            raise click.exceptions.Exit(2)

    if invocation.base == "?":
        return dispatch_question_operator(invocation, dry_run=dry_run)

    if invocation.base == "@":
        return dispatch_goal_operator(invocation, prompt, stdin_text, dry_run=dry_run)

    return dispatch_default_operator(invocation, dry_run=dry_run)


def dispatch_act_operator(
    invocation: OperatorInvocation,
    prompt: str,
    stdin_text: str,
    *,
    dry_run: bool,
) -> int:
    """Run a `,,`/`,,,` invocation through the Pi act stepper."""
    if dry_run:
        status = run_act_stepper(
            objective=prompt,
            stdin_text=stdin_text,
            confirm_step=invocation.depth == 2,
            glyph=invocation.glyph,
            dry_run=True,
        )
        if status:
            raise click.exceptions.Exit(status)
        return 0
    if should_confirm_piped_input(invocation):
        if not confirm_piped_input(stdin_text):
            print("sigil op: piped input declined", file=sys.stderr)
            raise click.exceptions.Exit(2)
    try:
        status = run_act_stepper(
            objective=prompt,
            stdin_text=stdin_text,
            confirm_step=invocation.depth == 2,
            glyph=invocation.glyph,
        )
    except RuntimeError as exc:
        print(f"sigil op: {exc}", file=sys.stderr)
        return 1
    if status:
        raise click.exceptions.Exit(status)
    return 0


def dispatch_question_operator(invocation: OperatorInvocation, *, dry_run: bool) -> int:
    """Run a `?`/`??` invocation through the question route."""
    if dry_run:
        tools = "read+search+web" if invocation.depth == 2 else "read+search"
        print(
            f"sigil op: {invocation.glyph} dry-run: would call {tools} question route",
            file=sys.stderr,
        )
        return 0
    return run_question_operator(invocation)


def dispatch_goal_operator(
    invocation: OperatorInvocation,
    prompt: str,
    stdin_text: str,
    *,
    dry_run: bool,
) -> int:
    """Run an `@`/`@@` invocation through the goal loop."""
    status = run_goal_loop(
        objective=prompt,
        stdin_text=stdin_text,
        confirm_steps=invocation.depth == 1,
        glyph=invocation.glyph,
        dry_run=dry_run,
    )
    if status:
        raise click.exceptions.Exit(status)
    return 0


def dispatch_default_operator(invocation: OperatorInvocation, *, dry_run: bool) -> int:
    """Run a `,`/`?` stdout-only invocation through the operator runtime."""
    try:
        result = run_invocation(
            invocation,
            policy=ExecutionPolicy(
                dry_run=dry_run,
            ),
        )
    except RuntimeError as exc:
        print(f"sigil op: {exc}", file=sys.stderr)
        raise click.exceptions.Exit(1) from exc
    if dry_run:
        print(f"sigil op: {result.decision.message}", file=sys.stderr)
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    if result.output:
        print(result.output, end="" if result.output.endswith("\n") else "\n")
    if result.exit_code:
        raise click.exceptions.Exit(result.exit_code)
    return 0


def run_question_operator(invocation: object) -> int:
    """Run question glyphs through explicitly authorized answer routes."""
    question = str(getattr(invocation, "prompt", "") or "")
    stdin_text = str(getattr(invocation, "stdin", "") or "")
    depth = int(getattr(invocation, "depth", 0) or 0)
    glyph = str(getattr(invocation, "glyph", "?") or "?")
    if stdin_text:
        question = question_with_stdin(question, stdin_text)
    if not question:
        question = "Answer the current shell question."
    use_web = depth == 2
    return ask(
        question,
        glyph=glyph,
        tools=PI_QUESTION_TOOLS_WITH_WEB if use_web else PI_QUESTION_TOOLS,
        use_web=use_web,
    )
