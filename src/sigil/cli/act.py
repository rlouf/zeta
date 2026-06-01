"""The `act` command: inspect, resume, or abort the current Zeta edit action."""

from __future__ import annotations

import click

from ._base import cli
from ._shared import pretty_print_json
from ..acts import abort_active_act, last_act, print_act, run_act_stepper


@cli.command("act")
@click.argument(
    "act_command",
    required=False,
    default="show",
    type=click.Choice(["show", "resume", "abort"]),
)
@click.option("--json", "json_output", is_flag=True)
def cmd_act(act_command: str, json_output: bool) -> int:
    """Inspect, resume, or abort the current Zeta edit action."""
    return run_act_command(act_command, json_output)


def run_act_command(act_command: str, json_output: bool) -> int:
    """Run the act control subcommands."""
    if act_command == "resume":
        return run_act_stepper(
            objective="",
            confirm_step=True,
            glyph=",,",
        )
    if act_command == "abort":
        act = abort_active_act()
        if json_output:
            pretty_print_json({"aborted": bool(act), "act": act})
        elif act is None:
            print("no active act")
        else:
            print(f"aborted act {act.get('act_id')}")
        return 0

    act = last_act()
    if json_output:
        pretty_print_json(act)
    elif act is None:
        print("no act recorded")
    else:
        print_act(act)
    return 0
