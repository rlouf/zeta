"""Prompt trace diff rendering helpers."""

import difflib

from commas.display.summarize import short_trace_id, trace_object_summary
from zeta.records.objects import Object, ObjectId
from zeta.records.stores.object_store import Store


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
    shared, removed, added = prompt_component_sets(old_prompt, new_prompt)
    changed = paired_component_changes(store, removed, added)
    paired_old = {pair[0] for pair in changed}
    paired_new = {pair[1] for pair in changed}
    lines = [f"prompts {short_trace_id(old_id)} → {short_trace_id(new_id)}"]
    lines.extend(changed_component_lines(store, changed, stat_only=stat_only))
    lines.extend(unpaired_component_lines(store, "-", removed, paired_old))
    lines.extend(unpaired_component_lines(store, "+", added, paired_new))
    lines.append(f"= {len(shared)} unchanged")
    return lines


def prompt_component_sets(
    old_prompt: Object,
    new_prompt: Object,
) -> tuple[set[ObjectId], list[ObjectId], list[ObjectId]]:
    shared = set(old_prompt.links) & set(new_prompt.links)
    removed = [link for link in old_prompt.links if link not in shared]
    added = [link for link in new_prompt.links if link not in shared]
    return shared, removed, added


def changed_component_lines(
    store: Store,
    changed: list[tuple[ObjectId, ObjectId]],
    *,
    stat_only: bool,
) -> list[str]:
    lines = []
    for old_component_id, new_component_id in changed:
        lines.append(
            f"~ {component_kind(store, old_component_id):<19} "
            f"{short_trace_id(old_component_id)} → {short_trace_id(new_component_id)}"
        )
        if not stat_only:
            lines.extend(component_text_diff(store, old_component_id, new_component_id))
    return lines


def unpaired_component_lines(
    store: Store,
    marker: str,
    links: list[ObjectId],
    paired: set[ObjectId],
) -> list[str]:
    return [
        f"{marker} {component_change_line(store, link)}"
        for link in links
        if link not in paired
    ]


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
