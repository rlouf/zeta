"""Zeta service CLI.

This CLI provides service commands to a shell-owned control loop. It is not the
interactive agent runtime; the shell binding owns control flow.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import click

from . import runtime as zeta


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli() -> None:
    """Zeta service commands."""


def main(argv: list[str] | None = None) -> int:
    try:
        result = cli.main(args=argv, prog_name="zeta", standalone_mode=False)
    except click.ClickException as error:
        error.show()
        return error.exit_code
    except click.Abort:
        click.echo("Aborted!", err=True)
        return 1
    except click.exceptions.Exit as error:
        return int(error.exit_code)
    return int(result or 0)


@cli.group("tools")
def tools_group() -> None:
    """Tool registry services."""


@tools_group.command("list")
@click.option("--json", "json_output", is_flag=True, required=False)
def tools_list(json_output: bool) -> int:
    if not json_output:
        for tool in zeta.tools_list()["tools"]:
            print(tool["name"])
        return 0
    print_json(zeta.tools_list())
    return 0


@cli.group("tool")
def tool_group() -> None:
    """Built-in tool services."""


def tool_command(name: str) -> click.Command:
    @click.command(
        name,
        context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
    )
    @click.option("--json", "json_meta", is_flag=True)
    @click.option("--schema", is_flag=True)
    @click.option("--analyze", is_flag=True)
    def command(json_meta: bool, schema: bool, analyze: bool) -> int:
        return run_tool_command(
            name, json_meta=json_meta, schema=schema, analyze=analyze
        )

    return command


for _name in sorted(zeta.TOOL_SPECS):
    tool_group.add_command(tool_command(_name))


def run_tool_command(
    name: str,
    *,
    json_meta: bool,
    schema: bool,
    analyze: bool,
) -> int:
    if json_meta:
        print_json(zeta.tool_metadata(name))
        return 0
    if schema:
        print_json(zeta.tool_metadata(name)["schema"])
        return 0
    try:
        params = zeta.read_json_stdin(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        raise click.BadParameter(str(exc), param_hint="stdin") from exc
    if analyze:
        print_json(zeta.analyze_tool(name, params))
        return 0
    print_json(zeta.run_tool(name, params))
    return 0


@cli.group("model")
def model_group() -> None:
    """Model transport services."""


@model_group.command("stream")
def model_stream() -> int:
    try:
        request = zeta.read_json_stdin(sys.stdin)
        for event in zeta.stream_model_events(request):
            print_json_line(event)
    except RuntimeError as exc:
        print_json_line({"type": "error", "message": str(exc)})
        return 1
    return 0


@cli.group("transcript")
def transcript_group() -> None:
    """Transcript storage services."""


@transcript_group.command("append")
def transcript_append() -> int:
    try:
        event = zeta.read_json_stdin(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        raise click.BadParameter(str(exc), param_hint="stdin") from exc
    print_json(zeta.append_transcript(event))
    return 0


@transcript_group.command("shell-result")
def transcript_shell_result() -> int:
    print_json(zeta.append_shell_result())
    return 0


@transcript_group.command("tail")
@click.option("--limit", default=zeta.DEFAULT_TAIL_LIMIT, show_default=True)
def transcript_tail(limit: int) -> int:
    print_json({"events": zeta.transcript_tail(limit)})
    return 0


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def print_json_line(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    raise SystemExit(main())
