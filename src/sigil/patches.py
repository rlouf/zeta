"""Patch preview storage and explicit application workflow."""

from __future__ import annotations

import os
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .policy import PolicyDecision, looks_like_patch
from .security import create_trust_metadata, normalize_trust_record
from .state import append_event, read_json, write_json

LAST_PATCH = "last-patch.json"
MAX_PATCH_SNIPPET_CHARS = 4000


@dataclass(frozen=True)
class PatchCommandResult:
    """Result of a git-apply patch command."""

    ok: bool
    status: int
    command: tuple[str, ...]
    cwd: str
    stdout: str
    stderr: str

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable representation."""
        return asdict(self)


def store_patch_preview(
    *,
    patch_text: str,
    operator: dict[str, object],
    operator_event: dict[str, Any],
    decision: PolicyDecision,
    security: dict[str, Any],
) -> dict[str, Any] | None:
    """Store a repair operator's unified diff for explicit later application."""
    if not looks_like_patch(patch_text):
        return None
    source_id = str(operator_event.get("id") or "")
    patch_security = {
        **security,
        "inputs": [source_id] if source_id else [],
    }
    event = append_event(
        {
            "type": "patch_preview_stored",
            "operator": operator,
            "operator_event_id": source_id,
            "patch_snippet": patch_text[:MAX_PATCH_SNIPPET_CHARS],
            "decision": decision.to_dict(),
            **patch_security,
        }
    )
    record = {
        "patch": patch_text,
        "cwd": str(operator_event.get("cwd") or os.getcwd()),
        "operator": operator,
        "operator_event_id": source_id,
        "event_id": event["id"],
        "decision": decision.to_dict(),
        **patch_security,
    }
    write_json(LAST_PATCH, record)
    return event


def last_patch() -> dict[str, Any]:
    """Load the last patch preview for this session."""
    record = read_json(LAST_PATCH)
    if not isinstance(record, dict) or not isinstance(record.get("patch"), str):
        raise ValueError("no patch preview recorded")
    return normalize_trust_record(record)


def patch_cwd(record: dict[str, Any]) -> str:
    """Return the patch working directory, falling back to the current cwd."""
    cwd = str(record.get("cwd") or os.getcwd())
    if Path(cwd).is_dir():
        return cwd
    return os.getcwd()


def check_patch(record: dict[str, Any]) -> PatchCommandResult:
    """Run git apply --check for a stored patch preview."""
    return run_git_apply(record, check_only=True)


def apply_patch(record: dict[str, Any]) -> PatchCommandResult:
    """Apply a stored patch preview with git apply."""
    return run_git_apply(record, check_only=False)


def run_git_apply(record: dict[str, Any], *, check_only: bool) -> PatchCommandResult:
    """Run git apply in the patch preview's original working directory."""
    command = ["git", "apply"]
    if check_only:
        command.append("--check")
    cwd = patch_cwd(record)
    proc = subprocess.run(
        command,
        cwd=cwd,
        input=str(record["patch"]),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return PatchCommandResult(
        ok=proc.returncode == 0,
        status=proc.returncode,
        command=tuple(command),
        cwd=cwd,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def record_patch_check(
    record: dict[str, Any],
    result: PatchCommandResult,
) -> dict[str, Any]:
    """Record a patch validation attempt."""
    security = normalize_trust_record(record)
    patch_event_id = str(record.get("event_id") or "")
    return append_event(
        {
            "type": "patch_checked",
            "patch_event_id": patch_event_id,
            "status": result.status,
            "command": list(result.command),
            "stdout_snippet": result.stdout[:MAX_PATCH_SNIPPET_CHARS],
            "stderr_snippet": result.stderr[:MAX_PATCH_SNIPPET_CHARS],
            "glyph": security["glyph"],
            "inputs": [patch_event_id] if patch_event_id else [],
            "integrity": security["integrity"],
            "capability": security["capability"],
            "taint": security["taint"],
            "provisional": security["provisional"],
        }
    )


def record_patch_apply(
    record: dict[str, Any],
    result: PatchCommandResult,
) -> dict[str, Any]:
    """Record an explicit patch application attempt."""
    source = normalize_trust_record(record)
    patch_event_id = str(record.get("event_id") or "")
    security = create_trust_metadata(
        glyph=str(record.get("glyph") or "^"),
        integrity="local_model",
        capability="write_boxed",
        taint=source["taint"],
        inputs=[patch_event_id] if patch_event_id else [],
        input_records=[source],
        fresh_human=True,
    )
    return append_event(
        {
            "type": "patch_applied" if result.ok else "patch_apply_failed",
            "patch_event_id": patch_event_id,
            "status": result.status,
            "command": list(result.command),
            "stdout_snippet": result.stdout[:MAX_PATCH_SNIPPET_CHARS],
            "stderr_snippet": result.stderr[:MAX_PATCH_SNIPPET_CHARS],
            **security,
        }
    )
