"""User-facing trace inspection commands."""

from __future__ import annotations

import difflib
from collections.abc import Callable
from typing import Any

import click

from zeta.models import (
    ModelSelection,
    chat_completion_messages,
    resolve_active_model,
    resolve_model_profile,
)
from zeta.prompt import reconstructed_prompt_request
from zeta.trace import (
    AmbiguousIdError,
    Derivation,
    Object,
    ObjectId,
    SqliteStore,
    Store,
    UnknownIdError,
    UnknownSessionError,
    available_session_ids,
    default_store,
    derivation_payload,
    object_payload,
    resolve_object_id,
    warn_trace_failure_once,
)

from ..display.summarize import (
    assistant_trace_summary,
    estimated_prompt_tokens,
    short_trace_id,
    text_content,
    trace_object_summary,
)
from ._base import cli, examples
from ._shared import pretty_print_json

NARRATIVE_KINDS = ("prompt", "assistant_message")
BODY_LINE_LIMIT = 8


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
        return default_store()
    store = open_session_store(scope)
    ctx.call_on_close(store.close)
    return store


def open_session_store(session_id: str) -> SqliteStore:
    """Open a named session's store, mapping lookup errors onto CLI errors."""
    try:
        return default_store(session_id=session_id)
    except UnknownSessionError as error:
        available = ", ".join(error.available) or "none recorded"
        raise click.ClickException(
            f"no trace store for session '{error.session_id}' (recorded: {available})"
        ) from error


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


def object_listing_lines(
    store: Store,
    listed: list[tuple[ObjectId, Object]],
) -> list[str]:
    """Render store objects as one-line listings."""
    lines = []
    for object_id_value, obj in listed:
        summary = trace_object_summary(obj, get_object=store.get_object)
        lines.append(format_trace_line(object_id_value, obj.kind, summary))
    return lines


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


def format_trace_line(object_id: ObjectId, kind: str, summary: str) -> str:
    """Format the one-line listing shared by trace log and tree nodes."""
    return f"{short_trace_id(object_id)}  {kind:<19} {summary}".rstrip()


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


def render_trace_object(
    object_id: ObjectId,
    *,
    store: Store | None = None,
) -> list[str] | None:
    """Render one trace object as human-readable lines."""
    active_store = store or default_store()
    obj = active_store.get_object(object_id)
    if obj is None:
        return None
    summary = trace_object_summary(obj, get_object=active_store.get_object)
    lines = [
        format_trace_line(object_id, obj.kind, summary),
        f"id      {object_id}",
        f"schema  {obj.schema}",
    ]
    body = trace_object_body(obj, active_store)
    if body:
        lines.extend(["", *body])
    produced = active_store.derivations_for_output(object_id)
    if produced:
        lines.extend(["", "produced by"])
        for derivation in produced:
            inputs = " ".join(
                short_trace_id(input_id) for input_id in derivation.input_ids
            )
            lines.append(
                f"  {derivation.producer}" + (f" ← {inputs}" if inputs else "")
            )
    consumed = active_store.derivations_for_input(object_id)
    if consumed:
        lines.extend(["", "consumed by"])
        for derivation in consumed:
            output = active_store.get_object(derivation.output_id)
            kind = output.kind if output is not None else "?"
            lines.append(
                f"  {derivation.producer} → "
                f"{short_trace_id(derivation.output_id)} {kind}"
            )
    return lines


def trace_object_body(obj: Object, store: Store) -> list[str]:
    """Render the kind-specific body lines for a trace object."""
    if obj.kind == "prompt":
        lines = ["components"]
        for link in obj.links:
            component = store.get_object(link)
            if component is None:
                lines.append(f"  {short_trace_id(link)}  (missing)")
                continue
            summary = trace_object_summary(component, get_object=store.get_object)
            lines.append("  " + format_trace_line(link, component.kind, summary))
        return lines
    text = trace_object_text(obj).strip()
    if not text:
        return []
    body = text.splitlines()[:BODY_LINE_LIMIT]
    if len(text.splitlines()) > BODY_LINE_LIMIT:
        body.append("…")
    return body


def trace_object_text(obj: Object) -> str:
    """Return the primary text carried by a trace object, if any."""
    message = obj.data.get("message")
    if isinstance(message, dict):
        return str(message.get("content") or "")
    result = obj.data.get("result")
    if isinstance(result, dict):
        return text_content(result)
    return ""


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


