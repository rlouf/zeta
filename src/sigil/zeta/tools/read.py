"""Read tool implementation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import ToolSpec, error_result

DEFAULT_READ_LIMIT = 2_000
MAX_READ_CHARS = 50_000
BINARY_SNIFF_BYTES = 8_192

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path"],
    "properties": {
        "path": {"type": "string"},
        "offset": {
            "type": "integer",
            "minimum": 0,
            "description": "Number of leading lines to skip (0-based).",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "description": "Maximum number of lines to return.",
        },
    },
}

SPEC = ToolSpec("read", "Read a UTF-8 text file.", SCHEMA, effects=("read",))


def run(params: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(params.get("path") or ""))
    offset = int(params.get("offset") or 0)
    limit = int(params.get("limit") or DEFAULT_READ_LIMIT)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return error_result("read-failed", str(exc))
    if b"\x00" in raw[:BINARY_SNIFF_BYTES]:
        return error_result(
            "binary-file",
            "file looks binary; read supports UTF-8 text only",
        )
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    content = "".join(lines[offset : offset + limit])
    truncated = len(content) > MAX_READ_CHARS
    if truncated:
        content = content[:MAX_READ_CHARS]
    return {
        "ok": True,
        "content": [{"type": "text", "text": content}],
        "metadata": {
            "path": str(path),
            "offset": offset,
            "limit": limit,
            "truncated": truncated,
        },
    }
