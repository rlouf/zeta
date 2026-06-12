"""The `log` group: queries over the delegation ledger."""

from __future__ import annotations

from typing import Any

import click

from ._base import cli
from ._shared import pretty_print_json

DEFAULT_LOG_LIMIT = 20


@cli.group("log", invoke_without_command=True)
@click.option(
    "--touched",
    help="Only turns that wrote or edited PATH through the write/edit tools.",
)
@click.option("--workflow", help="Only turns from this workflow (ask|propose|do|run).")
@click.option(
    "--since",
    help="Only turns at or after a time: YYYY-MM-DD, or an age like 2d, 6h, 30m.",
)
@click.option("--failed", is_flag=True, help="Only failed or aborted turns.")
@click.option("--session", "session_filter", help="Scope to one session id.")
@click.option(
    "--limit",
    default=DEFAULT_LOG_LIMIT,
    show_default=True,
    type=int,
    help="Maximum number of turns.",
)
@click.option("--cost", "show_cost", is_flag=True, help="Append token and call counts.")
@click.option("--json", "json_output", is_flag=True, help="Emit raw turn records.")
@click.pass_context
def cmd_log(
    ctx: click.Context,
    touched: str | None,
    workflow: str | None,
    since: str | None,
    failed: bool,
    session_filter: str | None,
    limit: int,
    show_cost: bool,
    json_output: bool,
) -> int:
    """List ledger turns across every session, newest first.

    Every delegation and recorded shell command is one turn; --session
    narrows to one shell. Subcommands query deeper; `sigil events`
    stays the raw event view.
    """
    if ctx.invoked_subcommand is not None:
        return 0
    # Imported lazily: `sigil.cli` must stay light at import time.
    from ..display.summarize import format_turn_line
    from ..ledger import default_ledger_index, touched_path_variants

    turns = default_ledger_index().query_turns(
        session=session_filter,
        workflow=workflow,
        since=since_epoch(since) if since else None,
        failed=failed,
        touched=touched_path_variants(touched) if touched else None,
        limit=limit,
    )
    if json_output:
        pretty_print_json({"turns": turns})
        return 0
    if not turns:
        click.echo("no turns recorded", err=True)
        return 0
    for turn in turns:
        click.echo(
            format_turn_line(
                turn,
                show_cost=show_cost,
                show_session=session_filter is None,
            )
        )
    return 0


def since_epoch(value: str) -> float:
    """Parse a --since value, mapping parse errors onto CLI errors."""
    from ..ledger import parse_since

    try:
        return parse_since(value)
    except ValueError as error:
        raise click.BadParameter(
            "expected YYYY-MM-DD or an age like 2d, 6h, 30m"
        ) from error


@cmd_log.command("export")
@click.option(
    "--since",
    help="Only turns at or after a time: YYYY-MM-DD, or an age like 2d, 6h, 30m.",
)
@click.option("--session", "session_filter", help="Scope to one session id.")
@click.option(
    "--output",
    "-o",
    "output_path",
    type=click.Path(dir_okay=False, writable=True),
    help="Write the bundle to a file instead of stdout.",
)
def cmd_log_export(
    since: str | None,
    session_filter: str | None,
    output_path: str | None,
) -> int:
    """Export turns and their trace closures as a portable bundle.

    The bundle is self-contained JSON: ledger records plus each turn's
    prompt and tool-result objects, ready for `sigil log import` on
    another machine.
    """
    import json

    from ..bundle import export_bundle

    bundle = export_bundle(
        since=since_epoch(since) if since else None,
        session=session_filter,
    )
    text = json.dumps(bundle, ensure_ascii=False, separators=(",", ":"))
    if output_path is None:
        click.echo(text)
    else:
        from pathlib import Path

        Path(output_path).write_text(text + "\n", encoding="utf-8")
    objects = sum(
        len(graph.get("objects") or ()) for graph in bundle["sessions"].values()
    )
    click.echo(
        f"exported {len(bundle['records'])} record(s),"
        f" {objects} object(s) from {len(bundle['sessions'])} session(s)",
        err=True,
    )
    return 0


