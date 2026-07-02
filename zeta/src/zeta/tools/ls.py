"""Directory listing tool implementation."""

from pathlib import Path
from typing import Any

from zeta.capabilities.execution import error_result
from zeta.capabilities.types import Capability, CapabilityId

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "path": {"type": "string", "description": "Directory or file to list."},
        "limit": {"type": "integer", "minimum": 1},
        "recursive": {
            "type": "boolean",
            "description": "List descendants recursively instead of only direct children.",
        },
        "min_size_bytes": {
            "type": "integer",
            "minimum": 0,
            "description": "Only include files at least this large. Directories are omitted when set.",
        },
        "exclude": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Path/name patterns to omit, such as .git or dist.",
        },
    },
}

SPEC = Capability(
    CapabilityId("zeta", "ls"),
    "List files with type and byte sizes.",
    SCHEMA,
)


def run(params: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(params.get("path") or "."))
    limit = int(params.get("limit") or 200)
    recursive = bool(params.get("recursive") or False)
    min_size_bytes = params.get("min_size_bytes")
    size_floor = int(min_size_bytes) if min_size_bytes is not None else None
    raw_exclude = params.get("exclude")
    exclude = (
        tuple(str(item) for item in raw_exclude)
        if isinstance(raw_exclude, list)
        else ()
    )
    try:
        entries = listed_entries(path, recursive=recursive, exclude=exclude)
    except OSError as exc:
        return error_result("ls-failed", str(exc))
    rows = []
    for entry in entries:
        row = entry_row(entry, root=path)
        if row is None:
            continue
        kind, size, label = row
        if size_floor is not None and (kind != "file" or size < size_floor):
            continue
        size_text = str(size) if kind == "file" else "-"
        rows.append(f"{size_text}\t{kind}\t{label}")
    lines = rows[:limit]
    omitted = max(len(rows) - limit, 0)
    if omitted:
        lines.append(f"... {omitted} more")
    return {
        "ok": True,
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "metadata": {
            "path": str(path),
            "limit": limit,
            "entries": len(rows),
            "recursive": recursive,
            "min_size_bytes": size_floor,
            "exclude": list(exclude),
        },
    }


def listed_entries(
    path: Path, *, recursive: bool, exclude: tuple[str, ...]
) -> list[Path]:
    if path.is_file():
        return [path]
    iterator = path.rglob("*") if recursive else path.iterdir()
    entries = [
        entry for entry in iterator if not excluded(entry, root=path, patterns=exclude)
    ]
    return sorted(entries, key=entry_sort_key)


def excluded(entry: Path, *, root: Path, patterns: tuple[str, ...]) -> bool:
    if not patterns:
        return False
    rel = entry.relative_to(root)
    rel_text = rel.as_posix()
    for pattern in patterns:
        normalized = pattern.strip().strip("/")
        if not normalized:
            continue
        if entry.name == normalized or rel_text == normalized:
            return True
        if rel_text.startswith(f"{normalized}/"):
            return True
        if rel.match(normalized):
            return True
    return False


def entry_row(entry: Path, *, root: Path) -> tuple[str, int, str] | None:
    try:
        stat = entry.stat(follow_symlinks=False)
    except OSError:
        return None
    kind = "dir" if entry.is_dir() else "file"
    label = entry.relative_to(root).as_posix() if entry != root else entry.name
    if kind == "dir":
        label = f"{label}/"
    return kind, stat.st_size, label


def entry_sort_key(entry: Path) -> tuple[bool, str]:
    return (not entry.is_dir(), entry.as_posix())
