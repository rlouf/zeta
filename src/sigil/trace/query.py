"""Trace object query and resolution helpers."""

from typing import Any

import click

from sigil.display.summarize import estimated_prompt_tokens
from zeta.records.objects import Object, ObjectId
from zeta.records.stores import (
    AmbiguousIdError,
    Store,
    UnknownIdError,
    resolve_object_id,
)


def resolve_cli_object_id(token: str, *, store: Store) -> ObjectId:
    """Resolve a CLI id token, mapping resolver errors onto CLI errors."""
    try:
        return resolve_object_id(store, token)
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


def get_trace_object(
    object_id: ObjectId,
    *,
    store: Store,
) -> dict[str, Any] | None:
    obj = store.get_object(object_id)
    if obj is None:
        return None
    return {
        "id": object_id,
        "object": {
            "kind": obj.kind,
            "schema": obj.schema,
            "data": obj.data,
            "links": list(obj.links),
        },
        "derivations": [
            {
                "producer": derivation.producer,
                "output_id": derivation.output_id,
                "input_ids": list(derivation.input_ids),
                "params": derivation.params,
            }
            for derivation in store.derivations_for_output(object_id)
        ],
    }


def list_trace_closure(object_id: ObjectId, *, store: Store) -> list[dict[str, Any]]:
    closure = store.graph_closure([object_id])
    return [
        {"id": closure_id, "kind": obj.kind, "schema": obj.schema}
        for closure_id, obj in closure.items()
        if closure_id != object_id
    ]


def list_trace_refs(*, store: Store) -> dict[str, ObjectId]:
    return {ref.name: ref.object_id for ref in store.refs()}


def list_trace_prompts(*, store: Store) -> list[dict[str, Any]]:
    prompts = []
    for prompt_id in store.prompt_object_ids():
        obj = store.get_object(prompt_id)
        if obj is None:
            continue
        prompts.append(
            {
                "id": prompt_id,
                "components": len(obj.links),
                "estimated_tokens": estimated_prompt_tokens(
                    obj.links, store.get_object
                ),
            }
        )
    return prompts
