"""Create the default inbox-summarizer project."""

from __future__ import annotations

import tempfile
from pathlib import Path

import yaml

from zeta.authoring.spec import SpecError, load_spec

GITIGNORE = ".zeta/\n"

INBOX_SUMMARIZER_BODY = """\
A new file is available at {{ event.payload.path }}.

Use the read tool. Write a one-sentence summary to
`summaries/{{ event.payload.name }}.md` with the write tool.

Reply with the summary path.
"""


class StarterError(ValueError):
    """Raised when Zeta cannot create a starter project."""


def scaffold_inbox_summarizer_project(project_root: Path) -> Path:
    """Create an empty project with the inbox summarizer."""
    root = project_root.expanduser().resolve()
    ensure_empty_project_root(root)
    agent = root / "agents" / "inbox-summarizer.md"
    content = inbox_summarizer_markdown(root)
    _validate_agent(content, agent)
    try:
        (root / "agents").mkdir(parents=True)
        (root / "inbox").mkdir()
        (root / "summaries").mkdir()
        agent.write_text(content, encoding="utf-8")
        (root / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    except OSError as exc:
        raise StarterError(f"could not create project {root}: {exc}") from exc
    return root


def ensure_empty_project_root(root: Path) -> None:
    """Reject a target that could lose or mix user work."""
    if not root.exists():
        return
    if not root.is_dir():
        raise StarterError(f"project path is not a directory: {root}")
    try:
        has_content = next(root.iterdir(), None) is not None
    except OSError as exc:
        raise StarterError(f"could not inspect project path {root}: {exc}") from exc
    if has_content:
        raise StarterError(
            f"project path is not empty: {root}; choose an empty directory"
        )


def inbox_summarizer_markdown(project_root: Path) -> str:
    """Render the default inbox-summarizer agent."""
    frontmatter: dict[str, object] = {
        "name": "Inbox Summarizer",
        "description": "Writes a summary for each new inbox file.",
        "session": "shared",
        "base_dir": str(project_root),
        "accepts": [
            {
                "event": "file.created",
                "filter": {"dir": str(project_root / "inbox")},
                "idempotency_key": "file:{path}",
            }
        ],
        "tools": ["read", "write"],
    }
    rendered = yaml.safe_dump(
        frontmatter,
        allow_unicode=True,
        sort_keys=False,
    ).rstrip()
    return f"---\n{rendered}\n---\n{INBOX_SUMMARIZER_BODY}"


def _validate_agent(content: str, path: Path) -> None:
    with tempfile.TemporaryDirectory() as temporary_dir:
        probe = Path(temporary_dir) / path.name
        probe.write_text(content, encoding="utf-8")
        try:
            load_spec(probe)
        except SpecError as exc:
            raise StarterError(f"generated agent is invalid: {exc}") from exc
