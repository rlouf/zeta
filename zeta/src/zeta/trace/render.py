"""Human-readable trace rendering helpers."""

from __future__ import annotations

from zeta.records.objects import Object, ObjectId
from zeta.records.stores.object_store import Store
from zeta.trace.summarize import short_trace_id, text_content, trace_object_summary

BODY_LINE_LIMIT = 8


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


def format_trace_line(object_id: ObjectId, kind: str, summary: str) -> str:
    """Format the one-line listing shared by trace log and tree nodes."""
    return f"{short_trace_id(object_id)}  {kind:<19} {summary}".rstrip()


def render_trace_object(
    object_id: ObjectId,
    *,
    store: Store,
) -> list[str] | None:
    """Render one trace object as human-readable lines."""
    obj = store.get_object(object_id)
    if obj is None:
        return None
    summary = trace_object_summary(obj, get_object=store.get_object)
    lines = [
        format_trace_line(object_id, obj.kind, summary),
        f"id      {object_id}",
        f"schema  {obj.schema}",
    ]
    body = trace_object_body(obj, store)
    if body:
        lines.extend(["", *body])
    produced = store.derivations_for_output(object_id)
    if produced:
        lines.extend(["", "produced by"])
        for derivation in produced:
            inputs = " ".join(
                short_trace_id(input_id) for input_id in derivation.input_ids
            )
            lines.append(
                f"  {derivation.producer}" + (f" ← {inputs}" if inputs else "")
            )
    consumed = store.derivations_for_input(object_id)
    if consumed:
        lines.extend(["", "consumed by"])
        for derivation in consumed:
            output = store.get_object(derivation.output_id)
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


def render_trace_tree(
    object_id: ObjectId,
    *,
    down: bool,
    depth: int,
    store: Store,
) -> list[str]:
    """Render the derivation tree as indented lines with producer edges."""
    lines: list[str] = []
    visited: set[ObjectId] = {object_id}

    def node_line(node_id: ObjectId) -> str:
        obj = store.get_object(node_id)
        if obj is None:
            return f"{short_trace_id(node_id)}  (missing)"
        summary = trace_object_summary(obj, get_object=store.get_object)
        return format_trace_line(node_id, obj.kind, summary)

    def walk(node_id: ObjectId, prefix: str, remaining: int) -> None:
        if remaining <= 0:
            return
        if down:
            edges = [
                (derivation.producer, [derivation.output_id])
                for derivation in store.derivations_for_input(node_id)
            ]
        else:
            edges = [
                (derivation.producer, list(derivation.input_ids))
                for derivation in store.derivations_for_output(node_id)
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
