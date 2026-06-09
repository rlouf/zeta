"""User-facing Zeta inspection commands."""

from __future__ import annotations

import json
from typing import Any

import click

from ._base import cli
from ..zeta import runtime as zeta


@cli.group("zeta")
def zeta_group() -> None:
    """Inspect Zeta runtime state."""


@zeta_group.group("trace")
def trace_group() -> None:
    """Inspect the current session trace store."""


@trace_group.command("show")
@click.argument("object_id")
def trace_show(object_id: str) -> int:
    data = zeta.get_trace_object(object_id)
    if data is None:
        raise click.ClickException(f"trace object not found: {object_id}")
    print_json(data)
    return 0


@trace_group.command("closure")
@click.argument("object_id")
def trace_closure(object_id: str) -> int:
    print_json({"objects": zeta.list_trace_closure(object_id)})
    return 0


@trace_group.command("refs")
def trace_refs() -> int:
    print_json({"refs": zeta.list_trace_refs()})
    return 0


@trace_group.command("prompts")
def trace_prompts() -> int:
    stats = zeta.trace_stats()
    print_json(
        {
            "stats": {
                "object_count": stats.object_count,
                "total_bytes": stats.total_bytes,
            },
            "prompts": zeta.list_trace_prompts(),
        }
    )
    return 0


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))
