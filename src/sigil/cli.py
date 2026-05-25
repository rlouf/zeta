"""Command-line boundary between shell bindings and the Sigil runtime.

The CLI is intentionally boring: shell integrations should call these commands
instead of reimplementing model calls, selectors, rendering, or state handling.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Literal, cast

import click

from .failure import record_failure
from .install import (
    SUPPORTED_SHELLS,
    checks_exit_code,
    checks_summary,
    checks_to_json,
    doctor_checks,
    install_shell,
)
from .operators import create_invocation, run_invocation
from .patches import (
    apply_patch,
    check_patch,
    last_patch,
    record_patch_apply,
    record_patch_check,
)
from .policy import ExecutionPolicy
from .pi_stream import stream_events
from .session import (
    clear_current_session,
    current_session_snapshot,
    event_lineage,
    known_sessions,
    session_paths,
)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli() -> None:
    pass


@cli.command("op", hidden=True)
@click.argument("glyph")
@click.argument("prompt_parts", nargs=-1)
@click.option("--json", "json_output", is_flag=True)
@click.option("--dry-run", is_flag=True, help="Classify output and skip execution.")
@click.option(
    "--yes", is_flag=True, help="Acknowledge higher-autonomy execution gates."
)
@click.option(
    "--policy",
    "policy_name",
    type=click.Choice(["preview", "allow"]),
    default="preview",
    show_default=True,
    help="Execution policy for depth-3 operators.",
)
def cmd_op(
    glyph: str,
    prompt_parts: tuple[str, ...],
    json_output: bool,
    dry_run: bool,
    yes: bool,
    policy_name: str,
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

    try:
        result = run_invocation(
            invocation,
            policy=ExecutionPolicy(
                yes=yes,
                dry_run=dry_run,
                policy=cast(Literal["preview", "allow"], policy_name),
            ),
        )
    except RuntimeError as exc:
        print(f"sigil op: {exc}", file=sys.stderr)
        return 1
    if result.decision.status != "preview" or invocation.depth >= 3:
        print(f"sigil op: {result.decision.message}", file=sys.stderr)
    if result.output:
        print(result.output)
    if result.decision.status == "blocked":
        raise click.exceptions.Exit(2)
    return 0


@cli.command("install")
@click.argument("shell", type=click.Choice(SUPPORTED_SHELLS))
@click.option(
    "--install-dir",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    help="Directory where the shell binding should be installed.",
)
@click.option(
    "--rc",
    "rc_path",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Shell rc file to update.",
)
@click.option("--json", "json_output", is_flag=True)
def cmd_install_shell(
    shell: str,
    install_dir: Path | None,
    rc_path: Path | None,
    json_output: bool,
) -> int:
    """Install or update a Sigil shell binding."""
    result = install_shell(shell, install_dir=install_dir, rc_path=rc_path)
    if json_output:
        pretty_print_json(
            {
                "shell": result.shell,
                "binding_path": result.binding_path,
                "rc_path": result.rc_path,
                "source_path": result.source_path,
                "wrote_rc": result.wrote_rc,
            }
        )
        return 0

    print(f"installed Sigil {shell} binding at {result.binding_path}")
    if result.wrote_rc:
        print(f"updated {result.rc_path}")
    else:
        print(f"{result.rc_path} already sources Sigil")
    print(f"restart your shell or run: source {result.rc_path}")
    return 0


@cli.command("doctor")
@click.option(
    "--shell",
    "shell_name",
    type=click.Choice(("auto", *SUPPORTED_SHELLS)),
    default="auto",
    show_default=True,
    help="Shell binding to diagnose.",
)
@click.option("--json", "json_output", is_flag=True)
def cmd_doctor(shell_name: str, json_output: bool) -> int:
    """Check whether Sigil is installed and ready to use."""
    checks = doctor_checks(shell=shell_name)
    if json_output:
        print(checks_to_json(checks))
        return checks_exit_code(checks)

    for check in checks:
        line = f"{check.status:4} {check.name} - {check.detail}"
        print(line)
        if check.hint and check.status != "ok":
            print(f"     hint: {check.hint}")
    summary = checks_summary(checks)
    print(f"{summary['ok']} ok, {summary['warn']} warnings, {summary['fail']} failures")
    return checks_exit_code(checks)


@cli.command("render-pi-stream", hidden=True)
@click.option("--json", "json_output", is_flag=True)
def cmd_render_pi_stream(json_output: bool) -> int:
    """Render Pi's JSON event stream for the question pipeline."""
    return stream_events(json_output=json_output)


