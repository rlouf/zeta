"""Model profile commands."""

import os
from urllib.parse import urlparse

import click

from zeta.models import (
    clear_active_model_profile,
    default_model_selection,
    load_model_profiles,
    resolve_active_model,
    resolve_model_profile,
    set_active_model_profile,
)

from ..sessions import session_dir
from ._base import cli, examples


@cli.group(
    "model",
    epilog=examples(
        "sigil model list",
        "sigil model use deep",
        "sigil model show",
    ),
)
def cmd_model() -> None:
    """Inspect and switch Zeta model profiles for this session.

    Profiles are defined in ~/.zeta/models.toml. The selection is scoped to
    the current shell session, so other terminals keep their own; without a
    selection, the `default = true` profile applies, then the builtin local
    default.
    """


@cmd_model.command(
    "list",
    epilog=examples("sigil model list"),
)
def cmd_model_list() -> int:
    """List configured model profiles, one per line.

    Reads ~/.zeta/models.toml and marks the profile the next request will use.
    With no profiles configured, prints the builtin local default. Exits 1 when
    the profile config has diagnostics.
    """
    catalog = load_model_profiles()
    active = resolve_active_model(session_dir=session_dir()).selection
    for diagnostic in catalog.diagnostics:
        click.echo(f"model config: {diagnostic.message}", err=True)
    if not catalog.profiles:
        default = default_model_selection()
        click.echo(
            format_model_list_rows([(default.profile, default.model, default.url, "")])
        )
        click.echo(
            "no profiles configured; using the builtin local default. "
            "Add profiles in ~/.zeta/models.toml.",
            err=True,
        )
        return 1 if catalog.diagnostics else 0
    rows: list[tuple[str, str, str, str]] = []
    for profile in sorted(catalog.profiles.values(), key=lambda item: item.name):
        selection = resolve_model_profile(profile.name, catalog=catalog)
        if selection is None:
            continue
        marker = "(active)" if profile.name == active.profile else ""
        rows.append((selection.profile, selection.model, selection.url, marker))
    click.echo(format_model_list_rows(rows))
    return 1 if catalog.diagnostics else 0


def format_model_list_rows(rows: list[tuple[str, str, str, str]]) -> str:
    profile_width = max(len(row[0]) for row in rows)
    model_width = max(len(row[1]) for row in rows)
    endpoint_width = max(len(endpoint_label(row[2])) for row in rows)
    lines = []
    for profile, model, url, marker in rows:
        endpoint = endpoint_label(url)
        endpoint_column = f"{endpoint:<{endpoint_width}}" if marker else endpoint
        line = f"{profile:<{profile_width}}  {model:<{model_width}}  {endpoint_column}"
        if marker:
            line += f"  {marker}"
        lines.append(line)
    return "\n".join(lines)


def endpoint_label(url: str) -> str:
    parsed = urlparse(url)
    return parsed.netloc or url


@cmd_model.command(
    "use",
    epilog=examples(
        "sigil model use deep",
        'sigil model use fast && , "why did the last command fail?"',
    ),
)
@click.argument("name")
def cmd_model_use(name: str) -> int:
    """Use the NAME profile for the current shell session.

    NAME is a profile from ~/.zeta/models.toml. The selection sticks until
    `sigil model clear`; other sessions are unaffected.
    """
    catalog = load_model_profiles()
    for diagnostic in catalog.diagnostics:
        click.echo(f"model config: {diagnostic.message}", err=True)
    selection = resolve_model_profile(name, catalog=catalog)
    if selection is None:
        raise click.ClickException(f"unknown model profile: {name}")
    set_active_model_profile(selection.profile, session_dir=session_dir())
    click.echo(f"model: {selection.profile} -> {selection.model} @ {selection.url}")
    if not os.environ.get("SIGIL_SESSION_ID"):
        click.echo(
            'no shell session detected; the selection applies to session "default"',
            err=True,
        )
    return 0


@cmd_model.command(
    "show",
    epilog=examples("sigil model show"),
)
def cmd_model_show() -> int:
    """Show the model the next request will use, and why.

    The source suffix says where the selection comes from: (session) after
    `sigil model use`, (config) for the `default = true` profile, (builtin)
    for the no-configuration fallback.
    """
    resolution = resolve_active_model(session_dir=session_dir())
    if resolution.stale_profile is not None:
        click.echo(
            f"model: {resolution.stale_profile} is no longer configured", err=True
        )
    active = resolution.selection
    click.echo(
        f"model: {active.profile} -> {active.model} @ {active.url}"
        f" ({resolution.source})"
    )
    return 0


@cmd_model.command(
    "clear",
    epilog=examples("sigil model clear"),
)
def cmd_model_clear() -> int:
    """Clear the session's model selection.

    The session returns to the `default = true` profile, or to the builtin
    local default when no profile claims the flag.
    """
    removed = clear_active_model_profile(session_dir=session_dir())
    if removed:
        click.echo("model: cleared")
    else:
        click.echo("model: default")
    return 0
