"""Write tool implementation."""

import shlex
from pathlib import Path
from typing import Any

from zeta.capabilities.base import (
    change_hashes,
    error_result,
    proposed_command_effect,
    write_temp,
)
from zeta.kernel.capabilities import Capability, CapabilityId

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

SPEC = Capability(
    CapabilityId("sigil", "write"),
    "Write content directly or stage a proposed cp command.",
    SCHEMA,
)


def stage(params: dict[str, Any]) -> dict[str, Any]:
    dest = str(params.get("path") or "")
    if not dest:
        return error_result("missing-path", "missing path")
    content = str(params.get("content") or "")
    path = write_temp("zeta-write-", ".tmp", content)
    result = proposed_command_effect(
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
