"""Execution policy gates for semantic operator output."""

from __future__ import annotations

import shlex
from dataclasses import asdict, dataclass
from typing import Literal

ActionLabel = Literal[
    "local",
    "read-only",
    "write",
    "network",
    "publish",
    "delete",
    "privileged",
    "focused",
    "high-risk",
]
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
PUBLISH_GIT_SUBCOMMANDS = {"push"}
READ_ONLY_UV_RUNNERS = {"pytest", "ruff", "ty"}


@dataclass(frozen=True)
class ActionClassification:
    """Action classes inferred from operator output."""

    classes: tuple[ActionClass, ...]
    reasons: tuple[str, ...]
    labels: tuple[ActionLabel, ...]

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
    labels: set[ActionLabel] = set()
    lines = command_lines(text)
    for line in lines:
        classify_command_line(line, classes, reasons)
        labels.update(command_labels(line))
    if not labels:
        labels.add("read-only")
    if not {"network", "publish"} & labels:
        labels.add("local")
    return ActionClassification(
        classes=tuple(sorted(classes)),
        reasons=tuple(dict.fromkeys(reasons)),
        labels=ordered_labels(labels),
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
    if glyph.startswith(",") and depth > 1:
        return PolicyDecision(
            status="preview",
            message=f"{glyph} is handled by the Pi act runner",
            classification=classification,
        )
    if depth == 3:
        return PolicyDecision(
            status="preview",
            message=f"{glyph} is handled by its depth-three route",
            classification=classification,
        )
    return PolicyDecision(
        status="preview",
        message="stdout-only preview",
        classification=classification,
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
    if is_write_command(command, parts):
        classes.add("file_write")
        reasons.append(f"file write command: {command}")
    if is_network_command(command, parts):
        classes.add("network")
        reasons.append(f"network-capable command: {command}")
    if is_delete_command(command, parts):
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


def command_labels(line: str) -> set[ActionLabel]:
    """Return user-facing trust labels for one shell command."""
    try:
        parts = shlex.split(line, comments=False, posix=True)
    except ValueError:
        return {"local", "read-only"}
    if not parts:
        return {"local", "read-only"}

    command = parts[0].split("/")[-1]
    if command == "sudo" and len(parts) > 1:
        nested = " ".join(shlex.quote(part) for part in parts[1:])
        labels = command_labels(nested)
        labels.add("privileged")
        labels.add("high-risk")
        return labels

    labels: set[ActionLabel] = set()
    if is_publish_command(command, parts):
        labels.update({"network", "publish", "high-risk"})
    elif is_network_command(command, parts):
        labels.add("network")
    else:
        labels.add("local")

    if is_publish_command(command, parts):
        pass
    elif is_delete_command(command, parts):
        labels.update({"delete", "high-risk"})
    elif command in PRIVILEGED_COMMANDS:
        labels.update({"privileged", "high-risk"})
    elif is_write_command(command, parts):
        labels.add("write")
    else:
        labels.add("read-only")

    if is_focused_command(command, parts):
        labels.add("focused")
    return labels


def is_publish_command(command: str, parts: list[str]) -> bool:
    """Return true when a command publishes local state elsewhere."""
    return command == "git" and git_subcommand(parts) in PUBLISH_GIT_SUBCOMMANDS


def is_network_command(command: str, parts: list[str]) -> bool:
    """Return true when a command requires or commonly reaches network."""
    if command == "git":
        return git_subcommand(parts) in NETWORK_GIT_SUBCOMMANDS
    if command == "uv" and is_read_only_uv_run(parts):
        return False
    return command in NETWORK_COMMANDS


def is_delete_command(command: str, parts: list[str]) -> bool:
    """Return true when a command deletes local or remote state."""
    if command == "git":
        return git_subcommand(parts) in DESTRUCTIVE_GIT_SUBCOMMANDS
    return command in DELETE_COMMANDS


def is_write_command(command: str, parts: list[str]) -> bool:
    """Return true when a command mutates local filesystem state."""
    if any(token in {">", ">>"} for token in parts):
        return True
    if command in {"sed", "perl"} and any(
        flag in parts for flag in ("-i", "-pi", "-0pi")
    ):
        return True
    return command in WRITE_COMMANDS


def is_focused_command(command: str, parts: list[str]) -> bool:
    """Return true when a command targets a narrow file/path/test scope."""
    if command == "uv" and is_read_only_uv_run(parts):
        return len(parts) > 3
    if command == "pytest":
        return len(parts) > 1
    return any(
        part.startswith(("tests/", "src/", "./tests/", "./src/"))
        or part.endswith((".py", ".rs", ".js", ".ts", ".tsx", ".md"))
        for part in parts[1:]
    )


def is_read_only_uv_run(parts: list[str]) -> bool:
    """Return true for `uv run` commands that only run local checks."""
    return (
        len(parts) >= 3
        and parts[1] == "run"
        and parts[2].split("/")[-1] in READ_ONLY_UV_RUNNERS
    )


def ordered_labels(labels: set[ActionLabel]) -> tuple[ActionLabel, ...]:
    """Return labels in stable display order."""
    order: tuple[ActionLabel, ...] = (
        "local",
        "network",
        "read-only",
        "write",
        "publish",
        "delete",
        "privileged",
        "focused",
        "high-risk",
    )
    return tuple(label for label in order if label in labels)


def git_subcommand(parts: list[str]) -> str:
    """Return the first non-option git subcommand."""
    for part in parts[1:]:
        if not part.startswith("-"):
            return part
    return ""
