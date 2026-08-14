from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from zeta_plugin import ProviderRegistration, ProviderSource, tool
from zeta_plugin.discovery import LoadedProvider, ProviderCatalog
from zeta_plugin.host import HostError, ProviderHost, serve


def test_invokes_a_decorated_tool_with_context(tmp_path: Path) -> None:
    _write(
        tmp_path / "tools" / "shell.py",
        """\
from zeta_plugin import tool


@tool("pi.bash")
async def bash(request, context):
    return {"command": request["command"], "effect_key": context["effect_key"]}
""",
    )
    host = ProviderHost(tmp_path, entry_points=[])

    result = host.call(
        "invoke",
        {
            "input": {"tool": "pi.bash", "request": {"command": "pwd"}},
            "base_dir": "/workspace",
            "effect_key": "effect-1",
        },
    )

    assert result == {"command": "pwd", "effect_key": "effect-1"}


def test_rejects_an_unknown_provider(tmp_path: Path) -> None:
    host = ProviderHost(tmp_path, entry_points=[])

    with pytest.raises(HostError, match="Unknown tool provider") as error:
        host.call("invoke", {"input": {"tool": "missing"}})

    assert error.value.stable_code == "provider_not_found"


def test_preserves_a_provider_error_retry_contract(tmp_path: Path) -> None:
    _write(
        tmp_path / "tools" / "limited.py",
        """\
from zeta_plugin import ProviderError, tool


@tool("limited")
async def limited(request, context):
    raise ProviderError("The provider rate limit is active", code="rate_limited", retryable=True)
""",
    )
    host = ProviderHost(tmp_path, entry_points=[])

    with pytest.raises(HostError) as error:
        host.call("invoke", {"input": {"tool": "limited"}})

    assert error.value.message == "The provider rate limit is active"
    assert error.value.stable_code == "rate_limited"
    assert error.value.retryable is True


def test_catalog_includes_a_source_and_fingerprint(tmp_path: Path) -> None:
    _write(
        tmp_path / "tools" / "search.py",
        """\
from zeta_plugin import tool


@tool("web_search", input_schema={"type": "object"})
async def web_search(request, context):
    return {"results": []}
""",
    )
    host = ProviderHost(tmp_path, entry_points=[])

    catalog = host.catalog_result()

    descriptor = catalog["tools"][0]
    assert descriptor["id"] == "web_search"
    assert descriptor["source"]["path"] == str(tmp_path / "tools" / "search.py")
    assert len(descriptor["fingerprint"]) == 64


def test_delivers_to_a_decorated_connector_with_effect_context(tmp_path: Path) -> None:
    _write(
        tmp_path / "connectors" / "slack.py",
        """\
from zeta_plugin import connector


@connector("slack")
class Slack:
    async def deliver(self, request, context):
        return {
            "operation": request["operation"],
            "idempotency_key": request["idempotency_key"],
            "effect_key": context["effect_key"],
        }
""",
    )
    host = ProviderHost(tmp_path, entry_points=[])

    result = host.call(
        "deliver",
        {
            "input": {
                "connector": "slack",
                "request": {
                    "operation": "slack.message.post",
                    "idempotency_key": "message-1",
                },
            },
            "effect_key": "effect-1",
        },
    )

    assert result == {
        "operation": "slack.message.post",
        "idempotency_key": "message-1",
        "effect_key": "effect-1",
    }


def test_subscribes_to_a_decorated_connector(tmp_path: Path) -> None:
    _write(
        tmp_path / "connectors" / "slack.py",
        """\
from zeta_plugin import connector


@connector("slack")
class Slack:
    async def subscribe(self, request, context):
        return {
            "cursor": request["cursor"],
            "events": [{"text": "hello"}],
        }
""",
    )
    host = ProviderHost(tmp_path, entry_points=[])

    result = host.call(
        "subscribe",
        {
            "input": {
                "connector": "slack",
                "request": {"cursor": "cursor-1", "event_type": "slack.message"},
            }
        },
    )

    assert result == {"cursor": "cursor-1", "events": [{"text": "hello"}]}


def test_serves_the_private_json_rpc_protocol(tmp_path: Path) -> None:
    _write(
        tmp_path / "tools" / "echo.py",
        """\
from zeta_plugin import tool


@tool("echo")
async def echo(request, context):
    return {"value": request["value"]}
""",
    )
    input_stream = io.StringIO(
        "\n".join(
            [
                json.dumps(
                    {"jsonrpc": "2.0", "id": "provider-initialize", "result": {}}
                ),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": "one",
                        "method": "invoke",
                        "params": {
                            "input": {"tool": "echo", "request": {"value": "ok"}}
                        },
                    }
                ),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": "two",
                        "method": "shutdown",
                        "params": {"reason": "test"},
                    }
                ),
            ]
        )
        + "\n"
    )
    output_stream = io.StringIO()

    serve(ProviderHost(tmp_path, entry_points=[]), input_stream, output_stream)

    messages = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    assert messages[0]["method"] == "initialize"
    assert messages[1]["result"] == {"value": "ok"}
    assert messages[2]["result"] == {}


def test_host_requires_object_results(tmp_path: Path) -> None:
    async def invalid(request, context):
        return "not an object"

    registration = ProviderRegistration(
        declaration=tool("invalid")(invalid).__zeta_plugin_registration__.declaration,
        target=invalid,
    )
    catalog = ProviderCatalog()
    catalog.add(
        LoadedProvider(
            registration=registration,
            source=ProviderSource(module="test", path=None),
        )
    )
    host = ProviderHost(tmp_path, entry_points=[])
    host._catalog = catalog

    with pytest.raises(HostError, match="non-object result"):
        host.call("invoke", {"input": {"tool": "invalid"}})


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
