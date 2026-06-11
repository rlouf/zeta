"""Write tool implementation."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from .base import (
    ToolSpec,
    analysis,
    change_hashes,
    effect,
    error_result,
    handoff,
    missing,
    write_temp,
)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path", "content"],
    "properties": {
        "path": {"type": "string"},
        "content": {"type": "string"},
        "reason": {"type": "string"},
    },
}

SPEC = ToolSpec(
    "write",
    "Write content directly or stage a cp handoff, depending on the active workflow.",
    SCHEMA,
    interactive=True,
    effects=("write",),
)


def analyze(params: dict[str, Any]) -> dict[str, Any]:
    path = str(params.get("path") or "")
    if not path:
        return missing("path")
    return analysis(effects=[effect("write", path)])


def stage(params: dict[str, Any]) -> dict[str, Any]:
    dest = str(params.get("path") or "")
    if not dest:
        return error_result("missing-path", "missing path")
    content = str(params.get("content") or "")
    path = write_temp("zeta-write-", ".tmp", content)
    result = handoff(
        f"cp {shlex.quote(str(path))} {shlex.quote(dest)}",
        str(params.get("reason") or f"Write {dest}."),
        artifact=str(path),
    )
    result["metadata"] = change_hashes(dest, content) | {"path": dest}
    return result


def run(params: dict[str, Any]) -> dict[str, Any]:
    dest = str(params.get("path") or "")
    if not dest:
        return error_result("missing-path", "missing path")
    content = str(params.get("content") or "")
    hashes = change_hashes(dest, content)
    try:
        Path(dest).write_text(content, encoding="utf-8")
    except OSError as exc:
        return error_result("write-failed", str(exc))
    return {
        "ok": True,
        "content": [{"type": "text", "text": f"wrote {dest}"}],
        "metadata": {"mode": "direct", "path": dest, **hashes},
    }
