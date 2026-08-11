"""Agent scaffolder tests."""

from pathlib import Path

import pytest
from click.testing import CliRunner
from zeta.authoring.scaffold import ScaffoldError, scaffold_agent
from zeta.authoring.spec import load_spec
from zeta.authoring.starter import StarterError, scaffold_inbox_summarizer_project
from zeta.cli.main import cli


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
    assert spec.base_dir == Path("~/vaults/CEO")


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
        ["agents", "new", "filer", "--name", "Filer", "--project-root", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    assert (tmp_path / "agents" / "filer.md").exists()


def test_zeta_new_scaffolds_inbox_summarizer_project(tmp_path: Path) -> None:
    root = scaffold_inbox_summarizer_project(tmp_path / "inbox-agent")

    spec = load_spec(root / "agents" / "inbox-summarizer.md")

    assert root == (tmp_path / "inbox-agent").resolve()
    assert spec.name == "Inbox Summarizer"
    assert spec.session == "shared"
    assert spec.base_dir == root
    assert spec.accepts == ("file.created",)
    assert spec.tools == ("read", "write")
    assert not (root / "agents" / "connectors.yaml").exists()
    assert (root / "inbox").is_dir()
    assert (root / "summaries").is_dir()
    assert (root / ".gitignore").read_text() == ".zeta/\n"


def test_zeta_new_refuses_nonempty_project_root(tmp_path: Path) -> None:
    root = tmp_path / "inbox-agent"
    root.mkdir()
    (root / "notes.md").write_text("keep me", encoding="utf-8")

    with pytest.raises(StarterError, match="project path is not empty"):
        scaffold_inbox_summarizer_project(root)


def test_zeta_new_cli_creates_inbox_summarizer_project(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "inbox-agent"

    result = CliRunner().invoke(cli, ["new", str(root)])

    assert result.exit_code == 0, result.output
    assert (root / "agents" / "inbox-summarizer.md").exists()
    assert "before you start, run: codex login" in result.output
    assert "zeta serve" in result.output