def render_trace_tree(
    object_id: ObjectId,
    *,
    down: bool,
    depth: int,
    store: Store | None = None,
) -> list[str]:
    """Render the derivation tree as indented lines with producer edges."""
    active_store = store or default_store()
    lines: list[str] = []
    visited: set[ObjectId] = {object_id}

    def node_line(node_id: ObjectId) -> str:
        obj = active_store.get_object(node_id)
        if obj is None:
            return f"{short_trace_id(node_id)}  (missing)"
        summary = trace_object_summary(obj, get_object=active_store.get_object)
        return format_trace_line(node_id, obj.kind, summary)

    def walk(node_id: ObjectId, prefix: str, remaining: int) -> None:
        if remaining <= 0:
            return
        if down:
            edges = [
                (derivation.producer, [derivation.output_id])
                for derivation in active_store.derivations_for_input(node_id)
            ]
        else:
            edges = [
                (derivation.producer, list(derivation.input_ids))
                for derivation in active_store.derivations_for_output(node_id)
            ]
        for edge_index, (producer, child_ids) in enumerate(edges):
            last_edge = edge_index == len(edges) - 1
            lines.append(f"{prefix}{'└─' if last_edge else '├─'} {producer}")
            child_prefix = prefix + ("   " if last_edge else "│  ")
            for child_index, child_id in enumerate(child_ids):
                last_child = child_index == len(child_ids) - 1
                connector = "└─" if last_child else "├─"
                seen = child_id in visited
                marker = " …" if seen else ""
                lines.append(f"{child_prefix}{connector} {node_line(child_id)}{marker}")
                if seen:
                    continue
                visited.add(child_id)
                walk(
                    child_id,
                    child_prefix + ("   " if last_child else "│  "),
                    remaining - 1,
                )

    lines.append(node_line(object_id))
    walk(object_id, "", depth)
    return lines


def resolve_cli_object_id(token: str, *, store: Store | None = None) -> ObjectId:
    """Resolve a CLI id token, mapping resolver errors onto CLI errors."""
    try:
        return resolve_object_id(store or default_store(), token)
    except AmbiguousIdError as error:
        candidates = "\n  ".join(error.candidates)
        raise click.ClickException(
            f"ambiguous trace id '{token}' matches:\n  {candidates}"
        ) from error
    except UnknownIdError as error:
        raise click.ClickException(f"trace object not found: {token}") from error


def resolve_cli_prompt(store: Store, token: str) -> tuple[ObjectId, Object]:
    """Resolve a CLI id token to a prompt object, or fail with its kind."""
    object_id = resolve_cli_object_id(token, store=store)
    obj = store.get_object(object_id)
    if obj is None or obj.kind != "prompt":
        kind = obj.kind if obj is not None else "missing"
        raise click.ClickException(f"not a prompt: {token} ({kind})")
    return object_id, obj


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


def render_prompt_diff(
    store: Store,
    old: tuple[ObjectId, Object],
    new: tuple[ObjectId, Object],
    *,
    stat_only: bool,
) -> list[str]:
    """Render the component-level changes between two prompts."""
    old_id, old_prompt = old
    new_id, new_prompt = new
    shared = set(old_prompt.links) & set(new_prompt.links)
    removed = [link for link in old_prompt.links if link not in shared]
    added = [link for link in new_prompt.links if link not in shared]
    changed = paired_component_changes(store, removed, added)
    paired_old = {pair[0] for pair in changed}
    paired_new = {pair[1] for pair in changed}
    lines = [f"prompts {short_trace_id(old_id)} → {short_trace_id(new_id)}"]
    for old_component_id, new_component_id in changed:
        lines.append(
            f"~ {component_kind(store, old_component_id):<19} "
            f"{short_trace_id(old_component_id)} → {short_trace_id(new_component_id)}"
        )
        if not stat_only:
            lines.extend(component_text_diff(store, old_component_id, new_component_id))
    for link in removed:
        if link not in paired_old:
            lines.append(f"- {component_change_line(store, link)}")
    for link in added:
        if link not in paired_new:
            lines.append(f"+ {component_change_line(store, link)}")
    lines.append(f"= {len(shared)} unchanged")
    return lines


def paired_component_changes(
    store: Store,
    removed: list[ObjectId],
    added: list[ObjectId],
) -> list[tuple[ObjectId, ObjectId]]:
    """Pair removed and added components of the same kind, in order."""
    added_by_kind: dict[str, list[ObjectId]] = {}
    for link in added:
        added_by_kind.setdefault(component_kind(store, link), []).append(link)
    pairs = []
    for link in removed:
        candidates = added_by_kind.get(component_kind(store, link))
        if candidates:
            pairs.append((link, candidates.pop(0)))
    return pairs


def component_kind(store: Store, object_id: ObjectId) -> str:
    obj = store.get_object(object_id)
    return obj.kind if obj is not None else "(missing)"


def component_change_line(store: Store, object_id: ObjectId) -> str:
    obj = store.get_object(object_id)
    if obj is None:
        return f"(missing) {short_trace_id(object_id)}"
    summary = trace_object_summary(obj, get_object=store.get_object)
    return f"{obj.kind:<19} {short_trace_id(object_id)}  {summary}".rstrip()


def component_text_diff(
    store: Store,
    old_id: ObjectId,
    new_id: ObjectId,
) -> list[str]:
    return [
        f"  {line}"
        for line in difflib.unified_diff(
            component_message_text(store, old_id).splitlines(),
            component_message_text(store, new_id).splitlines(),
            fromfile=short_trace_id(old_id),
            tofile=short_trace_id(new_id),
            lineterm="",
        )
    ]


