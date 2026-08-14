from __future__ import annotations

from pathlib import Path

import pytest

from zeta_plugin import DiscoveryError, ProviderKind, discover_project


def test_discovers_project_provider_directories(tmp_path: Path) -> None:
    _write(
        tmp_path / "models" / "codex.py",
        """\
from zeta_plugin import model


@model("codex", tool_profile={"read": "read_file"})
async def generate(request, context):
    return {"content": []}
""",
    )
    _write(
        tmp_path / "tools" / "web" / "search.py",
        """\
from zeta_plugin import tool


@tool("web_search")
async def search(request, context):
    return {"results": []}
""",
    )
    _write(
        tmp_path / "connectors" / "slack.py",
        """\
from zeta_plugin import connector


@connector("slack")
class Slack:
    async def deliver(self, request, context):
        return {"delivery_id": "123"}
""",
    )
    _write(
        tmp_path / "tools" / "_internal.py",
        """\
from zeta_plugin import tool


@tool("not_loaded")
async def not_loaded(request, context):
    return None
""",
    )

    catalog = discover_project(tmp_path)

    assert list(catalog.models) == ["codex"]
    assert list(catalog.tools) == ["web_search"]
    assert list(catalog.connectors) == ["slack"]
    assert catalog.models["codex"].registration.declaration.tool_profile == {
        "read": "read_file"
    }


def test_rejects_a_provider_in_the_wrong_directory(tmp_path: Path) -> None:
    _write(
        tmp_path / "tools" / "bad.py",
        """\
from zeta_plugin import model


@model("codex")
async def codex(request, context):
    return None
""",
    )

    with pytest.raises(DiscoveryError, match="expected 'tool'"):
        discover_project(tmp_path)


def test_rejects_a_duplicate_provider_identifier(tmp_path: Path) -> None:
    for name in ("first", "second"):
        _write(
            tmp_path / "tools" / f"{name}.py",
            """\
from zeta_plugin import tool


@tool("bash")
async def bash(request, context):
    return None
""",
        )

    with pytest.raises(DiscoveryError, match="Duplicate tool provider 'bash'"):
        discover_project(tmp_path)


def test_requires_an_existing_project_root(tmp_path: Path) -> None:
    with pytest.raises(DiscoveryError, match="does not exist"):
        discover_project(tmp_path / "missing")


def test_provider_catalog_filters_by_kind(tmp_path: Path) -> None:
    _write(
        tmp_path / "tools" / "shell.py",
        """\
from zeta_plugin import tool


@tool("bash")
async def bash(request, context):
    return None
""",
    )

    catalog = discover_project(tmp_path)

    assert list(catalog.providers(ProviderKind.TOOL)) == ["bash"]
    assert catalog.providers(ProviderKind.MODEL) == {}


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
