"""User-facing trace inspection commands."""

from collections.abc import Callable
from typing import Any

import click

from zeta.context import reconstructed_prompt_request
from zeta.models import chat_completion_messages
from zeta.substrate import (
    SqliteStore,
    Store,
    UnknownSessionError,
    available_session_ids,
    open_existing_trace_store,
    zeta_sqlite_path,
)

from ..trace.diff import render_prompt_diff
from ..trace.query import (
    get_trace_object,
    list_trace_closure,
    list_trace_prompts,
    list_trace_refs,
    resolve_cli_object_id,
    resolve_cli_prompt,
)
from ..trace.render import (
    object_listing_lines,
    render_trace_object,
    render_trace_tree,
)
from ..trace.replay import (
    answer_display_text,
    latest_model_answer,
    record_replay,
    render_replay,
    replay_model_selection,
)
from ..trace.tools import (
    tool_call_rows,
    tool_failure_detail,
)
from ._base import cli, examples
from ._shared import pretty_print_json

NARRATIVE_KINDS = ("prompt", "assistant_message")


@cli.group(
    "trace",
    epilog=examples(
        "sigil trace log",
        "sigil trace show 4f9d01c2",
        "sigil trace --session 47bd31c0 show turn/4f9d01c2",
    ),
)
@click.option(
    "--session",
    "session_scope",
    default=None,
    help="Read another session's trace store (read-only).",
)
@click.pass_context
def trace_group(ctx: click.Context, session_scope: str | None) -> None:
    """Inspect a session trace store, the current one by default.

    The store records prompts, assistant messages, tool calls, and tool
    results, content-addressed and linked by derivations. It answers "what
    exactly did the model see" for the prompt ids that `sigil log show`
    and `?` hand out.

    Every ID argument accepts a ref name (like turn/<id>), a full id, or
    a unique prefix of an id.
    """
    ctx.obj = session_scope


def trace_session_scope(ctx: click.Context) -> str | None:
    """Return the --session scope set by the enclosing trace group."""
    return ctx.obj if isinstance(ctx.obj, str) else None


def scoped_store(ctx: click.Context) -> Store:
    """Return the trace store selected by the group's --session option.

    Session-scoped stores are uncached read-only opens, closed with the
    command's context.
    """
    scope = trace_session_scope(ctx)
    if scope is None:
        return current_store()
    store = open_session_store(scope)
    ctx.call_on_close(store.close)
    return store


def current_store() -> Store:
    from .. import zeta_session_for_sigil

    return zeta_session_for_sigil().trace_store


def open_session_store(session_id: str) -> SqliteStore:
    """Open a named session's store, mapping lookup errors onto CLI errors."""
    try:
        return open_existing_trace_store(session_id, read_only=True)
    except UnknownSessionError as error:
        available = ", ".join(error.available) or "none recorded"
        raise click.ClickException(
            f"no trace store for session '{error.session_id}' (recorded: {available})"
        ) from error


@trace_group.command(
    "reinit-store",
    epilog=examples(
        "sigil trace reinit-store",
        "sigil trace reinit-store --yes",
    ),
)
@click.option(
    "--yes",
    is_flag=True,
    help="Recreate the unified Zeta SQLite database without prompting.",
)
def trace_reinit_store(yes: bool) -> int:
    """Recreate the local Zeta SQLite database."""
    path = zeta_sqlite_path()
    if not yes:
        click.confirm(
            f"Delete and recreate {path}?",
            abort=True,
            err=True,
        )
    if path.exists():
        path.unlink()
    store = SqliteStore(path)
    store.close()
    click.echo(f"reinitialized {path}")
    return 0


@trace_group.command(
    "log",
    epilog=examples(
        "sigil trace log",
        "sigil trace log --kind tool_call --limit 50",
        "sigil trace log --all",
    ),
)
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
    kinds: tuple[str, ...],
    all_kinds: bool,
    limit: int,
    all_sessions: bool,
) -> int:
    """List recent trace objects, newest first.

    Shows prompts and assistant messages by default; --kind and --all
    widen the listing. Ids are usable with show/closure/tree.
    """
    selected = None if all_kinds else (tuple(kinds) or NARRATIVE_KINDS)
    lines = scope_listing_lines(
        ctx,
        all_sessions,
        lambda store: object_listing_lines(store, store.objects(selected, limit)),
    )
    if not lines:
        click.echo("no trace objects recorded", err=True)
        return 0
    for line in lines:
        click.echo(line)
    return 0


@trace_group.command(
    "tools",
    epilog=examples(
        "sigil trace tools --json",
        "sigil trace tools --failed --json",
        "sigil trace tools --all-sessions --limit 50 --json",
    ),
)
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


@trace_group.command(
    "grep",
    epilog=examples(
        'sigil trace grep "parser test" --kind prompt',
        "sigil trace grep timeout --all-sessions",
    ),
)
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
    pattern: str,
    kinds: tuple[str, ...],
    limit: int,
    all_sessions: bool,
) -> int:
    """Search trace object data for a substring, newest first.

    Matching is case-insensitive over the stored JSON data. LIKE
    wildcards in PATTERN match literally. --kind narrows the search;
    --all-sessions searches every recorded session, grouped by session.
    """
    selected = tuple(kinds) or None
    lines = scope_listing_lines(
        ctx,
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
    all_sessions: bool,
    *,
    failed: bool,
    successful: bool,
    limit: int,
) -> list[dict[str, Any]]:
    if not all_sessions:
        return tool_call_rows(
            scoped_store(ctx),
            session=trace_session_scope(ctx),
            failed=failed,
            successful=successful,
            limit=limit,
        )
    if trace_session_scope(ctx) is not None:
        raise click.ClickException("--all-sessions conflicts with --session")
    rows: list[dict[str, Any]] = []
    for session_id_value in available_session_ids():
        store = open_session_store(session_id_value)
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
    all_sessions: bool,
    render: Callable[[Store], list[str]],
) -> list[str]:
    """Render lines for the scoped store, or for every recorded session.

    With all_sessions each line carries its session id as a prefix;
    the flag conflicts with the group's --session option.
    """
    if not all_sessions:
        return render(scoped_store(ctx))
    if trace_session_scope(ctx) is not None:
        raise click.ClickException("--all-sessions conflicts with --session")
    lines = []
    for session_id_value in available_session_ids():
        store = open_session_store(session_id_value)
        try:
            lines.extend(f"{session_id_value}  {line}" for line in render(store))
        finally:
            store.close()
    return lines


