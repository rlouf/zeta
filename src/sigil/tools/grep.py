"""Grep tool implementation."""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sigil.tools.read import snapshot_tag
from zeta.tools.base import ToolSpec, content_hash, error_result

MAX_TOOL_RESULT_CHARS = 12_000

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["pattern"],
    "properties": {
        "pattern": {
            "type": "string",
            "description": "Text or regular expression to search for.",
        },
        "path": {
            "type": "string",
            "description": (
                "File or directory to search. Defaults to the current working "
                "directory."
            ),
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "description": "Maximum number of matching lines to return.",
        },
    },
}

SPEC = ToolSpec(
    "grep",
    (
        "Search file contents recursively. Use before read when looking for "
        "symbols, errors, strings, or definitions. Successful results include "
        "[path#tag] snapshot headers and numbered lines for grounded edits."
    ),
    SCHEMA,
    effects=("search",),
)


def run(params: dict[str, Any]) -> dict[str, Any]:
    pattern = str(params.get("pattern") or "")
    path = str(params.get("path") or ".")
    limit = int(params.get("limit") or 100)
    if not pattern:
        return error_result("missing-pattern", "missing pattern")
    try:
        result = run_ripgrep(pattern, path, limit)
    except FileNotFoundError:
        result = grep_fallback(pattern, Path(path), limit)
    text, tags = tagged_result_text(result)
    text, content_truncated = truncate_content(text)
    truncated = result.truncated or content_truncated
    return {
        "ok": result.ok,
        "content": [{"type": "text", "text": text}],
        "metadata": {
            "pattern": pattern,
            "path": path,
            "limit": limit,
            "matches": result.matches,
            "files": result.files,
            "truncated": truncated,
            "match_limit_reached": result.truncated,
            "content_truncated": content_truncated,
            "max_chars": MAX_TOOL_RESULT_CHARS,
            "status": result.status,
            "tags": tags,
        },
    }


@dataclass(frozen=True)
class GrepResult:
    text: str
    matches: int
    files: int
    truncated: bool
    ok: bool = True
    status: int = 0


@dataclass(frozen=True)
class GrepMatch:
    path: str
    line_number: int
    text: str


def run_ripgrep(pattern: str, path: str, limit: int) -> GrepResult:
    with tempfile.TemporaryFile() as stderr_spool:
        proc = subprocess.Popen(
            [
                "rg",
                "--line-number",
                "--with-filename",
                "--color",
                "never",
                pattern,
                path,
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=stderr_spool,
            start_new_session=True,
        )
        assert proc.stdout is not None
        lines = []
        truncated = False
        for line in proc.stdout:
            if len(lines) >= limit:
                truncated = True
                proc.terminate()
                break
            lines.append(line.rstrip("\n"))
        proc.stdout.close()
        status = wait_for_exit(proc)
        stderr_spool.seek(0)
        stderr = stderr_spool.read().decode("utf-8", errors="replace")
    if status not in {0, 1} and not truncated:
        return GrepResult(stderr.strip(), 0, 0, False, ok=False, status=status)
    return grep_result_from_lines(lines, truncated=truncated, status=status)


def wait_for_exit(proc: subprocess.Popen[str]) -> int:
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        proc.wait()
    return proc.returncode or 0


def grep_fallback(pattern: str, root: Path, limit: int) -> GrepResult:
    matches: list[str] = []
    truncated = False
    for path in fallback_paths(root):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for index, line in enumerate(lines, start=1):
            if pattern in line:
                if len(matches) >= limit:
                    truncated = True
                    break
                matches.append(f"{path}:{index}:{line}")
        if truncated:
            break
    return grep_result_from_lines(matches, truncated=truncated)


def fallback_paths(root: Path) -> Iterator[Path]:
    """Yield candidate files lazily in a stable order without a global sort."""
    if root.is_file():
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            yield Path(dirpath) / name


def grep_result_from_lines(
    lines: list[str],
    *,
    truncated: bool,
    status: int = 0,
) -> GrepResult:
    files = {line.split(":", 1)[0] for line in lines if ":" in line}
    return GrepResult(
        "\n".join(lines),
        matches=len(lines),
        files=len(files),
        truncated=truncated,
        status=status,
    )


def tagged_result_text(result: GrepResult) -> tuple[str, dict[str, str]]:
    if not result.ok:
        return result.text, {}
    matches = [
        match for line in result.text.splitlines() if (match := parse_match(line))
    ]
    if not matches:
        return result.text, {}
    tags: dict[str, str] = {}
    rendered: list[str] = []
    current_path: str | None = None
    for match in matches:
        if match.path != current_path:
            tag = tag_for_path(match.path)
            if tag is None:
                continue
            tags[match.path] = tag
            rendered.append(f"[{match.path}#{tag}]")
            current_path = match.path
        rendered.append(f"{match.line_number}:{match.text}")
    return "\n".join(rendered), tags


def parse_match(line: str) -> GrepMatch | None:
    path, sep, rest = line.partition(":")
    if not sep:
        return None
    raw_line_number, sep, text = rest.partition(":")
    if not sep or not raw_line_number.isdigit():
        return None
    return GrepMatch(path=path, line_number=int(raw_line_number), text=text)


def tag_for_path(path: str) -> str | None:
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    return snapshot_tag(content_hash(raw))


def truncate_content(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_TOOL_RESULT_CHARS:
        return text, False
    return text[:MAX_TOOL_RESULT_CHARS], True
