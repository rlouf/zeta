"""Internal Zeta step command for shell bindings."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ._base import cli
from .. import handoff as sigil_handoff
from ..display import shell_result_summary
from ..zeta_runner import run_agent_step

CONTINUE_OBJECTIVE = (
    "Continue the active Zeta step. Read the latest "
    "zeta.shell_handoff_result.v1 transcript event as the source of truth for "
    "what the user ran after the last shell handoff. If the outcome is "
    "cancelled, do not assume the proposed command ran; continue from the "
    "recorded shell_turns and explain the cancellation plainly if it matters. "
    "If no relevant shell turn appears, ask for the command result instead of "
    "inventing it."
)


@cli.command("zeta-step", hidden=True)
@click.option("--glyph", default=",,", show_default=True)
@click.option(
    "--handoff-file",
    type=click.Path(path_type=Path, dir_okay=False),
)
@click.option("--continue", "continue_step", is_flag=True)
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
        render_shell_result(sigil_handoff.append_shell_result())
        if not objective:
            objective = CONTINUE_OBJECTIVE
    return run_agent_step(
        objective,
        glyph=glyph,
        handoff_path=handoff_file,
        handoff_output="summary",
        trace_output=sys.stdout,
    )


def render_shell_result(event: dict[str, object]) -> None:
    for line in shell_result_summary(event):
        print(line)
