"""Command-line boundary between shell bindings and the Sigil runtime.

The CLI is intentionally boring: shell integrations should call these commands
instead of reimplementing model calls, selectors, rendering, or state handling.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import cast

import click

from .commands import generate
from .failure import record_failure
from .handoff import consume_latest_bash_handoff, latest_bash_handoff
from .install import (
    SUPPORTED_SHELLS,
    checks_exit_code,
    checks_summary,
    checks_to_json,
    doctor_checks,
    install_shell,
)
from .operators import create_invocation, run_invocation
from .acts import abort_active_act, last_act, print_act, run_act_stepper
from .policy import ExecutionPolicy
from .pi_stream import stream_events
from .question import ask
from .session import (
    clear_current_session,
    current_session_snapshot,
    event_lineage,
    known_sessions,
    read_event_log,
    record_turn,
    session_paths,
)
from .status import current_status, format_status
from .tty import confirm_on_tty

MAX_CONFIRM_STDIN_CHARS = 4000
MAX_CONFIRM_STDIN_LINES = 80
EVENT_LIST_COLUMNS = ("time", "id", "action", "trust", "session", "summary")


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

    if invocation.base == "?":
        return run_question_operator(invocation)

    try:
        result = run_invocation(
            invocation,
            policy=ExecutionPolicy(
                confirm_execution=should_confirm_execution(invocation),
            ),
        )
    except RuntimeError as exc:
        print(f"sigil {invocation.name}: {exc}", file=sys.stderr)
        return 1
    if result.decision.status != "preview" or (
        invocation.base == "," and invocation.depth == 2
    ):
        print(f"sigil {invocation.name}: {result.decision.message}", file=sys.stderr)
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    if result.output:
        print(result.output, end="" if result.output.endswith("\n") else "\n")
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

    proposal, security = generate(prompt)
    if json_output:
        print_json_line({"prompt": prompt, "command": proposal})
        return 0
    print(proposal["command"])
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
    """Attach piped input to a web-authorized question prompt."""
    if question:
        return f"{question}\n\nPiped input:\n{stdin_text}"
    return f"Piped input:\n{stdin_text}"


@cli.command("op", hidden=True)
@click.argument("glyph")
@click.argument("prompt_parts", nargs=-1)
@click.option("--json", "json_output", is_flag=True)
@click.option("--dry-run", is_flag=True, help="Classify output and skip execution.")
@click.option("--verbose", is_flag=True, help="Show raw Pi tool and prose output.")
def cmd_op(
    glyph: str,
    prompt_parts: tuple[str, ...],
    json_output: bool,
    dry_run: bool,
    verbose: bool,
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
        if dry_run:
            return run_act_stepper(
                objective=prompt,
                stdin_text=stdin_text,
                dry_run=True,
                verbose=verbose,
            )
        if should_confirm_piped_input(invocation):
            if not confirm_piped_input(stdin_text):
                print("sigil op: piped input declined", file=sys.stderr)
                raise click.exceptions.Exit(2)
        try:
            return run_act_stepper(
                objective=prompt,
                stdin_text=stdin_text,
                verbose=verbose,
            )
        except RuntimeError as exc:
            print(f"sigil op: {exc}", file=sys.stderr)
            return 1

    if should_confirm_piped_input(invocation):
        if not confirm_piped_input(stdin_text):
            print("sigil op: piped input declined", file=sys.stderr)
            raise click.exceptions.Exit(2)

    if invocation.base == "?":
        if dry_run:
            print(
                "sigil op: ? dry-run: would call read+web question route",
                file=sys.stderr,
            )
            return 0
        return run_question_operator(invocation)

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


def should_confirm_piped_input(invocation: object) -> bool:
    """Return whether an operator needs piped-input confirmation."""
    return (
        getattr(invocation, "base", None) == ","
        and getattr(invocation, "mode", None) == "pipeline"
        and bool(getattr(invocation, "stdin", ""))
    )


def should_confirm_execution(invocation: object) -> bool:
    """Return whether command execution needs confirmation."""
    return (
        getattr(invocation, "base", None) == ","
        and should_confirm_piped_input(invocation)
        and getattr(invocation, "depth", 0) == 2
    )


def should_run_act_operator(invocation: object) -> bool:
    """Return whether this invocation targets the implemented act runner."""
    return (
        getattr(invocation, "base", None) == ","
        and getattr(invocation, "depth", 0) == 3
    )


def confirm_piped_input(stdin_text: str) -> bool:
    """Show a bounded stdin preview and ask whether it may influence a command."""
    print("Sigil received piped input:", file=sys.stderr)
    print("", file=sys.stderr)
    print(stdin_preview(stdin_text), file=sys.stderr)
    print("", file=sys.stderr)
    return confirm_on_tty("Use this input? [y/N] ")


def run_question_operator(invocation: object) -> int:
    """Run question glyphs through the web-authorized ask route."""
    question = str(getattr(invocation, "prompt", "") or "")
    stdin_text = str(getattr(invocation, "stdin", "") or "")
    depth = int(getattr(invocation, "depth", 0) or 0)
    if stdin_text:
        question = question_with_stdin(question, stdin_text)
    if not question:
        question = "Answer the current shell question."
    if depth == 3:
        question = "Give an exhaustive read-only answer.\n\n" + question
    return ask(
        question,
        follow_up=depth >= 2,
    )


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


@cli.command("act")
@click.argument(
    "act_command",
    required=False,
    default="show",
    type=click.Choice(["show", "resume", "abort"]),
)
@click.option("--json", "json_output", is_flag=True)
@click.option("--verbose", is_flag=True, help="Show raw Pi tool and prose output.")
def cmd_act(act_command: str, json_output: bool, verbose: bool) -> int:
    """Inspect, resume, or abort the current Pi edit action."""
    return run_act_command(act_command, json_output, verbose=verbose)


@cli.command("plan", hidden=True)
@click.argument(
    "act_command",
    required=False,
    default="show",
    type=click.Choice(["show", "resume", "abort"]),
)
@click.option("--json", "json_output", is_flag=True)
@click.option("--verbose", is_flag=True, help="Show raw Pi tool and prose output.")
def cmd_plan(act_command: str, json_output: bool, verbose: bool) -> int:
    """Compatibility alias for `sigil act`."""
    return run_act_command(act_command, json_output, verbose=verbose)


def run_act_command(
    act_command: str, json_output: bool, *, verbose: bool = False
) -> int:
    """Run the act control subcommands."""
    if act_command == "resume":
        return run_act_stepper(objective="", verbose=verbose)
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


@cli.command("status")
@click.option("--json", "json_output", is_flag=True)
def cmd_status(json_output: bool) -> int:
    """Show the current session's shortest useful status."""
    status = current_status()
    if json_output:
        pretty_print_json(status.to_dict())
    else:
        print(format_status(status))
    if status.state != "clean":
        raise click.exceptions.Exit(1)
    return 0