@cli.command("patch")
@click.argument(
    "patch_command",
    required=False,
    default="show",
    type=click.Choice(["show", "check", "apply"]),
)
@click.option("--json", "json_output", is_flag=True)
@click.option("--yes", is_flag=True, help="Apply the stored patch preview.")
def cmd_patch(patch_command: str, json_output: bool, yes: bool) -> int:
    """Inspect, validate, or explicitly apply the latest patch preview."""
    try:
        record = last_patch()
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    if patch_command == "show":
        if json_output:
            pretty_print_json(record)
        else:
            print(
                str(record["patch"]),
                end="" if str(record["patch"]).endswith("\n") else "\n",
            )
        return 0

    if patch_command == "check":
        result = check_patch(record)
        record_patch_check(record, result)
        if json_output:
            pretty_print_json(result.to_dict())
        elif result.ok:
            print("patch applies cleanly")
        else:
            print(result.stderr or "patch check failed", file=sys.stderr, end="")
        if not result.ok:
            raise click.exceptions.Exit(result.status or 1)
        return 0

    if not yes:
        print(
            "sigil patch apply: pass --yes to apply the stored patch", file=sys.stderr
        )
        raise click.exceptions.Exit(2)
    result = apply_patch(record)
    record_patch_apply(record, result)
    if json_output:
        pretty_print_json(result.to_dict())
    elif result.ok:
        print("patch applied")
    else:
        print(result.stderr or "patch apply failed", file=sys.stderr, end="")
    if not result.ok:
        raise click.exceptions.Exit(result.status or 1)
    return 0


def pretty_print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def print_json_line(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False))


@cli.group("events")
def cmd_events() -> None:
    """Inspect Sigil's read-only event log."""


@cmd_events.command("lineage")
@click.argument("event_id", required=False)
@click.option("--json", "json_output", is_flag=True)
def cmd_events_lineage(event_id: str | None, json_output: bool) -> int:
    """Show the provenance chain for an event."""
    lineage = event_lineage(event_id)
    if json_output:
        pretty_print_json(lineage)
        return 0 if lineage["nodes"] else 1

    if not lineage["nodes"]:
        print(
            "no events recorded" if event_id is None else f"event not found: {event_id}"
        )
        return 1
    for node in lineage["nodes"]:
        event = node["event"]
        indent = "  " * int(node["depth"])
        event_type = event.get("type", "event")
        glyph = event.get("glyph", "?")
        integrity = event.get("integrity", "unknown")
        capability = event.get("capability", "none")
        taint = ",".join(event.get("taint", [])) or "none"
        inputs = ",".join(event.get("inputs", [])) or "-"
        print(
            f"{indent}{node['id']} {event_type} "
            f"{glyph} {integrity}/{capability} taint={taint} inputs={inputs}"
        )
    for missing in lineage["missing_inputs"]:
        print(f"missing input: {missing}")
    return 0


@cli.command("session")
@click.argument(
    "session_command",
    required=False,
    default="show",
    type=click.Choice(["show", "path", "list", "clear"]),
)
@click.option("--json", "json_output", is_flag=True)
def cmd_session(session_command: str, json_output: bool) -> int:
    """Inspect or clear the current shell session state."""
    if session_command == "path":
        paths = session_paths()
        if json_output:
            pretty_print_json(paths)
        else:
            print(paths["session"])
        return 0
    if session_command == "list":
        sessions = known_sessions()
        if json_output:
            pretty_print_json(sessions)
        else:
            for session in sessions:
                print(f"{session['session_id']}\t{session['path']}")
        return 0
    if session_command == "clear":
        removed = clear_current_session()
        if json_output:
            pretty_print_json({"removed": removed})
        else:
            if removed:
                for path in removed:
                    print(f"removed {path}")
            else:
                print("session already clear")
        return 0

    snapshot = current_session_snapshot()
    if json_output:
        pretty_print_json(snapshot)
    else:
        print(f"session {snapshot['session_id']}")
        print(snapshot["path"])
        for name, value in snapshot["files"].items():
            if value is None:
                continue
            if isinstance(value, list):
                print(f"{name}: {len(value)} entries")
            elif isinstance(value, dict):
                print(f"{name}: {len(value)} keys")
            else:
                print(f"{name}: present")
    return 0


@cli.command("record-failure", hidden=True)
@click.option("--status", type=int, required=True)
@click.option("--cwd")
@click.option("--stdout-snippet", default="")
@click.option("--stderr-snippet", default="")
@click.argument("command")
def cmd_record_failure(
    command: str,
    status: int,
    cwd: str | None,
    stdout_snippet: str,
    stderr_snippet: str,
) -> int:
    """Record a failed shell command for later repair."""
    record_failure(command, status, cwd, stdout_snippet, stderr_snippet)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse the shell-agnostic Sigil CLI surface."""
    try:
        result = cli.main(args=argv, prog_name="sigil", standalone_mode=False)
    except click.ClickException as error:
        error.show()
        return error.exit_code
    except click.Abort:
        click.echo("Aborted!", err=True)
        return 1
    except click.exceptions.Exit as error:
        return int(error.exit_code)
    except FileNotFoundError as error:
        program = error.filename or "required executable"
        click.echo(f"sigil: missing executable: {program}", err=True)
        click.echo("Install it or make sure it is on PATH, then retry.", err=True)
        return 127
    except PermissionError as error:
        target = error.filename or "requested path"
        click.echo(f"sigil: permission denied: {target}", err=True)
        click.echo(
            "Check the path permissions or set SIGIL_STATE_DIR to a writable directory.",
            err=True,
        )
        return 1
    return int(result or 0)


if __name__ == "__main__":
    raise SystemExit(main())