@cmd_log.command("import")
@click.argument(
    "bundle_file",
    type=click.Path(exists=True, dir_okay=False),
)
def cmd_log_import(bundle_file: str) -> int:
    """Import a bundle produced by `sigil log export`.

    Records land in the global event log and index; trace objects land
    in per-session stores, so log/blame/trace queries answer here too.
    Re-importing a bundle is a no-op.
    """
    import json
    from pathlib import Path

    from ..bundle import import_bundle

    try:
        payload = json.loads(Path(bundle_file).read_text(encoding="utf-8"))
    except ValueError as error:
        raise click.ClickException(f"not a JSON bundle: {error}") from error
    if not isinstance(payload, dict):
        raise click.ClickException("not a JSON bundle: expected an object")
    try:
        counts = import_bundle(payload)
    except ValueError as error:
        raise click.ClickException(str(error)) from error
    click.echo(
        f"imported {counts['records']} record(s),"
        f" {counts['objects']} object(s) across {counts['sessions']} session(s)"
    )
    return 0


@cmd_log.command("reindex")
def cmd_log_reindex() -> int:
    """Rebuild the ledger index from the event log."""
    # Imported lazily: `sigil.cli` must stay light at import time.
    from ..ledger import default_ledger_index, reindex

    turns, effects = reindex(default_ledger_index())
    click.echo(f"indexed {turns} turn record(s), {effects} effect record(s)")
    return 0


@cmd_log.command("show")
@click.argument("turn_id")
@click.option("--json", "json_output", is_flag=True, help="Emit the raw records.")
def cmd_log_show(turn_id: str, json_output: bool) -> int:
    """Show one turn record in full: contract, cost, effects, prompts.

    TURN_ID may be a full id or a unique prefix.
    """
    from ..display.summarize import render_turn_record
    from ..ledger import default_ledger_index

    index = default_ledger_index()
    resolved = resolve_cli_turn_id(index, turn_id)
    turn = index.turn(resolved)
    if turn is None:
        raise click.ClickException(f"turn not found: {turn_id}")
    effects = index.effects_for_turn(resolved)
    if json_output:
        pretty_print_json({"turn": turn, "effects": effects})
        return 0
    for line in render_turn_record(turn, effects):
        click.echo(line)
    return 0


@cli.command("blame")
@click.argument("file")
def cmd_blame(file: str) -> int:
    """List every turn that wrote or edited FILE, oldest first.

    Covers writes made through the write/edit tools, which record paths
    and content hashes. Bash commands record what ran, not which files
    it touched — find those with `sigil log` and the command text.
    """
    from ..ledger import default_ledger_index, touched_path_variants

    index = default_ledger_index()
    effects: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in touched_path_variants(file):
        for effect in index.effects_touching(path):
            effect_id = str(effect.get("effect_id") or "")
            if effect_id not in seen:
                seen.add(effect_id)
                effects.append(effect)
    effects.sort(key=lambda effect: effect.get("time") or 0.0)
    if not effects:
        click.echo(f"no recorded writes touch {file}", err=True)
        return 0
    for effect in effects:
        turn = index.turn(str(effect.get("turn_id") or ""))
        for line in render_blame_block(effect, turn):
            click.echo(line)
    return 0


def resolve_cli_turn_id(index: Any, token: str) -> str:
    """Resolve a turn id token, mapping resolver errors onto CLI errors."""
    from ..ledger import AmbiguousTurnError, UnknownTurnError, resolve_turn_id

    try:
        return resolve_turn_id(index, token)
    except AmbiguousTurnError as error:
        candidates = "\n  ".join(error.candidates)
        raise click.ClickException(
            f"ambiguous turn id '{token}' matches:\n  {candidates}"
        ) from error
    except UnknownTurnError as error:
        raise click.ClickException(f"turn not found: {token}") from error


def render_blame_block(
    effect: dict[str, Any],
    turn: dict[str, Any] | None,
) -> list[str]:
    """Render one touching effect joined to its turn."""
    from ..display.summarize import (
        first_line,
        format_turn_time,
        short_trace_id,
        truncate,
    )

    when = format_turn_time(effect.get("time"))
    workflow = str((turn or {}).get("workflow") or "?")
    outcome = str((turn or {}).get("outcome") or "?")
    kind = str(effect.get("kind") or "?")
    turn_id = str(effect.get("turn_id") or "?")[:8]
    lines = [f"{when}  {workflow:<7} {outcome:<9} {kind:<10} turn {turn_id}"]
    objective = truncate(first_line(str((turn or {}).get("objective") or "")), 72)
    detail = [objective] if objective else []
    prompt_ids = (turn or {}).get("prompt_object_ids")
    if isinstance(prompt_ids, list) and prompt_ids:
        shorts = " ".join(short_trace_id(str(value)) for value in prompt_ids)
        detail.append(f"prompts {shorts}")
    if detail:
        lines.append("  " + " · ".join(detail))
    return lines
