"""Command-line boundary between shell bindings and the Sigil runtime.

The CLI is intentionally boring: shell integrations should call these commands
instead of reimplementing model calls, selectors, rendering, or state handling.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from .commands import generate, select
from .failure import record_failure, select_fix
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
from .question import ask
from .session import (
    clear_current_session,
    current_session_snapshot,
    event_lineage,
    known_sessions,
    session_paths,
)
from .state import append_event

MAX_CONFIRM_STDIN_CHARS = 4000
MAX_CONFIRM_STDIN_LINES = 80


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli() -> None:
    pass


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

    if json_output:
        print_json_line(invocation.to_dict())
        return 0

    if should_confirm_piped_input(invocation):
        if not confirm_piped_input(stdin_text):
            print("sigil op: piped input declined", file=sys.stderr)
            raise click.exceptions.Exit(2)

    try:
        result = run_invocation(
            invocation,
            policy=ExecutionPolicy(
                confirm_execution=should_confirm_execution(invocation)
            ),
        )
    except RuntimeError as exc:
        print(f"sigil {invocation.name}: {exc}", file=sys.stderr)
        return 1
    if result.decision.status != "preview" or (
        invocation.base == "," and invocation.depth >= 2
    ):
        print(f"sigil {invocation.name}: {result.decision.message}", file=sys.stderr)
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    if result.output:
        print(result.output)
    if result.exit_code:
        raise click.exceptions.Exit(result.exit_code)
    return 0


def piped_stdin_text() -> str | None:
    """Return piped stdin, treating empty test harness stdin as absent."""
    if sys.stdin.isatty():
        return None
    text = sys.stdin.read()
    return text if text else None


@cli.command("command")
@click.argument("prompt", required=False)
@click.option("--select", "select_candidate", is_flag=True)
@click.option("--json", "json_output", is_flag=True)
def cmd_command(
    prompt: str | None,
    select_candidate: bool,
    json_output: bool,
) -> int:
    """Generate command candidates and optionally run the selector UI."""
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

    candidates, security = generate(prompt)
    if json_output:
        print_json_line({"prompt": prompt, "commands": candidates})
        return 0
    if select_candidate:
        command = select(prompt, candidates, security)
        if command:
            append_event({"type": "command_selected", "command": command, **security})
            print(command)
        return 0
    for item in candidates:
        print(item["command"])
    return 0


@cli.command("ask")
@click.argument("question", required=False)
@click.option("--follow-up", is_flag=True)
@click.option("--json", "json_output", is_flag=True)
def cmd_ask(question: str | None, follow_up: bool, json_output: bool) -> int:
    """Answer a shell question, optionally continuing the prior answer."""
    stdin_text = piped_stdin_text()
    if stdin_text is not None:
        if follow_up:
            if not confirm_piped_input(stdin_text):
                print("sigil ask: piped input declined", file=sys.stderr)
                raise click.exceptions.Exit(2)
            prompt = question_with_stdin(question or "", stdin_text)
            return ask(prompt, follow_up=True, json_output=json_output)
        return run_stream_operator(
            "?",
            prompt=question or "",
            stdin_text=stdin_text,
            json_output=json_output,
        )
    if question is None:
        raise click.UsageError("QUESTION is required unless stdin is piped.")
    return ask(question, follow_up=follow_up, json_output=json_output)


def question_with_stdin(question: str, stdin_text: str) -> str:
    """Attach confirmed piped input to a web-authorized follow-up prompt."""
    if question:
        return f"{question}\n\nPiped input:\n{stdin_text}"
    return f"Piped input:\n{stdin_text}"


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

    if should_confirm_piped_input(invocation):
        if not confirm_piped_input(stdin_text):
            print("sigil op: piped input declined", file=sys.stderr)
            raise click.exceptions.Exit(2)

    try:
        result = run_invocation(
            invocation,
            policy=ExecutionPolicy(
                dry_run=dry_run,
                confirm_execution=should_confirm_execution(invocation),
            ),
        )
    except RuntimeError as exc:
        print(f"sigil op: {exc}", file=sys.stderr)
        return 1
    if dry_run:
        print(f"sigil op: {result.decision.message}", file=sys.stderr)
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    if result.output:
        print(result.output)
    if result.exit_code:
        raise click.exceptions.Exit(result.exit_code)
    return 0


def should_confirm_piped_input(invocation: object) -> bool:
    """Return whether a comma operator needs piped-input confirmation."""
    return (
        getattr(invocation, "base", None) == ","
        and getattr(invocation, "mode", None) == "pipeline"
        and bool(getattr(invocation, "stdin", ""))
    )


def should_confirm_execution(invocation: object) -> bool:
    """Return whether command execution needs confirmation."""
    return (
        should_confirm_piped_input(invocation) and getattr(invocation, "depth", 0) >= 2
    )


def confirm_piped_input(stdin_text: str) -> bool:
    """Show a bounded stdin preview and ask whether it may influence a command."""
    print("Sigil received piped input:", file=sys.stderr)
    print("", file=sys.stderr)
    print(stdin_preview(stdin_text), file=sys.stderr)
    print("", file=sys.stderr)
    return confirm_on_tty("Use this input? [y/N] ")


def stdin_preview(text: str) -> str:
    """Return a bounded preview of piped stdin for confirmation prompts."""
    lines = text.splitlines()
    preview_lines = lines[:MAX_CONFIRM_STDIN_LINES]
    preview = "\n".join(preview_lines)
    truncated = len(lines) > MAX_CONFIRM_STDIN_LINES
    if len(preview) > MAX_CONFIRM_STDIN_CHARS:
        preview = preview[:MAX_CONFIRM_STDIN_CHARS]
        truncated = True
    if truncated:
        preview += "\n..."
    return preview


def confirm_on_tty(prompt: str) -> bool:
    """Read a yes/no confirmation from the controlling terminal."""
    try:
        with open("/dev/tty", "r+", encoding="utf-8") as tty:
            tty.write(prompt)
            tty.flush()
            answer = tty.readline()
    except OSError:
        return False
    return answer.strip().lower() in {"y", "yes"}


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
@click.option(
    "--glyphs/--no-glyphs",
    "enable_glyphs",
    default=True,
    show_default=True,
    help="Enable punctuation aliases in the shell rc snippet.",
)
@click.option("--json", "json_output", is_flag=True)
def cmd_install_shell(
    shell: str,
    install_dir: Path | None,
    rc_path: Path | None,
    enable_glyphs: bool,
    json_output: bool,
) -> int:
    """Install or update a Sigil shell binding."""
    result = install_shell(
        shell,
        install_dir=install_dir,
        rc_path=rc_path,
        enable_glyphs=enable_glyphs,
    )
    if json_output:
        pretty_print_json(
            {
                "shell": result.shell,
                "binding_path": result.binding_path,
                "rc_path": result.rc_path,
                "source_path": result.source_path,
                "wrote_rc": result.wrote_rc,
                "glyphs_enabled": result.glyphs_enabled,
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


@cli.command("fix")
@click.argument("prompt_parts", nargs=-1)
def cmd_fix(prompt_parts: tuple[str, ...]) -> int:
    """Suggest fixes for the last recorded failed shell command."""
    stdin_text = piped_stdin_text()
    if stdin_text is not None:
        return run_stream_operator(
            "^",
            prompt=" ".join(prompt_parts),
            stdin_text=stdin_text,
        )
    if prompt_parts:
        raise click.UsageError("fix does not accept a prompt unless stdin is piped.")
    command = select_fix()
    if command:
        append_event({"type": "fix_selected", "command": command})
        print(command)
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
