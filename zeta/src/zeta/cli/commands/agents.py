"""The `zeta agents` command group."""

from pathlib import Path

import click


@click.group("agents")
def agents() -> None:
    """Manage authored agents."""


@agents.command("new")
@click.argument("slug")
@click.option("--name", default=None, help="Human-readable name.")
@click.option(
    "--description", default=None, help="One-line description / system prompt."
)
@click.option(
    "--accepts", multiple=True, help="Event type the agent accepts (repeatable)."
)
@click.option(
    "--tool", "tools", multiple=True, help="Capability the agent may use (repeatable)."
)
@click.option(
    "--skill", "skills", multiple=True, help="Shared skill the agent uses (repeatable)."
)
@click.option(
    "--base-dir", default=None, help="Base directory for relative file-tool paths."
)
@click.option(
    "--project-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
    help="Project root containing agents/.",
)
@click.option("--force", is_flag=True, help="Overwrite an existing agent file.")
def agents_new(
    slug: str,
    name: str | None,
    description: str | None,
    accepts: tuple[str, ...],
    tools: tuple[str, ...],
    skills: tuple[str, ...],
    base_dir: str | None,
    project_root: Path,
    force: bool,
) -> None:
    """Scaffold agents/<slug>.md from a template."""
    from zeta.authoring.scaffold import ScaffoldError, scaffold_agent

    try:
        path = scaffold_agent(
            project_root,
            slug,
            name=name,
            description=description,
            accepts=accepts,
            tools=tools,
            skills=skills,
            base_dir=base_dir,
            overwrite=force,
        )
    except ScaffoldError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"created {path}")