@cli.command("render-pi-stream", hidden=True)
@click.option("--json", "json_output", is_flag=True)
@click.option("--compact", is_flag=True)
def cmd_render_pi_stream(json_output: bool, compact: bool) -> int:
    """Render Pi's JSON event stream for the question pipeline."""
    return stream_events(json_output=json_output, compact=compact)


@cli.command("handoff", hidden=True)
@click.argument(
    "handoff_command",
    required=False,
    default="show",
    type=click.Choice(["show", "pop"]),
)
@click.option("--json", "json_output", is_flag=True)
def cmd_handoff(handoff_command: str, json_output: bool) -> int:
    """Inspect or consume the latest Pi bash handoff."""
    record = (
        consume_latest_bash_handoff()
        if handoff_command == "pop"
        else latest_bash_handoff()
    )
    if json_output:
        pretty_print_json(record)
        return 0 if record else 1
    if not record:
        return 1
    command = str(record.get("command") or "")
    if command:
        print(command)
        return 0
    return 1


def pretty_print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def print_json_line(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False))


@cli.group("events", invoke_without_command=True)
@click.option("--json", "json_output", is_flag=True)
@click.option("--raw", is_flag=True, help="With --json, return raw event payloads.")
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=20,
    show_default=True,
    help="Number of recent events to show.",
)
@click.pass_context
def cmd_events(ctx: click.Context, json_output: bool, raw: bool, limit: int) -> int:
    """Inspect Sigil's read-only event log."""
    if ctx.invoked_subcommand is not None:
        return 0
    return print_events_list(json_output=json_output, raw=raw, limit=limit)


