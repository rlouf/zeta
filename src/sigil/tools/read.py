"""Read tool implementation."""

import re
import urllib.error
import urllib.request
from html import unescape
from pathlib import Path
from typing import Any

from zeta.capabilities.base import content_hash, error_result
from zeta.capabilities.types import Capability, CapabilityId

DEFAULT_READ_LIMIT = 2_000
MAX_READ_CHARS = 50_000
BINARY_SNIFF_BYTES = 8_192
WEB_READ_TIMEOUT_SEC = 30.0

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path"],
    "properties": {
        "path": {
            "type": "string",
            "description": "Local file path or public HTTP(S) URL.",
        },
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

SPEC = Capability(
    CapabilityId("sigil", "read"),
    "Read a UTF-8 text file or public HTTP(S) URL. Returns a [path#tag] snapshot header and numbered lines.",
    SCHEMA,
)


def run(params: dict[str, Any]) -> dict[str, Any]:
    path_value = str(params.get("path") or "")
    offset = int(params.get("offset") or 0)
    limit = int(params.get("limit") or DEFAULT_READ_LIMIT)
    if is_url(path_value):
        return read_url(path_value, offset=offset, limit=limit)
    path = Path(path_value)
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


def read_url(url: str, *, offset: int, limit: int) -> dict[str, Any]:
    try:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "sigil/read-url",
                "Accept": "text/html,text/plain,*/*",
            },
        )
        with urllib.request.urlopen(request, timeout=WEB_READ_TIMEOUT_SEC) as response:
            raw = response.read()
            content_type = str(response.headers.get("content-type") or "")
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        return error_result("web-read-failed", str(exc))
    if b"\x00" in raw[:BINARY_SNIFF_BYTES]:
        return error_result(
            "binary-url",
            "URL looks binary; read supports UTF-8 text and simple HTML only",
        )
    file_hash = content_hash(raw)
    tag = snapshot_tag(file_hash)
    text = raw.decode("utf-8", errors="replace")
    if "html" in content_type.lower() or looks_like_html(text):
        text = html_to_text(text)
    lines = text.splitlines(keepends=True)
    selected = lines[offset : offset + limit]
    content = format_tagged_lines(url, tag, selected, line_start=offset + 1)
    truncated = len(content) > MAX_READ_CHARS
    if truncated:
        content = content[:MAX_READ_CHARS]
    line_end = offset + len(selected)
    return {
        "ok": True,
        "content": [{"type": "text", "text": content}],
        "metadata": {
            "path": url,
            "url": url,
            "source": "web",
            "offset": offset,
            "limit": limit,
            "truncated": truncated,
            "content_hash": file_hash,
            "tag": tag,
            "line_start": offset + 1 if selected else None,
            "line_end": line_end if selected else None,
            "content_type": content_type,
        },
    }


def is_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def looks_like_html(text: str) -> bool:
    prefix = text[:512].lower()
    return "<html" in prefix or "<!doctype html" in prefix or "<body" in prefix


def html_to_text(text: str) -> str:
    body = re.sub(r"(?is)<(script|style).*?</\1>", "", text)
    body = re.sub(r"(?i)<\s*(h[1-6])[^>]*>", "\n# ", body)
    body = re.sub(r"(?i)<\s*/\s*h[1-6]\s*>", "\n", body)
    body = re.sub(r"(?i)<\s*(p|div|section|article|br|li)[^>]*>", "\n", body)
    body = re.sub(r"(?i)<\s*/\s*(p|div|section|article|li)\s*>", "\n", body)
    body = re.sub(r"(?is)<[^>]+>", "", body)
    lines = [" ".join(unescape(line).split()) for line in body.splitlines()]
    return "\n".join(line for line in lines if line).strip() + "\n"


def snapshot_tag(file_hash: str) -> str:
    return file_hash.split(":", 1)[1][:8]


def format_tagged_lines(
    path: str, tag: str, lines: list[str], *, line_start: int
) -> str:
    numbered = [f"[{path}#{tag}]\n"]
    for index, line in enumerate(lines, start=line_start):
        numbered.append(f"{index}:{line}")
    return "".join(numbered)
