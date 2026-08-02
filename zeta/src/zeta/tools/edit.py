"""Exact-replacement edit tool implementation."""

import difflib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zeta.capabilities.delivery import (
    change_hashes,
    content_hash,
    short_tag,
    write_temp,
)
from zeta.capabilities.paths import resolve_path
from zeta.capabilities.registry import error_result
from zeta.capabilities.types import Capability, CapabilityId

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
    },
}

SPEC = Capability(
    CapabilityId("zeta", "edit"),
    "Edit a file. Prefer tagged input from read: [path#tag] plus SWAP, DEL, INS.PRE, or INS.POST line operations.",
    SCHEMA,
    delivery_semantics="idempotent_with_key",
)

PATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["patch"],
    "properties": {"patch": {"type": "string", "minLength": 1}},
}

PATCH_SPEC = Capability(
    CapabilityId("zeta", "patch"),
    "Apply a patch to files.",
    PATCH_SCHEMA,
    delivery_semantics="idempotent_with_key",
)

HEADER_RE = re.compile(r"^\[(?P<path>.+)\]$")
SWAP_RE = re.compile(r"^SWAP (?P<start>[1-9][0-9]*)\.\.(?P<end>[1-9][0-9]*):$")
DEL_RE = re.compile(r"^DEL (?P<start>[1-9][0-9]*)\.\.(?P<end>[1-9][0-9]*)$")
INS_RE = re.compile(r"^(?P<kind>INS\.PRE|INS\.POST) (?P<line>[1-9][0-9]*):$")
PATCH_SECTION_RE = re.compile(
    r"^\*\*\* (?P<kind>Add|Delete|Update) File: (?P<path>.+)$"
)
PATCH_MOVE_RE = re.compile(r"^\*\*\* Move to: (?P<path>.+)$")


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
class PatchSection:
    kind: str
    path: str
    lines: tuple[str, ...]
    move_to: str | None = None


@dataclass(frozen=True)
class PatchChange:
    kind: str
    label: str
    path: Path
    before: str | None
    after: str | None
    move_label: str | None = None
    move_path: Path | None = None


@dataclass(frozen=True)
class PatchHunk:
    context: str
    lines: tuple[str, ...]
    end_of_file: bool = False


def run_patch(params: dict[str, Any]) -> dict[str, Any]:
    patch = params.get("patch")
    if not isinstance(patch, str) or not patch:
        return error_result("missing-patch", "missing patch")
    prepared = prepare_patch(patch)
    if isinstance(prepared, dict):
        return prepared
    failure = commit_patch(prepared)
    if failure is not None:
        return failure
    artifact = write_temp("zeta-patch-", ".patch", patch)
    return {
        "ok": True,
        "content": [{"type": "text", "text": "applied patch"}],
        "metadata": {
            "artifact": str(artifact),
            "files": [change.move_label or change.label for change in prepared],
            "changes": [patch_change_metadata(change) for change in prepared],
        },
    }


def prepare_patch(patch: str) -> tuple[PatchChange, ...] | dict[str, Any]:
    sections = parse_patch_sections(patch)
    if isinstance(sections, dict):
        return sections
    changes: list[PatchChange] = []
    used_paths: set[Path] = set()
    for section in sections:
        prepared = prepare_patch_section(section)
        if not isinstance(prepared, PatchChange):
            return prepared
        conflict = patch_path_conflict(prepared, used_paths)
        if conflict is not None:
            return conflict
        changes.append(prepared)
    return tuple(changes)


def parse_patch_sections(patch: str) -> tuple[PatchSection, ...] | dict[str, Any]:
    lines = patch.splitlines()
    if len(lines) < 3 or lines[0] != "*** Begin Patch":
        return error_result("invalid-patch", "patch must start with *** Begin Patch")
    if lines[-1] != "*** End Patch":
        return error_result("invalid-patch", "patch must end with *** End Patch")
    sections: list[PatchSection] = []
    index = 1
    while index < len(lines) - 1:
        header = PATCH_SECTION_RE.match(lines[index])
        if header is None:
            return error_result(
                "invalid-patch",
                f"invalid patch section: {lines[index]}",
            )
        kind = header.group("kind").lower()
        path = header.group("path")
        index += 1
        move_to = None
        if index < len(lines) - 1:
            move = PATCH_MOVE_RE.match(lines[index])
            if move is not None:
                if kind != "update":
                    return error_result(
                        "invalid-patch",
                        "only an update can move a file",
                    )
                move_to = move.group("path")
                index += 1
        body: list[str] = []
        while index < len(lines) - 1 and PATCH_SECTION_RE.match(lines[index]) is None:
            if lines[index].startswith("*** ") and lines[index] != "*** End of File":
                return error_result(
                    "invalid-patch",
                    f"invalid patch line: {lines[index]}",
                )
            body.append(lines[index])
            index += 1
        sections.append(PatchSection(kind, path, tuple(body), move_to))
    if not sections:
        return error_result("invalid-patch", "patch has no file sections")
    return tuple(sections)


def prepare_patch_section(section: PatchSection) -> PatchChange | dict[str, Any]:
    resolved = resolve_patch_path(section.path)
    if isinstance(resolved, dict):
        return resolved
    if section.kind == "add":
        return prepare_add_patch(section, resolved)
    if section.kind == "delete":
        return prepare_delete_patch(section, resolved)
    return prepare_update_patch(section, resolved)


def resolve_patch_path(path: str) -> Path | dict[str, Any]:
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts:
        return error_result(
            "invalid-patch-path",
            f"patch path must stay in the base directory: {path}",
        )
    return resolve_path(path)


def prepare_add_patch(
    section: PatchSection,
    path: Path,
) -> PatchChange | dict[str, Any]:
    if section.move_to is not None:
        return error_result("invalid-patch", "an add cannot move a file")
    if path.exists():
        return error_result(
            "patch-target-exists", f"file already exists: {section.path}"
        )
    if not path.parent.is_dir():
        return error_result(
            "patch-parent-missing",
            f"parent directory does not exist: {path.parent}",
        )
    content = added_file_content(section.lines)
    if isinstance(content, dict):
        return content
    return PatchChange("add", section.path, path, None, content)


def added_file_content(lines: tuple[str, ...]) -> str | dict[str, Any]:
    content: list[str] = []
    for line in lines:
        if not line.startswith("+"):
            return error_result(
                "invalid-patch",
                "each added file line must start with +",
            )
        content.append(f"{line[1:]}\n")
    return "".join(content)


def prepare_delete_patch(
    section: PatchSection,
    path: Path,
) -> PatchChange | dict[str, Any]:
    if section.lines or section.move_to is not None:
        return error_result("invalid-patch", "a delete cannot contain patch lines")
    before = read_patch_file(path, section.path)
    if isinstance(before, dict):
        return before
    return PatchChange("delete", section.path, path, before, None)


def prepare_update_patch(
    section: PatchSection,
    path: Path,
) -> PatchChange | dict[str, Any]:
    before = read_patch_file(path, section.path)
    if isinstance(before, dict):
        return before
    hunks = parse_patch_hunks(section.lines)
    if isinstance(hunks, dict):
        return hunks
    after = apply_patch_hunks(before, hunks, section.path)
    if isinstance(after, dict):
        return after
    move_path = None
    if section.move_to is not None:
        move_path = resolve_patch_path(section.move_to)
        if isinstance(move_path, dict):
            return move_path
        if move_path.exists():
            return error_result(
                "patch-target-exists",
                f"file already exists: {section.move_to}",
            )
        if not move_path.parent.is_dir():
            return error_result(
                "patch-parent-missing",
                f"parent directory does not exist: {move_path.parent}",
            )
    if after == before and move_path is None:
        return error_result("empty-patch", f"patch does not change: {section.path}")
    return PatchChange(
        "update",
        section.path,
        path,
        before,
        after,
        move_label=section.move_to,
        move_path=move_path,
    )


def read_patch_file(path: Path, label: str) -> str | dict[str, Any]:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return error_result("not-utf8", f"file is not valid UTF-8: {label}")
    except OSError as exc:
        return error_result("patch-read-failed", f"could not read {label}: {exc}")


def parse_patch_hunks(lines: tuple[str, ...]) -> tuple[PatchHunk, ...] | dict[str, Any]:
    hunks: list[PatchHunk] = []
    index = 0
    while index < len(lines):
        header = lines[index]
        if not header.startswith("@@"):
            return error_result("invalid-patch", "each update hunk must start with @@")
        context = header[2:].strip()
        index += 1
        body: list[str] = []
        end_of_file = False
        while index < len(lines) and not lines[index].startswith("@@"):
            line = lines[index]
            if line == "*** End of File":
                end_of_file = True
                index += 1
                break
            if not line.startswith((" ", "+", "-")):
                return error_result(
                    "invalid-patch",
                    "update lines must start with a space, +, or -",
                )
            body.append(line)
            index += 1
        if not body:
            return error_result("invalid-patch", "update hunk has no lines")
        hunks.append(PatchHunk(context, tuple(body), end_of_file))
    if not hunks:
        return error_result("invalid-patch", "update has no hunks")
    return tuple(hunks)


def apply_patch_hunks(
    before: str,
    hunks: tuple[PatchHunk, ...],
    label: str,
) -> str | dict[str, Any]:
    current = before.splitlines(keepends=True)
    cursor = 0
    for hunk in hunks:
        anchor = patch_context_anchor(current, hunk.context, cursor, label)
        if isinstance(anchor, dict):
            return anchor
        cursor = anchor
        old_lines = [f"{line[1:]}\n" for line in hunk.lines if not line.startswith("+")]
        new_lines = [f"{line[1:]}\n" for line in hunk.lines if not line.startswith("-")]
        match = patch_hunk_match(current, old_lines, cursor, hunk.end_of_file, label)
        if isinstance(match, dict):
            return match
        current[match : match + len(old_lines)] = new_lines
        cursor = match + len(new_lines)
    return "".join(current)


def patch_context_anchor(
    lines: list[str],
    context: str,
    cursor: int,
    label: str,
) -> int | dict[str, Any]:
    if not context:
        return cursor
    matches = [
        index
        for index in range(cursor, len(lines))
        if lines[index].rstrip("\r\n") == context
    ]
    if len(matches) != 1:
        return patch_match_error(label, context, len(matches))
    return matches[0] + 1


def patch_hunk_match(
    lines: list[str],
    old_lines: list[str],
    cursor: int,
    end_of_file: bool,
    label: str,
) -> int | dict[str, Any]:
    if not old_lines:
        return len(lines) if end_of_file else cursor
    matches = [
        index
        for index in range(cursor, len(lines) - len(old_lines) + 1)
        if lines[index : index + len(old_lines)] == old_lines
        and (not end_of_file or index + len(old_lines) == len(lines))
    ]
    if len(matches) != 1:
        context = "".join(old_lines).rstrip("\n")
        return patch_match_error(label, context, len(matches))
    return matches[0]


def patch_match_error(label: str, context: str, count: int) -> dict[str, Any]:
    if count == 0:
        return error_result(
            "patch-context-mismatch",
            f"patch context was not found in {label}: {context}",
        )
    return error_result(
        "patch-context-ambiguous",
        f"patch context matched more than once in {label}: {context}",
    )


def patch_path_conflict(
    change: PatchChange,
    used_paths: set[Path],
) -> dict[str, Any] | None:
    paths = {change.path}
    if change.move_path is not None:
        paths.add(change.move_path)
    conflict = paths & used_paths
    if conflict:
        return error_result(
            "patch-path-conflict",
            f"patch uses a file more than once: {next(iter(conflict))}",
        )
    used_paths.update(paths)
    return None


def commit_patch(changes: tuple[PatchChange, ...]) -> dict[str, Any] | None:
    for change in changes:
        try:
            if change.kind == "delete":
                change.path.unlink()
            elif change.move_path is not None:
                change.move_path.write_text(change.after or "", encoding="utf-8")
                change.path.unlink()
            else:
                change.path.write_text(change.after or "", encoding="utf-8")
        except OSError as exc:
            return error_result(
                "patch-write-failed",
                f"could not apply patch to {change.label}: {exc}",
            )
    return None


def patch_change_metadata(change: PatchChange) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "operation": change.kind,
        "path": change.label,
    }
    if change.before is not None:
        metadata["before_hash"] = content_hash(change.before)
    if change.after is not None:
        metadata["after_hash"] = content_hash(change.after)
    if change.move_label is not None:
        metadata["move_to"] = change.move_label
    return metadata


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
    location = str(resolve_path(location))
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
        metadata={"operation": "exact_replace"},
    )


def prepare_hashline_edit(params: dict[str, Any]) -> PreparedEdit | dict[str, Any]:
    parsed = parse_hashline_input(str(params.get("input") or ""))
    if not isinstance(parsed, HashlineEdit):
        return parsed
    location = str(resolve_path(parsed.location))
    path = Path(location)
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
    patch = replacement_patch(location, text, updated)
    if not patch:
        return error_result("empty-edit", "replacement did not change the file")
    return PreparedEdit(
        location=location,
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
    """Parse Zeta's small OMP hashline-inspired tagged edit format."""
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
    overlap = overlapping_range_error(operations)
    if overlap is not None:
        return overlap
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


def overlapping_range_error(
    operations: tuple[LineOperation, ...],
) -> dict[str, Any] | None:
    """Reject SWAP/DEL operations whose consumed line ranges overlap.

    Operations are applied back-to-front assuming disjoint ranges; overlapping
    ranges would silently corrupt the result rather than being rejected.
    """
    spans = sorted(
        (operation.start, operation.end)
        for operation in operations
        if operation.kind in {"SWAP", "DEL"}
    )
    for (_, prev_end), (next_start, _) in zip(spans, spans[1:], strict=False):
        if next_start <= prev_end:
            return error_result(
                "overlapping-operations",
                "operations touch overlapping line ranges",
            )
    return None


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
    return short_tag(content_hash(text))


def operation_metadata(operation: LineOperation) -> dict[str, Any]:
    metadata = {
        "kind": operation.kind,
        "start": operation.start,
        "end": operation.end,
    }
    if operation.body:
        metadata["lines"] = len(operation.body)
    return metadata


def replacement_patch(location: str, old: str, new: str) -> str:
    before = old.splitlines(keepends=True)
    after = new.splitlines(keepends=True)
    lines = difflib.unified_diff(
        before,
        after,
        fromfile=patch_label(location, "a"),
        tofile=patch_label(location, "b"),
    )
    return "".join(mark_diff_lines_without_trailing_newline(lines))


def mark_diff_lines_without_trailing_newline(lines: Iterable[str]) -> list[str]:
    marked = []
    for line in lines:
        if line.endswith("\n"):
            marked.append(line)
        else:
            marked.append(f"{line}\n")
            marked.append("\\ No newline at end of file\n")
    return marked


def patch_label(path: str, prefix: str) -> str:
    if path.startswith("/"):
        return path
    return f"{prefix}/{path}"
