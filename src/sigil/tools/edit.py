"""Exact-replacement edit tool implementation."""

import difflib
import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zeta.capabilities import (
    CapabilityId,
    CapabilitySpec,
    change_hashes,
    content_hash,
    error_result,
    proposed_command_effect,
    write_temp,
)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "anyOf": [
        {"required": ["input"]},
        {"required": ["location", "old", "new"]},
    ],
    "properties": {
        "input": {"type": "string", "minLength": 1},
        "location": {"type": "string", "minLength": 1},
        "old": {"type": "string", "minLength": 1},
        "new": {"type": "string"},
        "reason": {"type": "string"},
    },
}

SPEC = CapabilitySpec(
    CapabilityId("sigil", "edit"),
    "Edit a file. Prefer tagged input from read: [path#tag] plus SWAP, DEL, INS.PRE, or INS.POST line operations.",
    SCHEMA,
    interactive=True,
    effects=("write",),
    aliases=("edit",),
)

HEADER_RE = re.compile(r"^\[(?P<path>.+)\]$")
SWAP_RE = re.compile(r"^SWAP (?P<start>[1-9][0-9]*)\.\.(?P<end>[1-9][0-9]*):$")
DEL_RE = re.compile(r"^DEL (?P<start>[1-9][0-9]*)\.\.(?P<end>[1-9][0-9]*)$")
INS_RE = re.compile(r"^(?P<kind>INS\.PRE|INS\.POST) (?P<line>[1-9][0-9]*):$")


def stage(params: dict[str, Any]) -> dict[str, Any]:
    edit = prepare_edit(params)
    if not isinstance(edit, PreparedEdit):
        return edit
    result = stage_patch(
        edit.patch,
        str(params.get("reason") or f"Apply edit in {edit.location}."),
    )
    result["metadata"] = change_hashes(edit.location, edit.updated) | {
        "path": edit.location,
        **edit.metadata,
    }
    return result


def run(params: dict[str, Any]) -> dict[str, Any]:
    edit = prepare_edit(params)
    if not isinstance(edit, PreparedEdit):
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
            **hashes,
            **edit.metadata,
        },
    }


@dataclass(frozen=True)
class PreparedEdit:
    location: str
    updated: str
    patch: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class LineOperation:
    kind: str
    start: int
    end: int
    body: tuple[str, ...] = ()


def prepare_edit(params: dict[str, Any]) -> PreparedEdit | dict[str, Any]:
    if "input" in params:
        return prepare_hashline_edit(params)
    return prepare_exact_replacement(params)


def prepare_exact_replacement(
    params: dict[str, Any],
) -> PreparedEdit | dict[str, Any]:
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
    return PreparedEdit(
        location=location,
        updated=updated,
        patch=patch,
        metadata={"mode": "direct_replace"},
    )


def prepare_hashline_edit(params: dict[str, Any]) -> PreparedEdit | dict[str, Any]:
    parsed = parse_hashline_input(str(params.get("input") or ""))
    if not isinstance(parsed, HashlineEdit):
        return parsed
    path = Path(parsed.location)
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return error_result(
            "not-utf8",
            "file is not valid UTF-8; editing it would corrupt its bytes",
        )
    except OSError as exc:
        return error_result("read-failed", str(exc))
    current_tag = tag_for_text(text)
    if current_tag != parsed.tag:
        return error_result(
            "stale-tag",
            "file changed since the tagged read; read it again before editing",
        )
    updated = apply_line_operations(text, parsed.operations)
    if not isinstance(updated, str):
        return updated
    patch = replacement_patch(parsed.location, text, updated)
    if not patch:
        return error_result("empty-edit", "replacement did not change the file")
    return PreparedEdit(
        location=parsed.location,
        updated=updated,
        patch=patch,
        metadata={
            "mode": "hashline",
            "tag": parsed.tag,
            "operations": [operation_metadata(op) for op in parsed.operations],
        },
    )


@dataclass(frozen=True)
class HashlineEdit:
    location: str
    tag: str
    operations: tuple[LineOperation, ...]


