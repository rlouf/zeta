"""Trace inspection commands for the Zeta runtime CLI."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click
from zeta.context.builder import reconstructed_prompt_request
from zeta.journal.sqlite import (
    UnknownSessionError,
    available_session_ids,
    open_existing_trace_store,
    resolve_state_dir,
    zeta_sqlite_path,
)
from zeta.models import chat_completion_messages
from zeta.substrate import InMemoryStore, SqliteObjectStore, Store
from zeta.substrate.sqlite import sqlite_table_names
from zeta.trace.diff import render_prompt_diff
from zeta.trace.query import (
    get_trace_object,
    list_trace_closure,
    list_trace_prompts,
    list_trace_refs,
    resolve_cli_object_id,
    resolve_cli_prompt,
)
from zeta.trace.render import (
    object_listing_lines,
    render_trace_object,
    render_trace_tree,
)
from zeta.trace.replay import (
    answer_display_text,
    latest_model_answer,
    record_replay,
    render_replay,
    replay_model_selection,
)
from zeta.trace.tool_calls import tool_call_rows, tool_failure_detail

NARRATIVE_KINDS = ("prompt", "assistant_message")


def trace_scope_options(
    function: Callable[..., Any],
) -> Callable[..., Any]:
    """Give each trace operation direct ownership of its runtime scope."""

    with_state_dir = click.option(
        "--state-dir",
        type=click.Path(file_okay=False, path_type=Path),
        help="Override the runtime state directory.",
    )(function)
    return click.option(
        "--session",
        default=None,
        help="Read this session's trace store.",
    )(with_state_dir)


@click.group("traces")
def traces_group() -> None:
    """Inspect runtime prompt and tool traces.

    Every ID argument accepts a ref name, a full id, or a unique prefix.
    """


def trace_context(
    state_dir: Path | None,
    session: str | None,
) -> tuple[Path, str]:
    """Resolve the runtime state and environment-backed session for one command."""

    return (
        resolve_state_dir(state_dir),
        session or os.environ.get("ZETA_SESSION_ID") or "default",
    )


def scoped_store(
    ctx: click.Context,
    state_dir: Path | None,
    session: str | None,
    *,
    read_only: bool = True,
) -> Store:
    """Return the trace store selected by one trace operation."""

    resolved_state_dir, session_id = trace_context(state_dir, session)
    if read_only and session is not None:
        store = open_session_store(resolved_state_dir, session_id)
        ctx.call_on_close(store.close)
        return store
    path = zeta_sqlite_path(resolved_state_dir)
    if not path.exists():
        if read_only:
            return InMemoryStore(session_id=session_id)
        raise click.ClickException(f"no trace store at {path}")
    store = SqliteObjectStore(
        path,
        session_id=session_id,
        read_only=read_only,
    )
    if read_only and "objects" not in sqlite_table_names(store.connection):
        store.close()
        return InMemoryStore(session_id=session_id)
    ctx.call_on_close(store.close)
    return store


def open_session_store(state_dir: Path, session_id: str) -> SqliteObjectStore:
    """Open a named session's store, mapping lookup errors onto CLI errors."""

    try:
        return open_existing_trace_store(session_id, read_only=True, root=state_dir)
    except UnknownSessionError as error:
        available = ", ".join(error.available) or "none recorded"
        raise click.ClickException(
            f"no trace store for session '{error.session_id}' (recorded: {available})"
        ) from error