@trace_group.command(
    "show",
    epilog=examples(
        "sigil trace show 4f9d01c2",
        "sigil trace show turn/4f9d01c2 --json",
    ),
)
@click.argument("object_id")
@click.option("--json", "json_output", is_flag=True, help="Emit the raw object JSON.")
@click.pass_context
def trace_show(ctx: click.Context, object_id: str, json_output: bool) -> int:
    """Show one trace object, its body, and both derivation directions.

    Renders a human summary by default; --json keeps the raw record.
    """
    store = scoped_store(ctx)
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


@trace_group.command(
    "closure",
    epilog=examples("sigil trace closure 4f9d01c2"),
)
@click.argument("object_id")
@click.pass_context
def trace_closure(ctx: click.Context, object_id: str) -> int:
    """List every object reachable from a trace object.

    Follows the object's links and derivations transitively and emits
    the result as JSON, one entry per reachable object.
    """
    store = scoped_store(ctx)
    resolved = resolve_cli_object_id(object_id, store=store)
    pretty_print_json({"objects": list_trace_closure(resolved, store=store)})
    return 0


@trace_group.command(
    "tree",
    epilog=examples(
        "sigil trace tree 4f9d01c2",
        "sigil trace tree 4f9d01c2 --down",
    ),
)
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
def trace_tree(ctx: click.Context, object_id: str, down: bool, depth: int) -> int:
    """Render the derivation tree around one trace object.

    Walks what produced the object by default; --down walks what came
    of it. Edges carry the producer name; repeated objects render as
    `…`.
    """
    store = scoped_store(ctx)
    resolved = resolve_cli_object_id(object_id, store=store)
    for line in render_trace_tree(resolved, down=down, depth=depth, store=store):
        click.echo(line)
    return 0


@trace_group.command(
    "diff",
    epilog=examples(
        "sigil trace diff 4f9d01c2 81be33aa",
        "sigil trace diff 4f9d01c2 81be33aa --stat",
    ),
)
@click.argument("old_id")
@click.argument("new_id")
@click.option(
    "--stat",
    "stat_only",
    is_flag=True,
    help="One line per component change, without text diffs.",
)
@click.pass_context
def trace_diff(ctx: click.Context, old_id: str, new_id: str, stat_only: bool) -> int:
    """Compare two prompts component by component.

    Identical component ids are unchanged. A removed/added pair of the
    same kind renders as changed, with a text diff of its messages;
    --stat keeps one line per change instead.
    """
    store = scoped_store(ctx)
    old = resolve_cli_prompt(store, old_id)
    new = resolve_cli_prompt(store, new_id)
    for line in render_prompt_diff(store, old, new, stat_only=stat_only):
        click.echo(line)
    return 0


@trace_group.command(
    "replay",
    epilog=examples(
        "sigil trace replay 4f9d01c2",
        "sigil trace replay 4f9d01c2 --model fast --diff",
    ),
)
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
    object_id: str,
    model_profile: str | None,
    diff_output: bool,
) -> int:
    """Resend a stored prompt through the model boundary.

    Rebuilds the exact request from the prompt's linked components,
    verifies it against the recorded payload hash, and sends it to the
    active model (--model replays against another profile). The new
    answer is recorded with a ModelReplay derivation, so replays are
    themselves traced.
    """
    store = scoped_store(ctx)
    prompt_id, _ = resolve_cli_prompt(store, object_id)
    reconstructed = reconstructed_prompt_request(store, prompt_id)
    if reconstructed is None:
        raise click.ClickException(f"not a prompt: {object_id}")
    selection = replay_model_selection(model_profile)
    original = latest_model_answer(store, prompt_id)
    from ..sessions import session_id as current_session_id

    message = chat_completion_messages(
        reconstructed.messages,
        api=selection.api,
        tools=reconstructed.tools or None,
        tool_choice="auto",
        max_tokens=reconstructed.max_tokens,
        selected_model=selection.model,
        selected_url=selection.url,
        session_id=trace_session_scope(ctx) or current_session_id(),
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


@trace_group.command(
    "refs",
    epilog=examples("sigil trace refs"),
)
@click.pass_context
def trace_refs(ctx: click.Context) -> int:
    """List the mutable refs and the objects they point at.

    Refs are stable names like turn/<id> that track moving targets;
    any of them works where an ID argument is expected.
    """
    pretty_print_json({"refs": list_trace_refs(store=scoped_store(ctx))})
    return 0


@trace_group.command(
    "prompts",
    epilog=examples("sigil trace prompts"),
)
@click.pass_context
def trace_prompts(ctx: click.Context) -> int:
    """List recorded prompts with store size statistics.

    Emits JSON: per prompt its id, component count, and estimated
    tokens, plus the store's object count and total bytes.
    """
    store = scoped_store(ctx)
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
