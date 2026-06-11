"""User-facing trace inspection commands."""

from __future__ import annotations

import difflib
from typing import Any

import click

from ..display.summarize import (
    assistant_trace_summary,
    estimated_prompt_tokens,
    short_trace_id,
    text_content,
    trace_object_summary,
)
from ..zeta.model import chat_completion_messages
from ..zeta.models import ModelSelection, resolve_active_model, resolve_model_profile
from ..zeta.prompt import reconstructed_prompt_request
from ..zeta.trace import (
    AmbiguousIdError,
    Derivation,
    Object,
    ObjectId,
    Store,
    UnknownIdError,
    default_store,
    derivation_payload,
    object_payload,
    resolve_object_id,
    warn_trace_failure_once,
)
from ._base import cli
from ._shared import pretty_print_json

NARRATIVE_KINDS = ("prompt", "assistant_message")
BODY_LINE_LIMIT = 8


@cli.group("trace")
def trace_group() -> None:
    """Inspect the current session trace store."""


@trace_group.command("log")
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
def trace_log(kinds: tuple[str, ...], all_kinds: bool, limit: int) -> int:
    """List recent trace objects, newest first.

    Shows prompts and assistant messages by default; --kind and --all
    widen the listing. Ids are usable with show/closure/tree.
    """
    store = default_store()
    selected = None if all_kinds else (tuple(kinds) or NARRATIVE_KINDS)
    listed = store.objects(kind=selected, limit=limit)
    if not listed:
        click.echo("no trace objects recorded", err=True)
        return 0
    for object_id_value, obj in listed:
        summary = trace_object_summary(obj, get_object=store.get_object)
        click.echo(format_trace_line(object_id_value, obj.kind, summary))
    return 0


def format_trace_line(object_id: ObjectId, kind: str, summary: str) -> str:
    """Format the one-line listing shared by trace log and tree nodes."""
    return f"{short_trace_id(object_id)}  {kind:<19} {summary}".rstrip()


@trace_group.command("show")
@click.argument("object_id")
@click.option("--json", "json_output", is_flag=True, help="Emit the raw object JSON.")
def trace_show(object_id: str, json_output: bool) -> int:
    """Show one trace object, its body, and both derivation directions.

    OBJECT_ID may be a ref name, a full id, or a unique id prefix.
    Renders a human summary by default; --json keeps the raw record.
    """
    resolved = resolve_cli_object_id(object_id)
    if json_output:
        data = get_trace_object(resolved)
        if data is None:
            raise click.ClickException(f"trace object not found: {object_id}")
        pretty_print_json(data)
        return 0
    lines = render_trace_object(resolved)
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


@trace_group.command("closure")
@click.argument("object_id")
def trace_closure(object_id: str) -> int:
    """List every object reachable from a trace object.

    OBJECT_ID may be a ref name, a full id, or a unique id prefix.
    """
    pretty_print_json({"objects": list_trace_closure(resolve_cli_object_id(object_id))})
    return 0


@trace_group.command("tree")
@click.argument("object_id")
@click.option("--down", is_flag=True, help="Follow consumers instead of producers.")
@click.option(
    "--depth",
    default=3,
    show_default=True,
    type=int,
    help="Maximum object depth below the root.",
)
def trace_tree(object_id: str, down: bool, depth: int) -> int:
    """Render the derivation tree around one trace object.

    OBJECT_ID may be a ref name, a full id, or a unique id prefix.
    Walks producers by default; --down walks what came of the object.
    Edges carry the producer name; repeated objects render as `…`.
    """
    resolved = resolve_cli_object_id(object_id)
    for line in render_trace_tree(resolved, down=down, depth=depth):
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


@trace_group.command("diff")
@click.argument("old_id")
@click.argument("new_id")
@click.option(
    "--stat",
    "stat_only",
    is_flag=True,
    help="One line per component change, without text diffs.",
)
def trace_diff(old_id: str, new_id: str, stat_only: bool) -> int:
    """Compare two prompts component by component.

    Identical component ids are unchanged. A removed/added pair of the
    same kind renders as changed, with a text diff of its messages.
    Both arguments accept ref names, full ids, or unique prefixes.
    """
    store = default_store()
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


@trace_group.command("replay")
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
def trace_replay(object_id: str, model_profile: str | None, diff_output: bool) -> int:
    """Resend a stored prompt through the model boundary.

    The request is rebuilt from the prompt's linked components and the
    new answer is recorded with a SigilModelReplay:v1 derivation, so
    replays are themselves traced. OBJECT_ID accepts a ref name, full
    id, or unique prefix.
    """
    store = default_store()
    prompt_id, _ = resolve_cli_prompt(store, object_id)
    reconstructed = reconstructed_prompt_request(store, prompt_id)
    if reconstructed is None:
        raise click.ClickException(f"not a prompt: {object_id}")
    selection = replay_model_selection(model_profile)
    original = latest_model_answer(store, prompt_id)
    message = chat_completion_messages(
        reconstructed.messages,
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
        if derivation.producer == "SigilModelResponse:v1"
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
                    producer="SigilModelReplay:v1",
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


@trace_group.command("refs")
def trace_refs() -> int:
    """List the mutable refs and the objects they point at."""
    pretty_print_json({"refs": list_trace_refs()})
    return 0


@trace_group.command("prompts")
def trace_prompts() -> int:
    """List recorded prompts with store size statistics."""
    stats = default_store().stats()
    pretty_print_json(
        {
            "stats": {
                "object_count": stats.object_count,
                "total_bytes": stats.total_bytes,
            },
            "prompts": list_trace_prompts(),
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
