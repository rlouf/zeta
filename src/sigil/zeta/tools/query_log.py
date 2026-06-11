"""Query-log tool implementation: read the delegation ledger."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .base import ToolSpec, error_result

if TYPE_CHECKING:
    from ...ledger import LedgerIndex

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

SPEC = ToolSpec(
    "query_log",
    (
        "Query the user's delegation ledger: what ran, in which workflow, "
        "what it touched, what it cost, and how it ended. Searches every "
        "session by default. Cite the returned turn ids in your answer. "
        "Pass turn_id to expand one record."
    ),
    SCHEMA,
    effects=("read",),
)


def run(params: dict[str, Any]) -> dict[str, Any]:
    # Imported lazily: the registry imports every tool module, and the
    # display layer reaches back into zeta.prompt — a module-level import
    # here closes that loop into a cycle.
    from ...display.summarize import format_turn_line
    from ...ledger import default_ledger_index, parse_since, touched_path_variants
    from ...state import session_id

    index = default_ledger_index()
    turn_token = str(params.get("turn_id") or "")
    if turn_token:
        return run_expand(index, turn_token)
    since_raw = str(params.get("since") or "")
    since = None
    if since_raw:
        try:
            since = parse_since(since_raw)
        except ValueError:
            return error_result(
                "invalid-since",
                "since must be YYYY-MM-DD or an age like 2d, 6h, 30m",
            )
    session = session_id() if params.get("current_session") is True else None
    touched = str(params.get("touched") or "")
    limit = min(int(params.get("limit") or DEFAULT_TURNS), MAX_TURNS)
    turns = index.query_turns(
        session=session,
        workflow=str(params.get("workflow") or "") or None,
        since=since,
        failed=params.get("failed") is True,
        touched=touched_path_variants(touched) if touched else None,
        limit=limit,
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


def run_expand(index: LedgerIndex, token: str) -> dict[str, Any]:
    from ...display.summarize import render_turn_record
    from ...ledger import AmbiguousTurnError, UnknownTurnError, resolve_turn_id

    try:
        resolved = resolve_turn_id(index, token)
    except AmbiguousTurnError as error:
        return error_result(
            "ambiguous-turn-id",
            f"'{token}' matches: " + ", ".join(error.candidates),
        )
    except UnknownTurnError:
        return error_result("unknown-turn-id", f"no turn matches '{token}'")
    turn = index.turn(resolved)
    if turn is None:
        return error_result("unknown-turn-id", f"no turn matches '{token}'")
    text = "\n".join(render_turn_record(turn, index.effects_for_turn(resolved)))
    return {
        "ok": True,
        "content": [{"type": "text", "text": text}],
        "metadata": {"turn_id": resolved},
    }
