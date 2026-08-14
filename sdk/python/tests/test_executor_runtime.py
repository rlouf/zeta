from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from zeta_plugin.executor_runtime import (
    bundle_identity,
    content_address,
    parse_open_request,
)


def test_parses_a_verified_bundle_and_rejects_a_changed_allow_list() -> None:
    request = _open_request()

    parsed = parse_open_request(request)

    assert parsed.workspace_id.startswith("workspace:b3:")
    assert parsed.tool_id.startswith("tools:b3:")
    assert [item.identifier for item in parsed.capabilities] == ["workspace.read"]

    request["capabilities"] = []

    with pytest.raises(ValueError, match="allow-list"):
        parse_open_request(request)


def test_remote_runtime_dispatches_only_the_declared_tool(tmp_path: Path) -> None:
    request = _open_request()
    parsed = parse_open_request(request)
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    for file in parsed.workspace_files:
        path = workspace / file.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(file.content)
    for file in parsed.runtime_files():
        path = runtime / file.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(file.content)
    call = {
        "capability": "workspace.read",
        "input": {"path": "src/lib.rs"},
        "workspace": str(workspace),
        "capabilities": request["tool_bundle"]["capabilities"],
    }
    request_path = runtime / "request.json"
    result_path = runtime / "result.json"
    request_path.write_text(json.dumps(call), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(runtime / "remote_runner.py"), str(request_path), str(result_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert json.loads(result_path.read_text(encoding="utf-8")) == {
        "result": {"path": "src/lib.rs"}
    }


def _open_request() -> dict[str, Any]:
    source = b'''from zeta_plugin import tool


@tool("workspace.read", input_schema={"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}}, output_schema={"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}})
def read(request, context):
    return {"path": request["path"]}
'''
    files = [
        {
            "path": "tools/workspace.py",
            "content_address": content_address(source),
            "content_base64": base64.b64encode(source).decode("ascii"),
        }
    ]
    capabilities = [
        {
            "id": "workspace.read",
            "description": "Read one workspace file.",
            "input_schema": {
                "type": "object",
                "required": ["path"],
                "properties": {"path": {"type": "string"}},
            },
            "output_schema": {
                "type": "object",
                "required": ["path"],
                "properties": {"path": {"type": "string"}},
            },
            "source_path": "tools/workspace.py",
        }
    ]
    return {
        "profile": "isolated-code",
        "policy": {"network": "none", "workspace": {"include": ["tools/**"]}},
        "reuse": "call",
        "workspace_bundle": {
            "id": bundle_identity("workspace", {"files": files}),
            "files": files,
        },
        "tool_bundle": {
            "id": bundle_identity(
                "tools", {"files": files, "capabilities": capabilities}
            ),
            "files": files,
            "capabilities": capabilities,
        },
        "capabilities": ["workspace.read"],
    }