def parse_hashline_input(value: str) -> HashlineEdit | dict[str, Any]:
    """Parse Sigil's small OMP hashline-inspired tagged edit format."""
    lines = value.splitlines()
    if not lines:
        return error_result("missing-section-header", "missing [path#tag] header")
    header = HEADER_RE.match(lines[0])
    if header is None:
        return error_result("missing-section-header", "missing [path#tag] header")
    header_value = header.group("path")
    if "#" not in header_value:
        return error_result("missing-tag", "section header must include #tag")
    location, tag = header_value.rsplit("#", 1)
    if not location or not tag:
        return error_result("missing-tag", "section header must include path and tag")
    operations: list[LineOperation] = []
    index = 1
    while index < len(lines):
        line = lines[index]
        if not line:
            index += 1
            continue
        parsed = parse_operation_header(line)
        if parsed is None:
            return error_result(
                "unknown-operation", f"unknown hashline operation: {line}"
            )
        if not isinstance(parsed, LineOperation):
            return parsed
        index += 1
        body: list[str] = []
        while (
            index < len(lines)
            and parse_operation_header(lines[index], quiet=True) is None
        ):
            body_line = lines[index]
            if not body_line.startswith("+"):
                return error_result(
                    "invalid-body-line",
                    "hashline edit body rows must start with +",
                )
            body.append(f"{body_line[1:]}\n")
            index += 1
        if parsed.kind != "DEL" and not body:
            return error_result("missing-body", f"{parsed.kind} requires + body rows")
        if parsed.kind == "DEL" and body:
            return error_result("invalid-body-line", "DEL does not accept body rows")
        operations.append(
            LineOperation(
                kind=parsed.kind,
                start=parsed.start,
                end=parsed.end,
                body=tuple(body),
            )
        )
    if not operations:
        return error_result("missing-operation", "hashline edit has no operations")
    return HashlineEdit(location=location, tag=tag, operations=tuple(operations))


def parse_operation_header(
    line: str, *, quiet: bool = False
) -> LineOperation | dict[str, Any] | None:
    swap = SWAP_RE.match(line)
    if swap is not None:
        start = int(swap.group("start"))
        end = int(swap.group("end"))
        if start > end:
            return error_result("invalid-range", "operation range is out of order")
        return LineOperation("SWAP", start, end)
    delete = DEL_RE.match(line)
    if delete is not None:
        start = int(delete.group("start"))
        end = int(delete.group("end"))
        if start > end:
            return error_result("invalid-range", "operation range is out of order")
        return LineOperation("DEL", start, end)
    insert = INS_RE.match(line)
    if insert is not None:
        target = int(insert.group("line"))
        return LineOperation(insert.group("kind"), target, target)
    if quiet:
        return None
    return error_result("unknown-operation", f"unknown hashline operation: {line}")


def apply_line_operations(
    text: str, operations: tuple[LineOperation, ...]
) -> str | dict[str, Any]:
    lines = text.splitlines(keepends=True)
    for operation in operations:
        error = validate_operation(operation, len(lines))
        if error is not None:
            return error
    updated = list(lines)
    for operation in sorted(operations, key=lambda op: op.start, reverse=True):
        start = operation.start - 1
        end = operation.end
        if operation.kind == "SWAP":
            updated[start:end] = list(operation.body)
        elif operation.kind == "DEL":
            del updated[start:end]
        elif operation.kind == "INS.PRE":
            updated[start:start] = list(operation.body)
        elif operation.kind == "INS.POST":
            updated[end:end] = list(operation.body)
    return "".join(updated)


def validate_operation(
    operation: LineOperation, line_count: int
) -> dict[str, Any] | None:
    if operation.kind == "INS.POST":
        valid = 1 <= operation.start <= line_count
    elif operation.kind == "INS.PRE":
        valid = 1 <= operation.start <= max(line_count, 1)
    else:
        valid = 1 <= operation.start <= operation.end <= line_count
    if not valid:
        return error_result("line-out-of-range", "operation refers to a missing line")
    return None


def tag_for_text(text: str) -> str:
    return content_hash(text).split(":", 1)[1][:8]


def operation_metadata(operation: LineOperation) -> dict[str, Any]:
    metadata = {
        "kind": operation.kind,
        "start": operation.start,
        "end": operation.end,
    }
    if operation.body:
        metadata["lines"] = len(operation.body)
    return metadata


def stage_patch(patch: str, reason: str) -> dict[str, Any]:
    path = write_temp("zeta-edit-", ".patch", patch)
    return proposed_command_effect(
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
