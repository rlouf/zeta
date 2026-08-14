from __future__ import annotations

import pytest
from zeta_plugin import (
    DeclarationError,
    ProviderKind,
    connector,
    model,
    provider_registration,
    tool,
)


def test_tool_declaration_attaches_metadata() -> None:
    @tool("web_search")
    async def web_search() -> None:
        return None

    registration = provider_registration(web_search)

    assert registration is not None
    assert registration.target is web_search
    assert registration.declaration.kind is ProviderKind.TOOL
    assert registration.declaration.identifier == "web_search"


def test_model_declaration_keeps_its_tool_profile() -> None:
    @model("codex", tool_profile={"read": "read_file"})
    async def codex() -> None:
        return None

    registration = provider_registration(codex)

    assert registration is not None
    assert registration.declaration.tool_profile == {"read": "read_file"}


def test_connector_must_expose_a_connector_method() -> None:
    with pytest.raises(DeclarationError, match="deliver or subscribe"):

        @connector("slack")
        class Slack:
            pass


def test_identifier_uses_a_stable_namespace_format() -> None:
    with pytest.raises(DeclarationError, match="lower-case"):

        @tool("Zeta.Web")
        async def invalid() -> None:
            return None
