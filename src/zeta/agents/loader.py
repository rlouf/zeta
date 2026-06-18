"""Recursive authored-agent loading."""

from pathlib import Path

from .spec import AgentSpec, load_spec


def load_specs_recursive(directory: str | Path) -> list[AgentSpec]:
    """Load every enabled Markdown spec under a directory in stable order."""
    root = Path(directory)
    specs = []
    for path in sorted(root.rglob("*.md")):
        if not path.is_file() or path.is_symlink():
            continue
        spec = load_spec(path)
        if spec.enabled:
            specs.append(spec)
    return specs
