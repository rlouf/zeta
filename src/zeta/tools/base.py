"""Shared primitives for built-in Zeta tools."""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, get_args

EffectKind = Literal["read", "write", "delete", "execute", "search"]

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
            "interactive": self.interactive,
            "effects": list(self.effects),
        }


ToolFunction = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class ToolImpl:
    """Executable implementation for one Zeta tool."""

    spec: ToolSpec
    run: ToolFunction
    stage: ToolFunction | None = None


def diagnostic(
    code: str, message: str, *, severity: str = "unsupported"
) -> dict[str, str]:
    return {"code": code, "message": message, "severity": severity}


def error_result(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def proposed_command_effect(
    command: str, reason: str, *, artifact: str | None = None
) -> dict[str, Any]:
    effect = {
        "kind": "command",
        "status": "proposed",
        "command": command,
        "reason": reason,
    }
    if artifact is not None:
        effect["artifact"] = artifact
    return {"ok": True, "effect": effect}


def proposed_effect(result: dict[str, Any]) -> dict[str, Any] | None:
    if result.get("ok") is not True:
        return None
    effect = effect_payload(result)
    if effect is None or effect.get("status") != "proposed":
        return None
    return effect


def effect_resolution(result: dict[str, Any]) -> dict[str, Any] | None:
    effect = effect_payload(result)
    if effect is None:
        return None
    status = effect.get("status")
    if status not in {"resolved", "cancelled"}:
        return None
    return effect


def effect_payload(result: dict[str, Any]) -> dict[str, Any] | None:
    effect = result.get("effect")
    if not isinstance(effect, dict):
        return None
    return effect


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
