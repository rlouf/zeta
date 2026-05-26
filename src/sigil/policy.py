"""Execution policy gates for semantic operator output."""

from __future__ import annotations

import re
import shlex
from dataclasses import asdict, dataclass
from typing import Literal

ActionClass = Literal[
    "stdout",
    "execute",
    "file_write",
    "network",
    "delete",
    "privileged",
]
PolicyStatus = Literal["preview", "allowed"]

WRITE_COMMANDS = {
    "cp",
    "install",
    "mkdir",
    "mv",
    "patch",
    "perl",
    "sed",
    "tee",
    "touch",
}
NETWORK_COMMANDS = {
    "brew",
    "curl",
    "git",
    "gh",
    "hf",
    "npm",
    "pip",
    "pnpm",
    "ssh",
    "uv",
    "wget",
    "yarn",
}
DELETE_COMMANDS = {"git", "rm", "rmdir", "trash"}
PRIVILEGED_COMMANDS = {"chmod", "chown", "sudo", "su"}
NETWORK_GIT_SUBCOMMANDS = {"clone", "fetch", "pull", "push", "submodule"}
DESTRUCTIVE_GIT_SUBCOMMANDS = {"clean", "reset"}


@dataclass(frozen=True)
class ActionClassification:
    """Action classes inferred from operator output."""

    classes: tuple[ActionClass, ...]
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable representation."""
        return asdict(self)


@dataclass(frozen=True)
class ExecutionPolicy:
    """Explicit execution policy selected at the CLI boundary."""

    dry_run: bool = False
    confirm_execution: bool = False

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable representation."""
        return asdict(self)


@dataclass(frozen=True)
class PolicyDecision:
    """Policy decision for an operator result."""

    status: PolicyStatus
    message: str
    classification: ActionClassification

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable representation."""
        return {
            "status": self.status,
            "message": self.message,
            "classification": self.classification.to_dict(),
        }


def classify_output(text: str) -> ActionClassification:
    """Classify operator output into broad action classes."""
    classes: set[ActionClass] = {"stdout"}
    reasons: list[str] = []
    if looks_like_patch(text):
        classes.add("file_write")
        reasons.append("unified diff or patch-like output")
    for line in command_lines(text):
        classify_command_line(line, classes, reasons)
    return ActionClassification(
        classes=tuple(sorted(classes)),
        reasons=tuple(dict.fromkeys(reasons)),
    )


def evaluate_policy(
    *,
    glyph: str,
    depth: int,
    output: str,
    policy: ExecutionPolicy,
) -> PolicyDecision:
    """Classify an operator result and describe how it will be handled."""
    classification = classify_output(output)
    if policy.dry_run:
        return PolicyDecision(
            status="preview",
            message=f"{glyph} dry-run: classified output and skipped execution",
            classification=classification,
        )
    if glyph.startswith(",") and depth >= 2:
        return PolicyDecision(
            status="allowed",
            message=f"{glyph} executes the generated command",
            classification=classification,
        )
    return PolicyDecision(
        status="preview",
        message="stdout-only preview",
        classification=classification,
    )


def looks_like_patch(text: str) -> bool:
    """Return true when text appears to be a unified diff."""
    return bool(
        re.search(r"(?m)^---\s+\S+", text)
        and re.search(r"(?m)^\+\+\+\s+\S+", text)
        and re.search(r"(?m)^@@", text)
    )


def command_lines(text: str) -> list[str]:
    """Extract shell-looking command lines from model output."""
    lines: list[str] = []
    in_fence = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if not line:
            continue
        if line.startswith(("#", "//", "---", "+++", "@@")):
            continue
        if line.startswith("$ "):
            line = line[2:].strip()
        if in_fence or looks_like_command_line(line):
            lines.append(line)
    return lines


def looks_like_command_line(line: str) -> bool:
    """Return true when a line plausibly starts with a shell command."""
    try:
        parts = shlex.split(line, comments=False, posix=True)
    except ValueError:
        return False
    if not parts:
        return False
    command = parts[0].split("/")[-1]
    known = WRITE_COMMANDS | NETWORK_COMMANDS | DELETE_COMMANDS | PRIVILEGED_COMMANDS
    return command in known or any(token in {">", ">>", "|"} for token in parts)


def classify_command_line(
    line: str,
    classes: set[ActionClass],
    reasons: list[str],
) -> None:
    """Add action classes inferred from one shell command line."""
    classes.add("execute")
    reasons.append(f"shell command: {line}")
    try:
        parts = shlex.split(line, comments=False, posix=True)
    except ValueError:
        return
    if not parts:
        return
    command = parts[0].split("/")[-1]
    if command == "sudo" and len(parts) > 1:
        nested = " ".join(shlex.quote(part) for part in parts[1:])
        classify_command_line(nested, classes, reasons)
    if command in WRITE_COMMANDS or any(token in {">", ">>"} for token in parts):
        classes.add("file_write")
        reasons.append(f"file write command: {command}")
    if command in NETWORK_COMMANDS:
        if command != "git" or git_subcommand(parts) in NETWORK_GIT_SUBCOMMANDS:
            classes.add("network")
            reasons.append(f"network-capable command: {command}")
    if command in DELETE_COMMANDS:
        if command != "git" or git_subcommand(parts) in DESTRUCTIVE_GIT_SUBCOMMANDS:
            classes.add("delete")
            reasons.append(f"deletion-capable command: {command}")
    if command in PRIVILEGED_COMMANDS:
        classes.add("privileged")
        reasons.append(f"privileged command: {command}")
    if command in {"sed", "perl"} and any(
        flag in parts for flag in ("-i", "-pi", "-0pi")
    ):
        classes.add("file_write")
        reasons.append(f"in-place edit command: {command}")


def git_subcommand(parts: list[str]) -> str:
    """Return the first non-option git subcommand."""
    for part in parts[1:]:
        if not part.startswith("-"):
            return part
    return ""