@cmd_events.command("list")
@click.option("--json", "json_output", is_flag=True)
@click.option("--raw", is_flag=True, help="With --json, return raw event payloads.")
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=20,
    show_default=True,
    help="Number of recent events to show.",
)
def cmd_events_list(json_output: bool, raw: bool, limit: int) -> int:
    """Show recent events from the global event log."""
    return print_events_list(json_output=json_output, raw=raw, limit=limit)


def print_events_list(*, json_output: bool, raw: bool, limit: int) -> int:
    """Print a bounded recent view of the global event log."""
    events = read_event_log()
    recent = events[-limit:]
    if json_output:
        pretty_print_json(recent if raw else [event_summary(event) for event in recent])
        return 0
    if not recent:
        print("no events recorded")
        return 0
    print_events_table([event_summary(event) for event in recent])
    return 0


def print_events_table(summaries: list[dict[str, object]]) -> None:
    """Print event summaries as a width-aligned table."""
    rows = [
        {
            "time": str(summary["time_label"]),
            "id": str(summary["short_id"]),
            "action": str(summary["action"]),
            "trust": str(summary["trust"]),
            "session": str(summary["short_session"]),
            "summary": str(summary["summary"]),
        }
        for summary in summaries
    ]
    widths = {
        column: max(len(column), *(len(row[column]) for row in rows))
        for column in EVENT_LIST_COLUMNS
        if column != "summary"
    }
    header = "  ".join(
        column.ljust(widths[column]) if column != "summary" else column
        for column in EVENT_LIST_COLUMNS
    )
    print(header)
    for row in rows:
        print(
            "  ".join(
                row[column].ljust(widths[column])
                if column != "summary"
                else row[column]
                for column in EVENT_LIST_COLUMNS
            )
        )


def event_summary(event: dict[str, object]) -> dict[str, object]:
    """Return a user-facing summary for one raw event log entry."""
    event_id = str(event.get("id") or "")
    session = str(event.get("session") or "")
    event_type = str(event.get("type") or "event")
    glyph = event_glyph(event)
    trust = f"{event.get('integrity') or 'unknown'}/{event.get('capability') or 'none'}"
    return {
        "id": event_id or "-",
        "short_id": short_token(event_id),
        "time": event.get("time"),
        "time_label": format_event_time(event.get("time")),
        "type": event_type,
        "glyph": glyph,
        "action": event_action(event, glyph, event_type),
        "trust": trust,
        "session": session or "-",
        "short_session": short_token(session),
        "cwd": str(event.get("cwd") or "-"),
        "summary": event_detail(event, event_type),
        "lineage": f"sigil events lineage {event_id}" if event_id else "",
    }


def short_token(value: str) -> str:
    """Return a short stable token for terminal listings."""
    return value[:8] if value else "-"


def format_event_time(value: object) -> str:
    """Format an event timestamp for a compact local terminal view."""
    if not isinstance(value, int | float):
        return "-"
    return datetime.fromtimestamp(value).strftime("%H:%M:%S")


def event_glyph(event: dict[str, object]) -> str:
    """Return the route glyph for an event, including nested operator events."""
    glyph = event.get("glyph")
    if isinstance(glyph, str) and glyph:
        return glyph
    operator = event.get("operator")
    if isinstance(operator, dict):
        operator = cast("dict[str, object]", operator)
        nested = operator.get("glyph")
        if isinstance(nested, str) and nested:
            return nested
    return "?"


