"""Shared primitives for built-in Zeta tools."""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, get_args

from ...protocols import shell_handoff_tool_result

EffectKind = Literal["read", "write", "delete", "execute", "search"]
Resource = Literal["path", "process", "session"]

EFFECT_KINDS = frozenset(get_args(EffectKind))
READ_ONLY_EFFECT_KINDS = frozenset({"read", "search"})


@dataclass(frozen=True)
class ToolSpec:
    """Metadata for one Zeta tool."""

    name: str
    description: str
    schema: dict[str, Any]
    interactive: bool = False
    effects: tuple[EffectKind, ...] = ()

    def mutates(self) -> bool:
        """Whether the tool declares effects beyond reading.

        Undeclared effects count as mutating so an unannotated tool can
        never run unreviewed in propose mode.
        """
        if not self.effects:
            return True
        return any(kind not in READ_ONLY_EFFECT_KINDS for kind in self.effects)

    def metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "schema": self.schema,
            "security": {
                "analyzer": "self",
                "analysis_schema": "zeta.analysis.v1",
            },
            "interactive": self.interactive,
            "effects": list(self.effects),
        }


ToolFunction = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class ToolImpl:
    """Executable implementation for one Zeta tool."""

    spec: ToolSpec
    analyze: ToolFunction
    run: ToolFunction
    stage: ToolFunction | None = None


def analysis(
    *,
    valid: bool = True,
    resolved: bool = True,
    effects: list[dict[str, Any]] | None = None,
    diagnostics: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "valid": valid,
        "resolved": resolved,
        "effects": effects or [],
        "diagnostics": diagnostics or [],
    }


def effect(
    kind: EffectKind,
    target: str,
    *,
    resource: Resource = "path",
    certainty: str = "certain",
) -> dict[str, str]:
    return {
        "kind": kind,
        "resource": resource,
        "target": target,
        "certainty": certainty,
    }


def diagnostic(
    code: str, message: str, *, severity: str = "unsupported"
) -> dict[str, str]:
    return {"code": code, "message": message, "severity": severity}


def missing(field: str) -> dict[str, Any]:
    return analysis(
        valid=False,
        resolved=False,
        diagnostics=[diagnostic("missing-field", f"missing {field}", severity="error")],
    )


def error_result(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def handoff(
    command: str, reason: str, *, artifact: str | None = None
) -> dict[str, Any]:
    return shell_handoff_tool_result(command, reason, artifact=artifact)


def content_hash(data: bytes | str) -> str:
    """Return the sha256 content address of file bytes or UTF-8 text."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return "sha256:" + hashlib.sha256(data).hexdigest()


def file_content_hash(path: str | Path) -> str | None:
    """Return the content address of a file, or None if it cannot be read."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    return content_hash(data)


def change_hashes(path: str, content: str) -> dict[str, str]:
    """Hash the file as it stands (when readable) and the content replacing it."""
    hashes = {"after_hash": content_hash(content)}
    before_hash = file_content_hash(path)
    if before_hash is not None:
        hashes["before_hash"] = before_hash
    return hashes


def write_temp(prefix: str, suffix: str, content: str) -> Path:
    fd, raw_path = tempfile.mkstemp(prefix=prefix, suffix=suffix)
    path = Path(raw_path)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    return path