def component_message_text(store: Store, object_id: ObjectId) -> str:
    obj = store.get_object(object_id)
    if obj is None:
        return ""
    message = obj.data.get("message")
    if not isinstance(message, dict):
        return ""
    return str(message.get("content") or "")


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
    message = chat_completion_messages(
        reconstructed.messages,
        api=selection.api,
        tools=reconstructed.tools or None,
        tool_choice="auto",
        max_tokens=reconstructed.max_tokens,
        selected_model=selection.model,
        selected_url=selection.url,
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


def replay_model_selection(model_profile: str | None) -> ModelSelection:
    """Return the model a replay should use, honoring --model."""
    if model_profile is None:
        return resolve_active_model().selection
    selection = resolve_model_profile(model_profile)
    if selection is None:
        raise click.ClickException(f"unknown model profile: {model_profile}")
    return selection


def latest_model_answer(
    store: Store,
    prompt_id: ObjectId,
) -> tuple[ObjectId, str] | None:
    """Return the newest recorded assistant answer for a prompt."""
    answer_ids = [
        derivation.output_id
        for derivation in store.derivations_for_input(prompt_id)
        if derivation.producer == "ModelResponse"
    ]
    for answer_id in reversed(answer_ids):
        obj = store.get_object(answer_id)
        if obj is None:
            continue
        message = obj.data.get("message")
        if isinstance(message, dict):
            return answer_id, answer_display_text(message)
    return None


def answer_display_text(message: dict[str, Any]) -> str:
    """Return an assistant message's text, or its tool calls when text-free."""
    content = str(message.get("content") or "")
    if content:
        return content
    return assistant_trace_summary({"message": message})


def record_replay(
    store: Store,
    prompt_id: ObjectId,
    message: dict[str, Any],
    selection: ModelSelection,
) -> ObjectId | None:
    """Record the replay answer in the trace graph, fail-open."""
    try:
        with store.batch():
            replay_id = store.put_object(
                Object(
                    kind="assistant_message",
                    schema="zeta.assistant_output.v1",
                    data={"message": message},
                    links=(prompt_id,),
                )
            )
            store.record_derivation(
                Derivation(
                    producer="ModelReplay",
                    output_id=replay_id,
                    input_ids=(prompt_id,),
                    params={"profile": selection.profile, "model": selection.model},
                )
            )
        return replay_id
    except Exception as exc:
        warn_trace_failure_once("trace_replay", exc)
        return None


def render_replay(
    prompt_id: ObjectId,
    payload_verified: bool,
    selection: ModelSelection,
    original: tuple[ObjectId, str] | None,
    replay_id: ObjectId | None,
    replay_content: str,
    *,
    diff_output: bool,
) -> list[str]:
    """Render the replay outcome as plain forensic lines."""
    verification = "verified" if payload_verified else "differs from the recorded hash"
    lines = [
        f"prompt   {short_trace_id(prompt_id)}  payload {verification}",
        f"model    {selection.profile} -> {selection.model} @ {selection.url}",
        "",
    ]
    original_label = short_trace_id(original[0]) if original else "(none recorded)"
    original_content = original[1] if original else ""
    replay_label = short_trace_id(replay_id) if replay_id else "(unrecorded)"
    if diff_output:
        lines.extend(
            difflib.unified_diff(
                original_content.splitlines(),
                replay_content.splitlines(),
                fromfile=f"original {original_label}",
                tofile=f"replay {replay_label}",
                lineterm="",
            )
        )
        return lines
    lines.append(f"original {original_label}")
    if original_content:
        lines.append(original_content)
    lines.extend(["", f"replay   {replay_label}"])
    if replay_content:
        lines.append(replay_content)
    return lines


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


def get_trace_object(
    object_id: ObjectId,
    *,
    store: Store | None = None,
) -> dict[str, Any] | None:
    active_store = store or default_store()
    obj = active_store.get_object(object_id)
    if obj is None:
        return None
    return {
        "id": object_id,
        "object": object_payload(obj),
        "derivations": [
            derivation_payload(derivation)
            for derivation in active_store.derivations_for_output(object_id)
        ],
    }


def list_trace_closure(
    object_id: ObjectId,
    *,
    store: Store | None = None,
) -> list[dict[str, Any]]:
    active_store = store or default_store()
    closure = active_store.graph_closure([object_id])
    return [
        {"id": closure_id, "kind": obj.kind, "schema": obj.schema}
        for closure_id, obj in closure.items()
        if closure_id != object_id
    ]


def list_trace_refs(*, store: Store | None = None) -> dict[str, ObjectId]:
    return dict((store or default_store()).refs())


def list_trace_prompts(*, store: Store | None = None) -> list[dict[str, Any]]:
    active_store = store or default_store()
    prompts = []
    for prompt_id in active_store.prompt_object_ids():
        obj = active_store.get_object(prompt_id)
        if obj is None:
            continue
        prompts.append(
            {
                "id": prompt_id,
                "components": len(obj.links),
                "estimated_tokens": estimated_prompt_tokens(
                    obj.links, active_store.get_object
                ),
            }
        )
    return prompts
