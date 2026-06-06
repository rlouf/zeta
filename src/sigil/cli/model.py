"""Model profile commands."""

from __future__ import annotations

import click

from ._base import cli
from ..zeta import model as zeta_model
from ..zeta.models import (
    active_model_profile,
    active_model_selection,
    clear_active_model_profile,
    default_model_selection,
    load_model_profiles,
    resolve_model_profile,
    set_active_model_profile,
)


@cli.group("model")
def cmd_model() -> None:
    """Inspect and switch Zeta model profiles for this session."""


@cmd_model.command("list")
def cmd_model_list() -> int:
    """List configured model profiles."""
    catalog = load_model_profiles()
    for diagnostic in catalog.diagnostics:
        click.echo(f"model config: {diagnostic.message}", err=True)
    for profile in sorted(catalog.profiles.values(), key=lambda item: item.name):
        click.echo(
            f"{profile.name}\t{profile.model}\t{profile.url or zeta_model.model_url()}"
        )
    return 1 if catalog.diagnostics else 0


@cmd_model.command("use")
@click.argument("name")
def cmd_model_use(name: str) -> int:
    """Use a model profile for the current shell session."""
    catalog = load_model_profiles()
    for diagnostic in catalog.diagnostics:
        click.echo(f"model config: {diagnostic.message}", err=True)
    selection = resolve_model_profile(name, catalog=catalog)
    if selection is None:
        raise click.ClickException(f"unknown model profile: {name}")
    set_active_model_profile(selection.profile)
    click.echo(f"model: {selection.profile} -> {selection.model} @ {selection.url}")
    return 0


@cmd_model.command("show")
def cmd_model_show() -> int:
    """Show the active model for the current shell session."""
    active_profile = active_model_profile()
    selection = active_model_selection()
    if active_profile is not None and selection is None:
        click.echo(f"model: {active_profile} is no longer configured", err=True)
    active = selection or default_model_selection()
    click.echo(f"model: {active.profile} -> {active.model} @ {active.url}")
    return 0


@cmd_model.command("clear")
def cmd_model_clear() -> int:
    """Clear the active model profile for the current shell session."""
    removed = clear_active_model_profile()
    if removed:
        click.echo("model: cleared")
    else:
        click.echo("model: default")
    return 0
