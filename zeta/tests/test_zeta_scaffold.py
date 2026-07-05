"""Agent scaffolder tests."""

from pathlib import Path

import pytest
from click.testing import CliRunner
from zeta.agents.scaffold import ScaffoldError, scaffold_agent
from zeta.agents.spec import load_spec
from zetad.cli import cli


def test_zeta_scaffold_creates_loadable_agent(tmp_path: Path) -> None:
    path = scaffold_agent(tmp_path, "note-filer")

    assert path == tmp_path / "agents" / "note-filer.md"
    spec = load_spec(path)
    assert spec.slug == "note-filer"
    assert spec.name == "Note Filer"
    assert spec.tools == ("read", "grep", "edit", "write")


def test_zeta_scaffold_honors_options(tmp_path: Path) -> None:
    path = scaffold_agent(
        tmp_path,
        "filer",
        name="Filer",
        description="Files notes.",
        accepts=["file.created"],
        tools=["read", "write"],
        skills=["entity-matching"],
        base_dir="~/vaults/CEO",
    )

    spec = load_spec(path)
    assert spec.name == "Filer"
    assert spec.description == "Files notes."
    assert spec.accepts == ("file.created",)
    assert spec.tools == ("read", "write")
    assert spec.skills == ("entity-matching",)
    assert spec.base_dir == Path.home() / "vaults" / "CEO"


def test_zeta_scaffold_refuses_existing_agent(tmp_path: Path) -> None:
    scaffold_agent(tmp_path, "filer")

    with pytest.raises(ScaffoldError, match="already exists"):
        scaffold_agent(tmp_path, "filer")


def test_zeta_scaffold_overwrites_with_flag(tmp_path: Path) -> None:
    scaffold_agent(tmp_path, "filer", description="v1")

    path = scaffold_agent(tmp_path, "filer", description="v2", overwrite=True)

    assert load_spec(path).description == "v2"


def test_zeta_scaffold_rejects_invalid_slug(tmp_path: Path) -> None:
    with pytest.raises(ScaffoldError, match="invalid agent slug"):
        scaffold_agent(tmp_path, "Bad Slug")


def test_zeta_agent_new_cli_creates_agent_file(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli,
        ["agent", "new", "filer", "--name", "Filer", "--project-root", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    assert (tmp_path / "agents" / "filer.md").exists()
