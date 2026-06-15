"""Read tool implementation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from zeta.tools.base import ToolSpec, content_hash, error_result

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

SPEC = ToolSpec(
    "read",
    "Read a UTF-8 text file. Returns a [path#tag] snapshot header and numbered lines for grounded edits.",
    SCHEMA,
    effects=("read",),
)


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
    file_hash = content_hash(raw)
    tag = snapshot_tag(file_hash)
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    selected = lines[offset : offset + limit]
    content = format_tagged_lines(str(path), tag, selected, line_start=offset + 1)
    truncated = len(content) > MAX_READ_CHARS
    if truncated:
        content = content[:MAX_READ_CHARS]
    line_end = offset + len(selected)
    return {
        "ok": True,
        "content": [{"type": "text", "text": content}],
        "metadata": {
            "path": str(path),
            "offset": offset,
            "limit": limit,
            "truncated": truncated,
            "content_hash": file_hash,
            "tag": tag,
            "line_start": offset + 1 if selected else None,
            "line_end": line_end if selected else None,
        },
    }


def snapshot_tag(file_hash: str) -> str:
    return file_hash.split(":", 1)[1][:8]


def format_tagged_lines(
    path: str, tag: str, lines: list[str], *, line_start: int
) -> str:
    numbered = [f"[{path}#{tag}]\n"]
    for index, line in enumerate(lines, start=line_start):
        numbered.append(f"{index}:{line}")
    return "".join(numbered)
