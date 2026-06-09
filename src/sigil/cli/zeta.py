"""User-facing Zeta inspection commands."""

from __future__ import annotations

import json
from typing import Any

import click

from ._base import cli
from ._shared import pretty_print_json
from ..zeta.prompt import estimated_tokens_for_text
from ..zeta.trace import (
    ObjectId,
    Store,
    default_store,
    derivation_payload,
    object_payload,
)


@cli.group("zeta")
def zeta_group() -> None:
    """Inspect Zeta runtime state."""


@zeta_group.group("trace")
def trace_group() -> None:
    """Inspect the current session trace store."""


@trace_group.command("show")
@click.argument("object_id")
def trace_show(object_id: str) -> int:
    data = get_trace_object(object_id)
    if data is None:
        raise click.ClickException(f"trace object not found: {object_id}")
    pretty_print_json(data)
    return 0


@trace_group.command("closure")
@click.argument("object_id")
def trace_closure(object_id: str) -> int:
    pretty_print_json({"objects": list_trace_closure(object_id)})
    return 0


@trace_group.command("refs")
def trace_refs() -> int:
    pretty_print_json({"refs": list_trace_refs()})
    return 0


@trace_group.command("prompts")
def trace_prompts() -> int:
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
        components = [
            active_store.get_object(component_id) for component_id in obj.links
        ]
        prompt_tokens = sum(
            estimated_tokens_for_text(
                json.dumps(
                    component.data,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            for component in components
            if component is not None
        )
        prompts.append(
            {
                "id": prompt_id,
                "components": len(obj.links),
                "estimated_tokens": prompt_tokens,
            }
        )
    return prompts
