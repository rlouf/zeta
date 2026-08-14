"""Portable workspace tools for the executor examples."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from zeta_plugin import tool


def _root() -> Path:
    return Path(os.environ["ZETA_WORKSPACE_ROOT"]).resolve()


def _path(value: str) -> Path:
    path = (_root() / value).resolve()
    if path != _root() and _root() not in path.parents:
        raise ValueError("the path is outside the workspace")
    return path


@tool(
    "workspace.read",
    description="Read one UTF-8 workspace file.",
    input_schema={
        "type": "object",
        "required": ["path"],
        "properties": {"path": {"type": "string"}},
        "additionalProperties": False,
    },
)
def read(request: dict[str, Any], _context: dict[str, Any]) -> dict[str, Any]:
    path = _path(request["path"])
    return {"path": str(path.relative_to(_root())), "content": path.read_text()}


@tool(
    "workspace.grep",
    description="Find text in workspace files.",
    input_schema={
        "type": "object",
        "required": ["query"],
        "properties": {
            "query": {"type": "string"},
            "path": {"type": "string"},
        },
        "additionalProperties": False,
    },
)
def grep(request: dict[str, Any], _context: dict[str, Any]) -> dict[str, Any]:
    query = request["query"]
    path = _path(request.get("path", "."))
    matches = []
    for candidate in sorted(path.rglob("*")):
        if candidate.is_file() and query in candidate.read_text(errors="replace"):
            matches.append(str(candidate.relative_to(_root())))
    return {"matches": matches[:100]}


@tool(
    "workspace.exec",
    description="Run one argument-vector command in the workspace.",
    input_schema={
        "type": "object",
        "required": ["argv"],
        "properties": {
            "argv": {"type": "array", "items": {"type": "string"}},
            "cwd": {"type": "string"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60},
        },
        "additionalProperties": False,
    },
)
def execute(request: dict[str, Any], _context: dict[str, Any]) -> dict[str, Any]:
    argv = request["argv"]
    if not isinstance(argv, list) or not argv or not all(isinstance(value, str) for value in argv):
        raise ValueError("argv must be a non-empty string array")
    cwd = _path(request.get("cwd", "."))
    completed = subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=min(int(request.get("timeout_seconds", 60)), 60),
        check=False,
    )
    return {
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }
