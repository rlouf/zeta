"""Internal Zeta step command for shell bindings."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TextIO

import click

from .. import handoff as sigil_handoff
from ..display import shell_result_summary
from ..protocols import SHELL_HANDOFF_RESULT_SCHEMA
from ..routes.zeta_step import run_agent_step
from ._base import cli

CONTINUE_OBJECTIVE = (
    "Continue the active Zeta step. Read the latest "
    f"{SHELL_HANDOFF_RESULT_SCHEMA} timeline event as the source of truth for "
    "what the user ran after the last shell handoff. If the outcome is "
    "cancelled, do not assume the proposed command ran; continue from the "
    "recorded shell_turns and explain the cancellation plainly if it matters. "
    "If no relevant shell turn appears, ask for the command result instead of "
    "inventing it."
)


@cli.command("zeta-step", hidden=True)
@click.option(
    "--glyph", default=",,", show_default=True, help="Glyph route the step runs as."
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
def cmd_zeta_step(
    glyph: str,
    handoff_file: Path | None,
    continue_step: bool,
    objective_parts: tuple[str, ...],
) -> int:
    """Run one Python-owned Zeta loop for shell bindings."""
    objective = " ".join(objective_parts)
    if continue_step:
        render_shell_result(sigil_handoff.append_shell_result(), output=sys.stderr)
        if not objective:
            objective = CONTINUE_OBJECTIVE
    return run_agent_step(
        objective,
        glyph=glyph,
        handoff_path=handoff_file,
        handoff_output="summary",
        trace_output=sys.stderr,
    )


def render_shell_result(
    event: dict[str, object],
    *,
    output: TextIO = sys.stdout,
) -> None:
    for line in shell_result_summary(event):
        print(line, file=output)
