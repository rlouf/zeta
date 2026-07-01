"""Shared protocol constants for Commas and the bundled Zeta runtime."""

from collections.abc import Iterable, Mapping
from typing import Any, cast

SHELL_PROMPT_HANDOFF_TYPE = "shell_prompt"
SHELL_HANDOFF_RESULT_SCHEMA = "zeta.shell_handoff_result.v1"
SHELL_HANDOFF_RESULT_TYPE = "shell_handoff_result"

SHELL_HANDOFF_OUTCOME_EXECUTED = "executed"
SHELL_HANDOFF_OUTCOME_CANCELLED = "cancelled"
SHELL_HANDOFF_OUTCOME_NO_PENDING = "no_pending_handoff"

SHELL_HANDOFF_CANCEL_NO_TURNS = "no_shell_turns_after_handoff"
SHELL_HANDOFF_CANCEL_EXPECTED_NOT_EXECUTED = "expected_command_not_executed"


def shell_prompt_handoff(
    command: str,
    reason: str,
    *,
    artifact: str | None = None,
) -> dict[str, Any]:
    """Return the stable handoff payload a shell binding can stage."""
    handoff: dict[str, Any] = {
        "type": SHELL_PROMPT_HANDOFF_TYPE,
        "command": command,
        "reason": reason,
    }
    if artifact is not None:
        handoff["artifact"] = artifact
    return handoff


def shell_handoff_tool_result(
    command: str,
    reason: str,
    *,
    artifact: str | None = None,
) -> dict[str, Any]:
    """Return a tool result containing a shell prompt handoff."""
    return {
        "ok": True,
        "handoff": shell_prompt_handoff(command, reason, artifact=artifact),
    }


def is_shell_prompt_handoff(value: object) -> bool:
    """Return whether a value is a shell prompt handoff payload."""
    if not isinstance(value, Mapping):
        return False
    payload = cast(Mapping[str, object], value)
    return payload.get("type") == SHELL_PROMPT_HANDOFF_TYPE and isinstance(
        payload.get("command"), str
    )


def _has_schema(value: object, schema: str) -> bool:
    if not isinstance(value, Mapping):
        return False
    return cast(Mapping[str, object], value).get("schema") == schema


def is_shell_handoff_result(value: object) -> bool:
    """Return whether a tool result resolves a shell handoff."""
    return _has_schema(value, SHELL_HANDOFF_RESULT_SCHEMA)


TURN_OUTCOME_ANSWERED = "answered"
TURN_OUTCOME_STAGED = "staged"
TURN_OUTCOME_EXECUTED = "executed"
TURN_OUTCOME_CANCELLED = "cancelled"
TURN_OUTCOME_ABORTED = "aborted"
TURN_OUTCOME_FAILED = "failed"

EFFECT_KIND_FILE_WRITE = "file_write"
EFFECT_KIND_FILE_EDIT = "file_edit"
EFFECT_KIND_COMMAND = "command"
EFFECT_KIND_HANDOFF = "handoff"


def turn_contract(
    workflow: str,
    allowed_tools: Iterable[str],
    *,
    staged: bool,
) -> dict[str, Any]:
    """Return the enforced contract block of a turn record."""
    return {
        "workflow": workflow,
        "allowed_tools": list(allowed_tools),
        "staged": staged,
    }
