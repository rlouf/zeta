"""Exact-replacement edit handoff tool implementation."""

from __future__ import annotations

import difflib
import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .base import (
    ToolSpec,
    change_hashes,
    error_result,
    handoff,
    write_temp,
)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["location", "old", "new"],
    "properties": {
        "location": {"type": "string", "minLength": 1},
        "old": {"type": "string", "minLength": 1},
        "new": {"type": "string"},
        "reason": {"type": "string"},
    },
}

SPEC = ToolSpec(
    "edit",
    "Edit a file by exact replacement using location, old text, and new text.",
    SCHEMA,
    interactive=True,
    effects=("write",),
)


def stage(params: dict[str, Any]) -> dict[str, Any]:
    edit = prepare_exact_replacement(params)
    if not isinstance(edit, ExactReplacement):
        return edit
    result = stage_patch(
        edit.patch,
        str(params.get("reason") or f"Apply exact replacement in {edit.location}."),
    )
    result["metadata"] = change_hashes(edit.location, edit.updated) | {
        "path": edit.location
    }
    return result


def run(params: dict[str, Any]) -> dict[str, Any]:
    edit = prepare_exact_replacement(params)
    if not isinstance(edit, ExactReplacement):
        return edit
    hashes = change_hashes(edit.location, edit.updated)
    try:
        Path(edit.location).write_text(edit.updated, encoding="utf-8")
    except OSError as exc:
        return error_result("write-failed", str(exc))
    artifact = write_temp("zeta-edit-", ".patch", edit.patch)
    return {
        "ok": True,
        "content": [
            {"type": "text", "text": f"applied exact replacement to {edit.location}"}
        ],
        "metadata": {
            "location": edit.location,
            "artifact": str(artifact),
            "mode": "direct_replace",
            **hashes,
        },
    }


@dataclass(frozen=True)
class ExactReplacement:
    location: str
    updated: str
    patch: str


def prepare_exact_replacement(
    params: dict[str, Any],
) -> ExactReplacement | dict[str, Any]:
    location = str(params.get("location") or "")
    if not location:
        return error_result("missing-location", "missing location")
    old = str(params.get("old") or "")
    if not old:
        return error_result("missing-old", "missing old")
    new = str(params.get("new") or "")
    try:
        text = Path(location).read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return error_result(
            "not-utf8",
            "file is not valid UTF-8; editing it would corrupt its bytes",
        )
    except OSError as exc:
        return error_result("read-failed", str(exc))
    matches = text.count(old)
    if matches == 0:
        return error_result("old-text-not-found", "old text was not found")
    if matches > 1:
        return error_result("old-text-not-unique", "old text matched more than once")
    updated = text.replace(old, new, 1)
    patch = replacement_patch(location, text, updated)
    if not patch:
        return error_result("empty-edit", "replacement did not change the file")
    return ExactReplacement(location=location, updated=updated, patch=patch)


def stage_patch(patch: str, reason: str) -> dict[str, Any]:
    path = write_temp("zeta-edit-", ".patch", patch)
    return handoff(
        f"git apply {shlex.quote(str(path))}",
        reason,
        artifact=str(path),
    )


def replacement_patch(location: str, old: str, new: str) -> str:
    before = old.splitlines(keepends=True)
    after = new.splitlines(keepends=True)
    lines = difflib.unified_diff(
        before,
        after,
        fromfile=patch_label(location, "a"),
        tofile=patch_label(location, "b"),
    )
    return "".join(normalize_diff_lines(lines))


def normalize_diff_lines(lines: Iterable[str]) -> list[str]:
    normalized = []
    for line in lines:
        if line.endswith("\n"):
            normalized.append(line)
        else:
            normalized.append(f"{line}\n")
            normalized.append("\\ No newline at end of file\n")
    return normalized


def patch_label(path: str, prefix: str) -> str:
    if path.startswith("/"):
        return path
    return f"{prefix}/{path}"
