"""Model profile inventory commands for the Zeta runtime CLI."""

from __future__ import annotations

import json
from urllib.parse import urlparse

import click
from zeta.models.profiles import (
    ModelSelection,
    default_model_selection,
    load_model_profiles,
    resolve_active_model,
    resolve_model_profile,
)


@click.group("models")
def models_group() -> None:
    """Inspect configured Zeta model profiles."""


@models_group.command("list")
@click.option("--json", "json_output", is_flag=True, help="Emit JSON.")
def models_list(json_output: bool) -> int:
    """List configured model profiles."""

    catalog = load_model_profiles()
    active = resolve_active_model().selection
    for diagnostic in catalog.diagnostics:
        click.echo(f"model config: {diagnostic.message}", err=True)
    if not catalog.profiles:
        default = default_model_selection()
        rows = [model_row(default, active)]
        if json_output:
            click.echo(json.dumps(rows, ensure_ascii=False))
        else:
            click.echo(format_model_list_rows(rows))
            click.echo(
                "no profiles configured; using built-in Codex. "
                "Run `codex login` before the first request.",
                err=True,
            )
        return 1 if catalog.diagnostics else 0
    rows = []
    for profile in sorted(catalog.profiles.values(), key=lambda item: item.name):
        selection = resolve_model_profile(profile.name, catalog=catalog)
        if selection is not None:
            rows.append(model_row(selection, active))
    if json_output:
        click.echo(json.dumps(rows, ensure_ascii=False))
        return 1 if catalog.diagnostics else 0
    click.echo(format_model_list_rows(rows))
    return 1 if catalog.diagnostics else 0


@models_group.command("show")
@click.option("--json", "json_output", is_flag=True, help="Emit JSON.")
def models_show(json_output: bool) -> int:
    """Show the model the next runtime request will use."""

    resolution = resolve_active_model()
    if resolution.stale_profile is not None:
        click.echo(
            f"model: {resolution.stale_profile} is no longer configured",
            err=True,
        )
    active = resolution.selection
    row = {
        "profile": active.profile,
        "model": active.model,
        "url": active.url,
        "thinking": active.thinking,
        "api": active.api,
        "source": resolution.source,
        "stale_profile": resolution.stale_profile,
    }
    if json_output:
        click.echo(json.dumps(row, ensure_ascii=False))
        return 0
    click.echo(
        f"model: {active.profile} -> {active.model} @ {active.url}"
        f" ({resolution.source})"
    )
    return 0


def model_row(selection: ModelSelection, active: ModelSelection) -> dict[str, object]:
    return {
        "profile": selection.profile,
        "model": selection.model,
        "url": selection.url,
        "endpoint": endpoint_label(selection.url),
        "thinking": selection.thinking,
        "api": selection.api,
        "active": selection.profile == active.profile,
    }


def format_model_list_rows(rows: list[dict[str, object]]) -> str:
    profile_width = max(len(str(row["profile"])) for row in rows)
    model_width = max(len(str(row["model"])) for row in rows)
    endpoint_width = max(len(str(row["endpoint"])) for row in rows)
    lines = []
    for row in rows:
        marker = "(active)" if row["active"] else ""
        endpoint = str(row["endpoint"])
        endpoint_column = f"{endpoint:<{endpoint_width}}" if marker else endpoint
        line = (
            f"{str(row['profile']):<{profile_width}}  "
            f"{str(row['model']):<{model_width}}  {endpoint_column}"
        )
        if marker:
            line += f"  {marker}"
        lines.append(line)
    return "\n".join(lines)


def endpoint_label(url: str) -> str:
    parsed = urlparse(url)
    return parsed.netloc or url


__all__ = ["models_group"]