def event_action(event: dict[str, object], glyph: str, event_type: str) -> str:
    """Return a combined glyph plus lifecycle label."""
    operator = event.get("operator")
    if event_type == "operator_completed" and isinstance(operator, dict):
        operator = cast("dict[str, object]", operator)
        name = str(operator.get("name") or "operator")
        return f"{glyph} {name}"
    labels = {
        "question": "question",
        "answer_done": "answer",
        "tool_start": "tool start",
        "tool_end": "tool end",
        "operator_command_executed": "executed",
        "act_created": "act created",
        "act_step_decision": "act decision",
        "act_step_executed": "act executed",
        "act_completed": "act complete",
        "act_aborted": "act aborted",
        "plan_created": "plan created",
        "plan_step_decision": "plan decision",
        "plan_step_executed": "plan executed",
        "plan_completed": "plan complete",
        "plan_aborted": "plan aborted",
        "command_selected": "selected",
    }
    label = labels.get(event_type, event_type.replace("_", " "))
    return f"{glyph} {label}"


def event_detail(event: dict[str, object], event_type: str) -> str:
    """Return the most useful human summary available on an event."""
    if event_type == "operator_completed":
        operator = event.get("operator")
        if isinstance(operator, dict):
            operator = cast("dict[str, object]", operator)
            prompt = clean_summary_text(operator.get("prompt"))
            output = clean_summary_text(event.get("output_snippet"))
            name = str(operator.get("name") or "operator")
            detail = prompt or output
            return f"{name}: {detail}" if detail else name
    if event_type == "question":
        return clean_summary_text(event.get("question")) or "question"
    if event_type == "tool_start":
        tool = clean_summary_text(event.get("tool")) or "tool"
        detail = clean_summary_text(event.get("detail"))
        return f"{tool}: {detail}" if detail else tool
    if event_type == "tool_end":
        return clean_summary_text(event.get("tool")) or "tool finished"
    if event_type == "operator_command_executed":
        return command_status_summary(event)
    if event_type.startswith("act_"):
        if event_type == "act_step_executed":
            return command_status_summary(event)
        return clean_summary_text(event.get("objective")) or clean_summary_text(
            event.get("command")
        )
    if event_type.startswith("plan_"):
        if event_type == "plan_step_executed":
            return command_status_summary(event)
        return clean_summary_text(event.get("objective")) or clean_summary_text(
            event.get("command")
        )
    if event_type == "answer_done":
        return f"{event.get('bytes') or 0} bytes"
    for key in ("command", "output_snippet", "stdout_snippet", "stderr_snippet"):
        detail = clean_summary_text(event.get(key))
        if detail:
            return detail
    return event_type.replace("_", " ")


def command_status_summary(event: dict[str, object]) -> str:
    """Summarize a command-like event with status."""
    command = event.get("command")
    if isinstance(command, list):
        command_text = " ".join(str(part) for part in command)
    else:
        command_text = clean_summary_text(command)
    status = event.get("status")
    if isinstance(status, int):
        return f"{command_text} -> {status}" if command_text else f"status {status}"
    return command_text or "command"


def clean_summary_text(value: object, *, limit: int = 96) -> str:
    """Return a single-line bounded summary string."""
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


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
                parts = [
                    str(session["session_id"]),
                    str(session.get("last_cwd") or "-"),
                    str(session.get("last_event_type") or "-"),
                    str(session["path"]),
                ]
                print("\t".join(parts))
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
    """Record a failed shell command for later comma proposal context."""
    record_failure(command, status, cwd, stdout_snippet, stderr_snippet)
    return 0


@cli.command("record-turn", hidden=True)
@click.option("--status", type=int, required=True)
@click.option("--cwd")
@click.option("--stdout-snippet", default="")
@click.option("--stderr-snippet", default="")
@click.argument("command")
def cmd_record_turn(
    command: str,
    status: int,
    cwd: str | None,
    stdout_snippet: str,
    stderr_snippet: str,
) -> int:
    """Record one shell turn; fans out to failure recording on non-zero exit."""
    record_turn(command, status, cwd, stdout_snippet, stderr_snippet)
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