def pretty_print_json(value: object) -> None:
    click.echo(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


@traces_group.command("log")
@trace_scope_options
@click.option(
    "--kind",
    "kinds",
    multiple=True,
    help="Only list this object kind (repeatable).",
)
@click.option("--all", "all_kinds", is_flag=True, help="List every object kind.")
@click.option(
    "--limit",
    default=20,
    show_default=True,
    type=int,
    help="Maximum number of objects.",
)
@click.option(
    "--all-sessions",
    "all_sessions",
    is_flag=True,
    help="List every recorded session's store, grouped by session.",
)
@click.pass_context
def trace_log(
    ctx: click.Context,
    state_dir: Path | None,
    session: str | None,
    kinds: tuple[str, ...],
    all_kinds: bool,
    limit: int,
    all_sessions: bool,
) -> int:
    """List recent trace objects, newest first."""

    selected = None if all_kinds else (tuple(kinds) or NARRATIVE_KINDS)
    lines = scope_listing_lines(
        ctx,
        state_dir,
        session,
        all_sessions,
        lambda store: object_listing_lines(store, store.objects(selected, limit)),
    )
    if not lines:
        click.echo("no trace objects recorded", err=True)
        return 0
    for line in lines:
        click.echo(line)
    return 0


@traces_group.command("reinit-store")
@click.option(
    "--state-dir",
    type=click.Path(file_okay=False, path_type=Path),
    help="Override the runtime state directory.",
)
@click.option(
    "--yes",
    is_flag=True,
    help="Recreate the unified Zeta SQLite database without prompting.",
)
def trace_reinit_store(state_dir: Path | None, yes: bool) -> int:
    """Delete all trace and runtime records in the selected Zeta database."""

    path = zeta_sqlite_path(resolve_state_dir(state_dir))
    if not yes:
        click.confirm(
            f"Delete and recreate {path}?",
            abort=True,
            err=True,
        )
    if path.exists():
        path.unlink()
    store = SqliteObjectStore(path)
    store.close()
    click.echo(f"reinitialized {path}")
    return 0


@traces_group.command("tools")
@trace_scope_options
@click.option("--json", "json_output", is_flag=True, help="Emit rows as JSON.")
@click.option("--failed", is_flag=True, help="Only include failed tool calls.")
@click.option("--successful", is_flag=True, help="Only include successful tool calls.")
@click.option(
    "--limit",
    default=20,
    show_default=True,
    type=int,
    help="Maximum number of tool calls.",
)
@click.option(
    "--all-sessions",
    "all_sessions",
    is_flag=True,
    help="List every recorded session's store, grouped by session.",
)
@click.pass_context
def trace_tools(
    ctx: click.Context,
    state_dir: Path | None,
    session: str | None,
    json_output: bool,
    failed: bool,
    successful: bool,
    limit: int,
    all_sessions: bool,
) -> int:
    """List tool calls joined with their results from the trace store."""

    if failed and successful:
        raise click.ClickException("--failed conflicts with --successful")
    rows = scope_tool_rows(
        ctx,
        state_dir,
        session,
        all_sessions,
        failed=failed,
        successful=successful,
        limit=limit,
    )
    if json_output:
        pretty_print_json(rows)
        return 0
    if not rows:
        click.echo("no tool calls recorded", err=True)
        return 0
    for row in rows:
        status = "ok" if row.get("ok") is True else "failed"
        if row.get("ok") is None:
            status = "pending"
        detail = tool_failure_detail(row)
        session = row.get("session")
        prefix = f"{session}  " if isinstance(session, str) and session else ""
        click.echo(
            f"{prefix}{row.get('tool_call_id')}  {row.get('name')}  {status}{detail}"
        )
    return 0


@traces_group.command("grep")
@trace_scope_options
@click.argument("pattern")
@click.option(
    "--kind",
    "kinds",
    multiple=True,
    help="Only search this object kind (repeatable).",
)
@click.option(
    "--limit",
    default=20,
    show_default=True,
    type=int,
    help="Maximum number of matches.",
)
@click.option(
    "--all-sessions",
    "all_sessions",
    is_flag=True,
    help="Search every recorded session's store, grouped by session.",
)
@click.pass_context
def trace_grep(
    ctx: click.Context,
    state_dir: Path | None,
    session: str | None,
    pattern: str,
    kinds: tuple[str, ...],
    limit: int,
    all_sessions: bool,
) -> int:
    """Search trace object data for a substring, newest first."""

    selected = tuple(kinds) or None
    lines = scope_listing_lines(
        ctx,
        state_dir,
        session,
        all_sessions,
        lambda store: object_listing_lines(
            store, store.search_objects(pattern, kind=selected, limit=limit)
        ),
    )
    if not lines:
        click.echo("no trace objects match", err=True)
        return 0
    for line in lines:
        click.echo(line)
    return 0


def scope_tool_rows(
    ctx: click.Context,
    state_dir: Path | None,
    session: str | None,
    all_sessions: bool,
    *,
    failed: bool,
    successful: bool,
    limit: int,
) -> list[dict[str, Any]]:
    resolved_state_dir, session_id = trace_context(state_dir, session)
    if not all_sessions:
        return tool_call_rows(
            scoped_store(ctx, state_dir, session),
            session=session_id,
            failed=failed,
            successful=successful,
            limit=limit,
        )
    rows: list[dict[str, Any]] = []
    for session_id_value in available_session_ids(resolved_state_dir):
        store = open_session_store(resolved_state_dir, session_id_value)
        try:
            rows.extend(
                tool_call_rows(
                    store,
                    session=session_id_value,
                    failed=failed,
                    successful=successful,
                    limit=max(limit, 1),
                )
            )
        finally:
            store.close()
    rows.sort(key=lambda row: float(row.get("created_at") or 0), reverse=True)
    return rows[:limit]


def scope_listing_lines(
    ctx: click.Context,
    state_dir: Path | None,
    session: str | None,
    all_sessions: bool,
    render: Callable[[Store], list[str]],
) -> list[str]:
    """Render lines for the scoped store, or every recorded session."""

    if not all_sessions:
        return render(scoped_store(ctx, state_dir, session))
    resolved_state_dir, _session_id = trace_context(state_dir, session)
    lines = []
    for session_id_value in available_session_ids(resolved_state_dir):
        store = open_session_store(resolved_state_dir, session_id_value)
        try:
            lines.extend(f"{session_id_value}  {line}" for line in render(store))
        finally:
            store.close()
    return lines


@traces_group.command("show")
@trace_scope_options
@click.argument("object_id")
@click.option("--json", "json_output", is_flag=True, help="Emit the raw object JSON.")
@click.pass_context
def trace_show(
    ctx: click.Context,
    state_dir: Path | None,
    session: str | None,
    object_id: str,
    json_output: bool,
) -> int:
    """Show one trace object, its body, and both derivation directions."""

    store = scoped_store(ctx, state_dir, session)
    resolved = resolve_cli_object_id(object_id, store=store)
    if json_output:
        data = get_trace_object(resolved, store=store)
        if data is None:
            raise click.ClickException(f"trace object not found: {object_id}")
        pretty_print_json(data)
        return 0
    lines = render_trace_object(resolved, store=store)
    if lines is None:
        raise click.ClickException(f"trace object not found: {object_id}")
    for line in lines:
        click.echo(line)
    return 0


@traces_group.command("closure")
@trace_scope_options
@click.argument("object_id")
@click.pass_context
def trace_closure(
    ctx: click.Context,
    state_dir: Path | None,
    session: str | None,
    object_id: str,
) -> int:
    """List every object reachable from a trace object."""

    store = scoped_store(ctx, state_dir, session)
    resolved = resolve_cli_object_id(object_id, store=store)
    pretty_print_json({"objects": list_trace_closure(resolved, store=store)})
    return 0


@traces_group.command("tree")
@trace_scope_options
@click.argument("object_id")
@click.option("--down", is_flag=True, help="Follow consumers instead of producers.")
@click.option(
    "--depth",
    default=3,
    show_default=True,
    type=int,
    help="Maximum object depth below the root.",
)
@click.pass_context
def trace_tree(
    ctx: click.Context,
    state_dir: Path | None,
    session: str | None,
    object_id: str,
    down: bool,
    depth: int,
) -> int:
    """Render the derivation tree around one trace object."""

    store = scoped_store(ctx, state_dir, session)
    resolved = resolve_cli_object_id(object_id, store=store)
    for line in render_trace_tree(resolved, down=down, depth=depth, store=store):
        click.echo(line)
    return 0


@traces_group.command("diff")
@trace_scope_options
@click.argument("old_id")
@click.argument("new_id")
@click.option(
    "--stat",
    "stat_only",
    is_flag=True,
    help="One line per component change, without text diffs.",
)
@click.pass_context
def trace_diff(
    ctx: click.Context,
    state_dir: Path | None,
    session: str | None,
    old_id: str,
    new_id: str,
    stat_only: bool,
) -> int:
    """Compare two prompts component by component."""

    store = scoped_store(ctx, state_dir, session)
    old = resolve_cli_prompt(store, old_id)
    new = resolve_cli_prompt(store, new_id)
    for line in render_prompt_diff(store, old, new, stat_only=stat_only):
        click.echo(line)
    return 0


@traces_group.command("replay")
@trace_scope_options
@click.argument("object_id")
@click.option(
    "--model",
    "model_profile",
    default=None,
    help="Replay against this model profile instead of the active one.",
)
@click.option(
    "--diff",
    "diff_output",
    is_flag=True,
    help="Print a diff of the original and replay answers.",
)
@click.pass_context
def trace_replay(
    ctx: click.Context,
    state_dir: Path | None,
    session: str | None,
    object_id: str,
    model_profile: str | None,
    diff_output: bool,
) -> int:
    """Resend a stored prompt through the model boundary."""

    store = scoped_store(ctx, state_dir, session, read_only=False)
    resolved_state_dir, session_id = trace_context(state_dir, session)
    prompt_id, _ = resolve_cli_prompt(store, object_id)
    reconstructed = reconstructed_prompt_request(store, prompt_id)
    if reconstructed is None:
        raise click.ClickException(f"not a prompt: {object_id}")
    selection = replay_model_selection(
        model_profile,
        session_dir=resolved_state_dir / "sessions" / session_id,
    )
    original = latest_model_answer(store, prompt_id)

    message = chat_completion_messages(
        reconstructed.messages,
        api=selection.api,
        tools=reconstructed.tools or None,
        tool_choice="auto",
        max_tokens=reconstructed.max_tokens,
        selected_model=selection.model,
        selected_url=selection.url,
        session_id=session_id,
        thinking=reconstructed.thinking,
    )
    replay_id = record_replay(store, prompt_id, message, selection)
    for line in render_replay(
        prompt_id,
        reconstructed.payload_verified,
        selection,
        original,
        replay_id,
        answer_display_text(message),
        diff_output=diff_output,
    ):
        click.echo(line)
    return 0


@traces_group.command("refs")
@trace_scope_options
@click.pass_context
def trace_refs(
    ctx: click.Context,
    state_dir: Path | None,
    session: str | None,
) -> int:
    """List the mutable refs and the objects they point at."""

    store = scoped_store(ctx, state_dir, session)
    pretty_print_json({"refs": list_trace_refs(store=store)})
    return 0


@traces_group.command("prompts")
@trace_scope_options
@click.pass_context
def trace_prompts(
    ctx: click.Context,
    state_dir: Path | None,
    session: str | None,
) -> int:
    """List recorded prompts with store size statistics."""

    store = scoped_store(ctx, state_dir, session)
    stats = store.stats()
    pretty_print_json(
        {
            "stats": {
                "object_count": stats.object_count,
                "total_bytes": stats.total_bytes,
            },
            "prompts": list_trace_prompts(store=store),
        }
    )
    return 0


__all__ = ["traces_group"]
