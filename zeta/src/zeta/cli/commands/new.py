"""The `zeta new` starter-project command."""

from pathlib import Path

import click
from zeta.authoring.starter import StarterError, scaffold_inbox_summarizer_project
from zeta.models.codex_auth import codex_auth_path


@click.command("new")
@click.argument(
    "project_root",
    required=False,
    default=Path("."),
    type=click.Path(file_okay=False, path_type=Path),
)
def new(project_root: Path) -> None:
    """Create an inbox-summarizer project."""
    try:
        root = scaffold_inbox_summarizer_project(project_root)
    except StarterError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"created inbox summarizer in {root}")
    if not codex_auth_path().exists():
        click.echo("before you start, run: codex login")
    click.echo("next:")
    click.echo(f"  cd {root}")
    click.echo("  zeta serve")
    click.echo(f"  echo 'Buy milk.' > {root}/inbox/todo.txt")
    click.echo(f"  cat {root}/summaries/todo.txt.md")
