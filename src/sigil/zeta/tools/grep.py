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

from .base import ToolSpec, analysis, effect, error_result, missing

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
        "symbols, errors, strings, or definitions."
    ),
    SCHEMA,
    effects=("search",),
)


def analyze(params: dict[str, Any]) -> dict[str, Any]:
    path = str(params.get("path") or ".")
    pattern = str(params.get("pattern") or "")
    if not pattern:
        return missing("pattern")
    return analysis(effects=[effect("search", path)])


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
    text, content_truncated = truncate_content(result.text)
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


def run_ripgrep(pattern: str, path: str, limit: int) -> GrepResult:
    with tempfile.TemporaryFile() as stderr_spool:
        proc = subprocess.Popen(
            ["rg", "--line-number", "--color", "never", pattern, path],
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


def truncate_content(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_TOOL_RESULT_CHARS:
        return text, False
    return text[:MAX_TOOL_RESULT_CHARS], True
