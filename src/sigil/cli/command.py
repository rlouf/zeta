"""The `command` verb: generate a single command proposal."""

from __future__ import annotations

import click

from ._base import cli
from ._shared import piped_stdin_text, print_json_line
from .operators import run_stream_operator
from ..commands import generate


@cli.command("command")
@click.argument("prompt", required=False)
@click.option("--json", "json_output", is_flag=True)
def cmd_command(
    prompt: str | None,
    json_output: bool,
) -> int:
    """Generate a single command proposal."""
    stdin_text = piped_stdin_text()
    if stdin_text is not None:
        return run_stream_operator(
            ",",
            prompt=prompt or "",
            stdin_text=stdin_text,
            json_output=json_output,
        )

    if prompt is None:
        raise click.UsageError("PROMPT is required unless stdin is piped.")

    proposal, _security = generate(prompt)
    if json_output:
        print_json_line({"prompt": prompt, "command": proposal})
        return 0
    print(proposal["command"])
    return 0
