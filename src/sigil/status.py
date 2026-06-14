"""Compact current-session status for shell-native Sigil workflows."""

from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass
from typing import Any, Literal

from zeta.models import resolve_active_model

from .ledger import ledger_index, warn_ledger_failure_once
from .session import latest_active_failure
from .state import session_id

StatusState = Literal["clean", "attention"]
DELEGATION_WORKFLOWS = ("ask", "propose", "do")
LEDGER_SCAN_LIMIT = 50


@dataclass(frozen=True)
class Status:
    """Current operational status for the shell session."""

    state: StatusState
    reason: str
    session_id: str
    cwd: str
    actions: tuple[str, ...]
    details: dict[str, object]
    model: dict[str, str]
    last_turn: dict[str, Any] | None
    pending: dict[str, Any] | None
    today: dict[str, int]

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable status payload."""
        return asdict(self)


def current_status() -> Status:
    """Reduce current session state into the most important live condition."""
    current_session = session_id()
    cwd = os.getcwd()
    model = active_model_fields()
    last_turn, pending, today = ledger_status_fields(current_session)

    failure = latest_active_failure()
    if failure is not None:
        return attention(
            "last command failed",
            session=current_session,
            cwd=cwd,
            actions=(", suggest a fix",),
            details={
                "event_id": failure.get("event_id"),
                "command": failure.get("command"),
                "status": failure.get("status"),
                "cwd": failure.get("cwd"),
            },
            model=model,
            last_turn=last_turn,
            pending=pending,
            today=today,
        )

    return Status(
        state="clean",
        reason="clean",
        session_id=current_session,
        cwd=cwd,
        actions=(),
        details={},
        model=model,
        last_turn=last_turn,
        pending=pending,
        today=today,
    )


def attention(
    reason: str,
    *,
    session: str,
    cwd: str,
    actions: tuple[str, ...],
    details: dict[str, object],
    model: dict[str, str],
    last_turn: dict[str, Any] | None = None,
    pending: dict[str, Any] | None = None,
    today: dict[str, int] | None = None,
) -> Status:
    """Build an attention status."""
    return Status(
        state="attention",
        reason=reason,
        session_id=session,
        cwd=cwd,
        actions=actions,
        details=details,
        model=model,
        last_turn=last_turn,
        pending=pending,
        today=today or {},
    )


def active_model_fields() -> dict[str, str]:
    """Return the resolved model the next request will use, with its source."""
    from . import configure_zeta_for_sigil

    configure_zeta_for_sigil()
    resolution = resolve_active_model()
    selection = resolution.selection
    fields = {
        "profile": selection.profile,
        "model": selection.model,
        "url": selection.url,
        "source": resolution.source,
    }
    if resolution.stale_profile is not None:
        fields["stale_profile"] = resolution.stale_profile
    return fields


def ledger_status_fields(
    current_session: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, int]]:
    """Read the session's ledger facts, failing open to empty values."""
    try:
        index = ledger_index()
        turns = index.query_turns(session=current_session, limit=LEDGER_SCAN_LIMIT)
        last_turn = next(
            (turn for turn in turns if turn.get("workflow") in DELEGATION_WORKFLOWS),
            None,
        )
        pending = index.pending_staged_command(current_session)
        today = index.cost_since(current_session, local_midnight())
    except Exception as exc:
        warn_ledger_failure_once("status", exc)
        return None, None, {}
    return last_turn, pending, today


def local_midnight() -> float:
    """Return the epoch time of today's local midnight."""
    now = time.localtime()
    return time.mktime(
        (now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, now.tm_wday, now.tm_yday, -1)
    )


def format_status(status: Status) -> str:
    """Render status as terse human-readable terminal text."""
    if status.state == "clean":
        return "\n".join(
            ["clean", *ledger_status_lines(status), format_model_line(status.model)]
        )

    lines = [f"attention: {status.reason}"]
    details = status.details

    command = details.get("command")
    if command:
        lines.extend(["", "command", f"  {command}"])

    objective = details.get("objective")
    if objective:
        lines.extend(["", "objective", f"  {objective}"])

    if status.actions:
        lines.extend(["", "next"])
        lines.extend(f"  {action}" for action in status.actions)

    extra = ledger_status_lines(status)
    if extra:
        lines.extend(["", *extra])
        lines.append(format_model_line(status.model))
    else:
        lines.extend(["", format_model_line(status.model)])
    return "\n".join(lines)


def ledger_status_lines(status: Status) -> list[str]:
    """Render the session's ledger facts as status lines."""
    lines = []
    last = status.last_turn
    if last:
        parts = [str(last.get("workflow") or "?"), str(last.get("outcome") or "?")]
        objective = text_head(last.get("objective"))
        if objective:
            parts.append(objective)
        lines.append("last: " + " · ".join(parts))
    pending = status.pending
    if pending:
        lines.append(f"staged: {text_head(pending.get('command'))} (pending)")
    today = status.today
    if today.get("turns"):
        tokens = int(today.get("input_tokens") or 0) + int(
            today.get("output_tokens") or 0
        )
        turns = int(today["turns"])
        lines.append(
            f"today: {tokens} tok · {today.get('model_calls') or 0} calls"
            f" · {turns} turn" + ("" if turns == 1 else "s")
        )
    return lines


def text_head(value: object, limit: int = 60) -> str:
    """Return the first display line of a value, bounded."""
    text = str(value or "").strip()
    if not text:
        return ""
    line = text.splitlines()[0]
    return line if len(line) <= limit else line[: limit - 1] + "…"


def format_model_line(model: dict[str, str]) -> str:
    """Render the resolved model selection as one status line."""
    source = model["source"]
    stale_profile = model.get("stale_profile")
    if stale_profile:
        source = f"{source}; profile {stale_profile!r} missing from models.toml"
    return f"model: {model['profile']} -> {model['model']} @ {model['url']} ({source})"
