"""Shared primitives for built-in Zeta tools."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ...protocols import shell_handoff_tool_result

EffectKind = Literal["read", "write", "delete", "execute", "search"]
Resource = Literal["path", "process", "session"]


@dataclass(frozen=True)
class ToolSpec:
    """Metadata for one Zeta tool."""

    name: str
    description: str
    schema: dict[str, Any]
    interactive: bool = False

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
        }


ToolFunction = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class ToolImpl:
    """Executable implementation for one Zeta tool."""

    spec: ToolSpec
    analyze: ToolFunction
    run: ToolFunction


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


def write_temp(prefix: str, suffix: str, content: str) -> Path:
    fd, raw_path = tempfile.mkstemp(prefix=prefix, suffix=suffix)
    path = Path(raw_path)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    return path
