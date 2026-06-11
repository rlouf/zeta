"""Shared protocol constants for Sigil and the bundled Zeta runtime."""

from __future__ import annotations

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


TURN_RECORD_TYPE = "turn"
TURN_RECORD_SCHEMA = "sigil.turn.v1"
EFFECT_RECORD_TYPE = "effect"
EFFECT_RECORD_SCHEMA = "sigil.effect.v1"

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


def turn_record(
    turn_id: str,
    *,
    workflow: str,
    objective: str,
    contract: Mapping[str, Any],
    outcome: str,
    agent: Mapping[str, str] | None = None,
    cost: Mapping[str, int] | None = None,
    prompt_object_ids: Iterable[str] = (),
    effect_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Return one ledger turn record; the event envelope adds time/cwd/session."""
    record: dict[str, Any] = {
        "type": TURN_RECORD_TYPE,
        "schema": TURN_RECORD_SCHEMA,
        "turn_id": turn_id,
        "workflow": workflow,
        "objective": objective,
        "contract": dict(contract),
        "outcome": outcome,
        "prompt_object_ids": list(prompt_object_ids),
        "effect_ids": list(effect_ids),
    }
    if agent is not None:
        record["agent"] = dict(agent)
    if cost is not None:
        record["cost"] = dict(cost)
    return record


def effect_record(
    effect_id: str,
    *,
    turn_id: str,
    kind: str,
    staged: bool,
    path: str | None = None,
    before_hash: str | None = None,
    after_hash: str | None = None,
    command: str | None = None,
    exit_status: int | None = None,
    duration_ms: int | None = None,
    tool_call_id: str | None = None,
    resolved_outcome: str | None = None,
) -> dict[str, Any]:
    """Return one ledger effect record; unset optional facts are omitted."""
    record: dict[str, Any] = {
        "type": EFFECT_RECORD_TYPE,
        "schema": EFFECT_RECORD_SCHEMA,
        "effect_id": effect_id,
        "turn_id": turn_id,
        "kind": kind,
        "staged": staged,
    }
    optionals: dict[str, Any] = {
        "path": path,
        "before_hash": before_hash,
        "after_hash": after_hash,
        "command": command,
        "exit_status": exit_status,
        "duration_ms": duration_ms,
        "tool_call_id": tool_call_id,
        "resolved_outcome": resolved_outcome,
    }
    record.update({key: value for key, value in optionals.items() if value is not None})
    return record


def is_turn_record(value: object) -> bool:
    """Return whether a value is a ledger turn record."""
    return _has_schema(value, TURN_RECORD_SCHEMA)


def is_effect_record(value: object) -> bool:
    """Return whether a value is a ledger effect record."""
    return _has_schema(value, EFFECT_RECORD_SCHEMA)
