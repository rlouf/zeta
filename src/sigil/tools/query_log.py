"""Query-log tool implementation: read turn/effect history."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from zeta.capabilities.execution import error_result
from zeta.capabilities.types import Capability, CapabilityId

if TYPE_CHECKING:
    from sigil.history import HistoryView

DEFAULT_TURNS = 20
MAX_TURNS = 50

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "since": {
            "type": "string",
            "description": (
                "Only turns at or after a time: YYYY-MM-DD, or an age like 2d, 6h, 30m."
            ),
        },
        "workflow": {
            "type": "string",
            "description": "Only turns from one workflow: ask, propose, do, or run.",
        },
        "failed": {
            "type": "boolean",
            "description": "Only failed or aborted turns.",
        },
        "touched": {
            "type": "string",
            "description": (
                "Only turns that wrote or edited this file through the "
                "write/edit tools. Bash commands record what ran, not "
                "which files they touched."
            ),
        },
        "turn_id": {
            "type": "string",
            "description": (
                "Expand one turn in full (contract, cost, effects, prompt "
                "ids). Accepts a unique id prefix."
            ),
        },
        "current_session": {
            "type": "boolean",
            "description": (
                "Only this shell session; the default searches every session."
            ),
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_TURNS,
            "description": "Maximum number of turns to list.",
        },
    },
}

SPEC = Capability(
    CapabilityId("sigil", "query_log"),
    (
        "Query the user's turn history: what ran, in which workflow, "
        "what it touched, what it cost, and how it ended. Searches every "
        "session by default. Cite the returned turn ids in your answer. "
        "Pass turn_id to expand one record."
    ),
    SCHEMA,
)


def run(params: dict[str, Any]) -> dict[str, Any]:
    # Imported lazily: the registry imports every tool module, and the
    # display layer reaches back into zeta.context — a module-level import
    # here closes that loop into a cycle.
    from sigil.display.summarize import format_turn_line
    from sigil.history import query_history
    from sigil.sessions import session_id
    from sigil.state import history_view

    history = history_view()
    turn_token = str(params.get("turn_id") or "")
    if turn_token:
        return run_expand(history, turn_token)
    session = session_id() if params.get("current_session") is True else None
    limit = min(int(params.get("limit") or DEFAULT_TURNS), MAX_TURNS)
    try:
        turns = query_history(
            history,
            session=session,
            workflow=str(params.get("workflow") or "") or None,
            since=str(params.get("since") or "") or None,
            failed=params.get("failed") is True,
            touched=str(params.get("touched") or "") or None,
            limit=limit,
        )
    except ValueError:
        return error_result(
            "invalid-since",
            "since must be YYYY-MM-DD or an age like 2d, 6h, 30m",
        )
    if not turns:
        return {
            "ok": True,
            "content": [{"type": "text", "text": "no turns recorded"}],
            "metadata": {"turns": 0, "scope": session or "all-sessions"},
        }
    lines = [format_turn_line(turn, show_cost=True) for turn in turns]
    return {
        "ok": True,
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "metadata": {
            "turns": len(turns),
            "turn_ids": [str(turn.get("turn_id") or "")[:8] for turn in turns],
            "scope": session or "all-sessions",
            "limit": limit,
        },
    }


def run_expand(history: HistoryView, token: str) -> dict[str, Any]:
    from sigil.display.summarize import render_turn_record
    from sigil.history import (
        AmbiguousTurnError,
        UnknownTurnError,
        resolve_turn_id,
    )

    try:
        resolved = resolve_turn_id(history, token)
    except AmbiguousTurnError as error:
        return error_result(
            "ambiguous-turn-id",
            f"'{token}' matches: " + ", ".join(error.candidates),
        )
    except UnknownTurnError:
        return error_result("unknown-turn-id", f"no turn matches '{token}'")
    turn = history.turn(resolved)
    if turn is None:
        return error_result("unknown-turn-id", f"no turn matches '{token}'")
    text = "\n".join(render_turn_record(turn, history.effects_for_turn(resolved)))
    return {
        "ok": True,
        "content": [{"type": "text", "text": text}],
        "metadata": {"turn_id": resolved},
    }
