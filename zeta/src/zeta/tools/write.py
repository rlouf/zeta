"""Write tool implementation."""

from pathlib import Path
from typing import Any

from zeta.capabilities.delivery import change_hashes
from zeta.capabilities.paths import resolve_path
from zeta.capabilities.registry import error_result
from zeta.capabilities.types import Capability, CapabilityId

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path", "content"],
    "properties": {
        "path": {"type": "string"},
        "content": {"type": "string"},
    },
}

SPEC = Capability(
    CapabilityId("zeta", "write"),
    "Write content to a file.",
    SCHEMA,
    delivery_semantics="idempotent_with_key",
)


def run(params: dict[str, Any]) -> dict[str, Any]:
    dest = str(params.get("path") or "")
    if not dest:
        return error_result("missing-path", "missing path")
    dest = str(resolve_path(dest))
    content = str(params.get("content") or "")
    hashes = change_hashes(dest, content)
    try:
        Path(dest).write_text(content, encoding="utf-8")
    except OSError as exc:
        return error_result("write-failed", str(exc))
    return {
        "ok": True,
        "content": [{"type": "text", "text": f"wrote {dest}"}],
        "metadata": {"path": dest, **hashes},
    }
